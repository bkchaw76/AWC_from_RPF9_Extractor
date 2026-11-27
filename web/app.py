"""Experimental headless web radio player using upstream AWC's indexing and schedules."""
import asyncio
import queue
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "radio"), str(ROOT / "extractor")]
os.environ.setdefault("APPDATA", "/cache")
import awc_credits as credits
import awc_icons as icons
import awc_dj
from awc_radio import SHAPES, segment_at
from awc_radio import Schedule, load_index, make_index, cache_dir, extract_track, _tkey, credits_cache_path

GAME = Path(os.getenv("GAME_DIR", "/game"))
RPF = GAME / "audio.rpf"
POOL = GAME / "data/ttdb/ui/ttdb_ui.ttpf"
BITRATE = os.getenv("RADIO_BITRATE", "128")
if BITRATE not in ("128", "192", "256"):
    raise ValueError("RADIO_BITRATE must be 128, 192 or 256")
CONTENT = os.getenv("RADIO_CONTENT", "ads,news,dj,talk,jingles").split(",")
DJ = os.getenv("RADIO_DJ", "true").lower() in ("1", "true", "yes")
app = FastAPI(title="AWC Radio Web")
state = {"status": "starting", "error": None, "index": None}
state_lock = threading.Lock()
tracks_lock = threading.Lock()
schedule_lock = threading.Lock()
schedules = {}
hubs_lock = threading.Lock()
hubs = {}

def load_credits():
    cache = Path(credits_cache_path())
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    try:
        songs = credits.parse(credits.download(timeout=25))
    except Exception:
        songs = []
    if songs:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(songs), encoding="utf-8")
    return songs

def initialize():
    try:
        if not RPF.is_file():
            raise FileNotFoundError("audio.rpf not found under /game")
        pool = str(POOL) if POOL.is_file() else ""
        with state_lock:
            state["status"] = "scanning"
        idx = load_index(str(RPF), pool) or make_index(str(RPF), pool, load_credits)
        if not idx or not idx.get("stations"):
            raise ValueError("No stations found in audio.rpf")
        if pool:
            try:
                icons.extract(pool)
            except Exception as exc:
                print("Logo extraction failed:", exc, flush=True)
        with state_lock:
            state.update(status="ready", index=idx, error=None)
        print("AWC Web: %d stations ready" % len(idx["stations"]), flush=True)
    except Exception as exc:
        with state_lock:
            state.update(status="error", error=str(exc))
        print("Startup failed:", exc, flush=True)

@app.on_event("startup")
def startup():
    threading.Thread(target=initialize, daemon=True).start()

@app.get("/")
def home():
    return FileResponse(ROOT / "web/static/index.html")

@app.get("/api/status")
def status():
    with state_lock:
        idx = state["index"]
        return {"status": state["status"], "error": state["error"],
                "station_count": len(idx["stations"]) if idx else 0,
                "data_rpf": (GAME / "data.rpf").is_file(), "logos": POOL.is_file()}

@app.get("/api/stations")
def stations():
    with state_lock:
        idx = state["index"]
    if idx is None:
        return []
    return [{"name": name, "tracks": len(v.get("songs", []))}
            for name, v in idx["stations"].items()]

def station(name):
    with state_lock:
        idx = state["index"]
    if idx is None:
        raise HTTPException(503, "Indexing has not completed")
    if name not in idx["stations"]:
        raise HTTPException(404, "Station not found")
    with schedule_lock:
        if name not in schedules:
            schedules[name] = Schedule(name, idx["stations"][name], content=CONTENT, news=idx.get("news", []))
        return idx, schedules[name]

def current_item(sched, now):
    with schedule_lock:
        return sched.items_from(now, 1)[0]

@app.get("/api/logo/{name}")
def logo(name: str):
    station(name)
    path = icons.icon_path(name)
    if not path:
        raise HTTPException(404, "Logo not found")
    return FileResponse(path, media_type="image/png")

@app.get("/api/stations/{name}/now")
def now_playing(name: str):
    idx, sched = station(name)
    start, end, kind, track, _, _ = current_item(sched, time.time())
    seg = segment_at(track, max(0, time.time()-start))
    return {"station": name, "kind": kind, "title": (seg[1] if seg and seg[1] else track.get("title", "")),
            "artist": (seg[2] if seg and seg[1] else track.get("artist", "")), "start": start, "end": end}

def track_file(idx, tr):
    dest = Path(cache_dir()) / ("r_" + _tkey(tr) + ".opus")
    # One global extraction lock avoids concurrent partial-file collisions.
    with tracks_lock:
        if not dest.exists() or dest.stat().st_size == 0:
            tmp = str(dest) + ".part"
            try:
                extract_track(idx, str(RPF), tr, tmp)
                os.replace(tmp, dest)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
    return dest

def programme_window(sched, now):
    with schedule_lock:
        items = sched.items_from(now, 2)
        active = [items[0]]
        at = items[0][0]
        for _ in range(2):
            prev = sched.items_from(at - .001, 1)[0]
            if prev[0] >= at:
                break
            if prev[1] > now:
                active.append(prev)
            at = prev[0]
        end = min(items[1][0], *(x[1] for x in active))
        if end <= now:
            return end, []
        plan = [(item, sched.voices(item) if DJ else [], sched.bed_of(item),
                 sched.talk_over(item, DJ)) for item in active]
        return end, plan

def mix_command(idx, plan, now, end):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-filter_complex_threads", "1"]
    filters, labels = [], []
    n = 0
    duration = end-now

    def source(track, offset, length, delay=0, gain=1, shape=(), loop=False):
        nonlocal n
        path = track_file(idx, track)
        if loop:
            cmd.extend(["-stream_loop", "-1"])
        cmd.extend(["-ss", "%.3f" % offset, "-re", "-i", str(path)])
        label = "a%d" % n
        chain = ["aresample=44100", "aformat=channel_layouts=stereo",
                 "atrim=duration=%.3f" % length,
                 "asetpts=N/SR/TB+%.3f/TB" % offset]
        chain.extend(shape)
        chain.extend(["asetpts=N/SR/TB", "volume=%.3f" % gain])
        if delay > 0:
            chain.append("adelay=%d:all=1" % round(delay*1000))
        filters.append("[%d:a]%s[%s]" % (n, ",".join(chain), label))
        labels.append("[%s]" % label)
        n += 1

    for item, voices, bed, ducks in plan:
        start, finish, kind, track, fin, fout = item
        offset = max(0, now-start)
        length = min(end, finish)-now
        if length <= 0:
            continue
        shape = []
        spans = ducks + [(at, at+line[2]) for at,line in voices]
        if spans:
            shape.append("volume=eval=frame:volume='%s'" % awc_dj.duck_filter(spans))
        for direction, fade in (("in", fin), ("out", fout)):
            if fade:
                shape.append("afade=t=%s:st=%.3f:d=%.3f:curve=%s" %
                             (direction, fade[0], fade[1], SHAPES[fade[2]]))
        source(track, offset, length, shape=shape)
        if bed:
            source(bed, offset % max(.1, bed["sec"]), length, gain=awc_dj.BED_GAIN, loop=True)
        for at, line in voices:
            begin = start+at
            lo, hi = max(now, begin), min(end, begin+line[2])
            if hi > lo:
                source({"base":line[0], "idx":line[1], "sec":line[2]},
                       lo-begin, hi-lo, delay=lo-now)
    filters.append("".join(labels)+"amix=inputs=%d:duration=longest:normalize=0,alimiter=limit=0.89:level=disabled,apad[out]" % n)
    return cmd+["-filter_complex", ";".join(filters), "-map", "[out]",
                "-t", "%.3f" % duration, "-vn", "-ac", "2", "-ar", "44100",
                "-c:a", "libmp3lame", "-b:a", BITRATE+"k", "-write_xing", "0",
                "-id3v2_version", "0", "-f", "mp3", "pipe:1"]

def mp3_stream(name, stop):
    idx, sched = station(name)
    proc = None
    try:
        while not stop.is_set():
            now = time.time()
            end, plan = programme_window(sched, now)
            if not plan or end-now < .15:
                stop.wait(.15)
                continue
            # Warm the extraction cache before fixing the live offset.
            for item, voices, bed, _ in plan:
                track_file(idx, item[3])
                if bed:
                    track_file(idx, bed)
                for _, line in voices:
                    track_file(idx, {"base":line[0], "idx":line[1], "sec":line[2]})
            now = time.time()
            end, plan = programme_window(sched, now)
            if not plan or end-now < .15 or stop.is_set():
                continue
            cmd = mix_command(idx, plan, now, end)
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
            while not stop.is_set():
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                yield chunk
            if stop.is_set():
                proc.kill()
            rc = proc.wait()
            proc.stdout.close()
            proc = None
            if rc and not stop.is_set():
                raise RuntimeError("FFmpeg mixer exited with code %d" % rc)
    finally:
        if proc is not None:
            proc.kill()
            proc.wait()
            proc.stdout.close()

class StationHub:
    def __init__(self, name):
        self.name = name
        self.stop = threading.Event()
        self.listeners = set()

    def run(self):
        generator = mp3_stream(self.name, self.stop)
        try:
            for chunk in generator:
                with hubs_lock:
                    for listener in list(self.listeners):
                        try:
                            listener.put_nowait(chunk)
                        except queue.Full:
                            # Disconnect slow clients rather than blocking the station.
                            self.listeners.discard(listener)
                            while not listener.empty():
                                try:
                                    listener.get_nowait()
                                except queue.Empty:
                                    break
                            listener.put_nowait(None)
                    if not self.listeners:
                        self.stop.set()
        except Exception as exc:
            print("Station stream failed:", self.name, exc, flush=True)
        finally:
            generator.close()
            with hubs_lock:
                self.stop.set()
                for listener in self.listeners:
                    while not listener.empty():
                        try:
                            listener.get_nowait()
                        except queue.Empty:
                            break
                    listener.put_nowait(None)
                self.listeners.clear()
                if hubs.get(self.name) is self:
                    hubs.pop(self.name, None)

async def shared_stream(name):
    listener = queue.Queue(maxsize=64)
    with hubs_lock:
        hub = hubs.get(name)
        if hub is None or hub.stop.is_set():
            hub = StationHub(name)
            hubs[name] = hub
            hub.listeners.add(listener)
            threading.Thread(target=hub.run, daemon=True).start()
        else:
            hub.listeners.add(listener)
    try:
        while True:
            try:
                chunk = listener.get_nowait()
            except queue.Empty:
                await asyncio.sleep(.05)
                continue
            if chunk is None:
                break
            yield chunk
    finally:
        with hubs_lock:
            hub.listeners.discard(listener)
            if not hub.listeners:
                hub.stop.set()

@app.get("/api/streams")
def active_streams():
    with hubs_lock:
        return [{"station": name, "listeners": len(hub.listeners)}
                for name, hub in hubs.items() if not hub.stop.is_set()]

@app.get("/api/stations/{name}/stream")
def stream(name: str):
    station(name)
    return StreamingResponse(shared_stream(name), media_type="audio/mpeg",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
