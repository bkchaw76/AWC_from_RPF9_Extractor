"""Experimental headless web radio player using upstream AWC's indexing and schedules."""
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
from awc_radio import Schedule, load_index, make_index, cache_dir, extract_track, _tkey, credits_cache_path

GAME = Path(os.getenv("GAME_DIR", "/game"))
RPF = GAME / "audio.rpf"
POOL = GAME / "data/ttdb/ui/ttdb_ui.ttpf"
app = FastAPI(title="AWC Radio Web")
state = {"status": "starting", "error": None, "index": None}
state_lock = threading.Lock()
tracks_lock = threading.Lock()

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
    return idx, Schedule(name, idx["stations"][name], content=False, news=idx.get("news", []))

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
    start, end, kind, track, _, _ = sched.items_from(time.time(), 1)[0]
    return {"station": name, "kind": kind, "title": track.get("title", ""),
            "artist": track.get("artist", ""), "start": start, "end": end}

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

def mp3_stream(name):
    idx, sched = station(name)
    proc = None
    try:
        while True:
            now = time.time()
            start, end, kind, track, fin, fout = sched.items_from(now, 1)[0]
            if end <= now + 0.2:
                time.sleep(.15)
                continue
            path = track_file(idx, track)
            now = time.time()
            if now >= end - .2:
                continue
            offset = max(0.0, now - start)
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-ss", "%.3f" % offset, "-re", "-i", str(path),
                   "-t", "%.3f" % max(.1, end-now),
                   "-vn", "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame",
                   "-b:a", "128k", "-write_xing", "0", "-id3v2_version", "0",
                   "-f", "mp3", "pipe:1"]
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                yield chunk
            proc.wait()
            proc = None
    finally:
        if proc is not None:
            proc.kill()
            proc.wait()

@app.get("/api/stations/{name}/stream")
def stream(name: str):
    station(name)
    return StreamingResponse(mp3_stream(name), media_type="audio/mpeg",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
