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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, RedirectResponse, JSONResponse
import base64, hashlib, hmac, secrets
from urllib.parse import parse_qs

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

# Remote authentication is opt-in; retain the existing NPM access list until tested.
AUTH_PASSWORD = os.getenv("RADIO_AUTH_PASSWORD", "")
AUTH_USER = os.getenv("RADIO_AUTH_USER", "awcradio")
AUTH_KEY = os.getenv("RADIO_SESSION_SECRET", "")
LOCAL_HOST = os.getenv("RADIO_LOCAL_HOST", "192.168.8.8:8088")
SESSION_AGE = 30 * 86400
if AUTH_PASSWORD and len(AUTH_KEY) < 32:
    raise RuntimeError("RADIO_SESSION_SECRET must contain at least 32 characters")
def equal(a, b):
    return hmac.compare_digest(a.encode(), b.encode())
def credentials(u, p):
    return equal(u, AUTH_USER) & equal(p, AUTH_PASSWORD)
def session_token():
    payload = str(int(time.time()) + SESSION_AGE) + "." + secrets.token_hex(16)
    return payload + "." + hmac.new(AUTH_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
def valid_session(token):
    try:
        expiry, nonce, sig = token.split(".")
        expected = hmac.new(AUTH_KEY.encode(), (expiry+"."+nonce).encode(), hashlib.sha256).hexdigest()
        return int(expiry) > time.time() and equal(sig, expected)
    except (ValueError, TypeError):
        return False
LOGIN = """<!doctype html><html><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#11131b"><title>AWC Radio Login</title><style>body{background:#11131b;color:white;font:17px system-ui;display:grid;place-items:center;min-height:90vh}form{width:min(85vw,340px)}input,button{box-sizing:border-box;width:100%;padding:14px;margin:8px 0;border-radius:9px;font:inherit}button{background:#e9ad60;border:0}h1{color:#e9ad60}</style><form method="post" action="/login"><h1>AWC RADIO</h1><p>Sign in to your radio</p><input name="username" autocomplete="username" placeholder="Username" required><input name="password" type="password" autocomplete="current-password" placeholder="Password" required><button>Sign in for 30 days</button></form></html>"""
@app.middleware("http")
async def remote_auth(request: Request, call_next):
    if not AUTH_PASSWORD:
        return await call_next(request)
    # NPM must overwrite X-AWC-Remote on every public request.
    local = (request.headers.get("host","").lower() == LOCAL_HOST.lower()
             and not request.headers.get("x-awc-remote")
             and not request.headers.get("x-forwarded-for")
             and not request.headers.get("forwarded"))
    if local:
        return await call_next(request)
    path = request.url.path
    if path in ("/icon.svg", "/manifest.webmanifest"):
        return await call_next(request)
    if path == "/login" and request.method == "POST":
        if request.headers.get("origin") != "https://" + request.headers.get("host",""):
            return HTMLResponse("Invalid origin", status_code=403)
        if request.headers.get("content-type","").split(";")[0] != "application/x-www-form-urlencoded":
            return HTMLResponse("Invalid form", status_code=400)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>4096:
                return HTMLResponse("Form too large", status_code=413)
        fields = parse_qs(body.decode("utf-8", errors="replace"))
        if credentials(fields.get("username",[""])[0], fields.get("password",[""])[0]):
            response = RedirectResponse("/", status_code=303)
            response.set_cookie("awc_session", session_token(), max_age=SESSION_AGE, secure=True, httponly=True, samesite="strict")
            response.headers["Cache-Control"]="no-store"
            return response
        await asyncio.sleep(1)
        return HTMLResponse(LOGIN.replace("Sign in to your radio","Incorrect username or password"), status_code=401, headers={"Cache-Control":"no-store"})
    if path == "/logout" and request.method == "POST":
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie("awc_session", secure=True, httponly=True, samesite="strict")
        return response
    auth = request.headers.get("authorization","")
    basic = False
    if auth.lower().startswith("basic "):
        try:
            u,p = base64.b64decode(auth[6:], validate=True).decode().split(":",1)
            basic = credentials(u,p)
        except (ValueError, UnicodeError):
            pass
    if basic or valid_session(request.cookies.get("awc_session","")):
        return await call_next(request)
    if path == "/" or path == "/login":
        return HTMLResponse(LOGIN, headers={"Cache-Control":"no-store"})
    return JSONResponse({"detail":"Authentication required"}, status_code=401, headers={"Cache-Control":"no-store"})


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
    with state_lock:
        idx = state["index"]
    if idx is None:
        raise HTTPException(503, "Indexing")
    if name not in idx["stations"] and name not in idx.get("podcasts", {}):
        raise HTTPException(404, "Unknown station or podcast")
    path = icons.icon_path(name)
    if not path:
        raise HTTPException(404, "Logo not found")
    return FileResponse(path, media_type="image/png")

# Artwork lookups are independent of the streaming pipeline.
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen, Request as URLRequest
from concurrent.futures import ThreadPoolExecutor
import unicodedata
art_pool = ThreadPoolExecutor(max_workers=1)
art_lock = threading.Lock()
art_pending = set()
art_misses = {}
art_dir = Path(os.environ["APPDATA"]) / "album-art"
def art_norm(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())
def art_key(title, artist):
    return hashlib.sha256((art_norm(title)+"|"+art_norm(artist)).encode()).hexdigest()
import re
def match_artist(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"\b(?:and|with|featuring|feat|ft)\b\.?|&|,", " ", value)
    return art_norm(value)
def match_title(value):
    value = re.sub(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+[^)\]]+[)\]]", "", value, flags=re.I)
    value = re.sub(r"\s*(?:[-–—]\s*|[\(\[]\s*)(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?\s*[)\]]?\s*$", "", value, flags=re.I)
    return art_norm(value)
art_last_query = 0.0
art_errors = set()
def search_art(term, country):
    global art_last_query
    time.sleep(max(0, 3.2-(time.monotonic()-art_last_query)))
    art_last_query = time.monotonic()
    query = urlencode({"term":term,"entity":"song","limit":50,"country":country,"explicit":"Yes"})
    for attempt in range(2):
        try:
            with urlopen("https://itunes.apple.com/search?"+query,timeout=8) as response:
                return json.loads(response.read(2000000)).get("results",[])
        except Exception:
            if attempt:
                raise
            time.sleep(4)
            art_last_query = time.monotonic()
def fallback_art(title,artist,key):
    from urllib.request import Request as URLRequest
    query = urlencode({"query":'recording:"'+title.replace('"','')+'" AND artist:"'+artist.replace('"','')+'"',"fmt":"json","limit":10})
    req = URLRequest("https://musicbrainz.org/ws/2/recording/?"+query,headers={"User-Agent":"AWC-Radio/0.5.5 (https://github.com/bkchaw76/AWC_from_RPF9_Extractor)"})
    with urlopen(req,timeout=10) as response:
        recordings = json.loads(response.read(2000000)).get("recordings",[])
    releases = {}
    for rec in recordings:
        credit = "".join(x.get("name","")+x.get("joinphrase","") for x in rec.get("artist-credit",[]))
        if match_title(rec.get("title",""))!=match_title(title) or match_artist(credit)!=match_artist(artist):
            continue
        for rel in rec.get("releases",[]):
            group = rel.get("release-group",{})
            if rel.get("status")=="Official" and group.get("primary-type")=="Album" and not group.get("secondary-types"):
                releases[rel["id"]] = rel.get("date") or "9999"
    from urllib.error import HTTPError
    for rid in sorted(releases,key=releases.get)[:5]:
        try:
            with urlopen("https://coverartarchive.org/release/"+rid+"/front-500",timeout=10) as response:
                data = response.read(2000001)
            if len(data)<=2000000 and data.startswith(b"\xff\xd8\xff"):
                art_dir.mkdir(parents=True,exist_ok=True)
                tmp = art_dir/(key+".tmp")
                tmp.write_bytes(data)
                os.replace(tmp,art_dir/(key+".jpg"))
                return True
        except HTTPError as exc:
            if exc.code!=404:
                raise
    return False
def lookup_override_art(title, artist, key):
    global art_last_query
    overrides = json.loads((ROOT/"web/artwork_overrides.json").read_text())
    ids = next((entry["track_ids"] for entry in overrides
                if match_artist(entry["artist"])==match_artist(artist)
                and match_title(entry["title"])==match_title(title)), [])
    for track_id in ids:
        for country in ("SG","US"):
            time.sleep(max(0,3.2-(time.monotonic()-art_last_query)))
            art_last_query = time.monotonic()
            query = urlencode({"id":track_id,"country":country,"entity":"song"})
            with urlopen("https://itunes.apple.com/lookup?"+query,timeout=8) as response:
                items = json.loads(response.read(2000000)).get("results",[])
            for item in items:
                if item.get("trackId")!=track_id or match_title(item.get("trackName",""))!=match_title(title) or match_artist(item.get("artistName",""))!=match_artist(artist):
                    continue
                url = item.get("artworkUrl100","").replace("100x100bb","600x600bb")
                parsed = urlparse(url)
                if parsed.scheme!="https" or not (parsed.hostname or "").endswith(".mzstatic.com"):
                    continue
                with urlopen(url,timeout=8) as response:
                    data = response.read(2000001)
                if len(data)<=2000000 and data.startswith(b"\xff\xd8\xff"):
                    art_dir.mkdir(parents=True,exist_ok=True)
                    tmp = art_dir/(key+".tmp")
                    tmp.write_bytes(data)
                    os.replace(tmp,art_dir/(key+".jpg"))
                    return True
    return False
def fetch_art(title, artist, key):
    failed = False
    try:
        if (art_dir/(key+".jpg")).is_file():
            return
        wanted_artist = match_artist(artist)
        wanted_title = match_title(title)
        # Require matching title and full artist credit, including featured credits
        # carried in the catalogue track name. Never accept title-only matches.
        for country in ("SG","US"):
            try:
                results = search_art(artist+" "+title,country)
            except Exception:
                failed = True
                continue
            matches = []
            for item in results:
                track_name = item.get("trackName","")
                if match_title(track_name)!=wanted_title:
                    continue
                credited = item.get("artistName","")
                featured = re.search(r"[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+([^)\]]+)",track_name,re.I)
                if match_artist(credited)!=wanted_artist and (not featured or match_artist(credited+" "+featured.group(1))!=wanted_artist):
                    continue
                collection = item.get("collectionName","").casefold()
                compilation = bool(re.search(r"greatest hits|best of|collection|anthology|various artists|essential",collection))
                matches.append((compilation,item.get("releaseDate") or "9999",item))
            for _,_,item in sorted(matches,key=lambda x:(x[0],x[1])):
                url = item.get("artworkUrl100","").replace("100x100bb","600x600bb")
                parsed = urlparse(url)
                if parsed.scheme!="https" or not (parsed.hostname or "").endswith(".mzstatic.com"):
                    continue
                try:
                    with urlopen(url,timeout=8) as response:
                        data = response.read(2000001)
                    if len(data)>2000000 or not data.startswith(b"\xff\xd8\xff"):
                        continue
                    art_dir.mkdir(parents=True,exist_ok=True)
                    tmp = art_dir/(key+".tmp")
                    tmp.write_bytes(data)
                    os.replace(tmp,art_dir/(key+".jpg"))
                    return
                except Exception:
                    failed = True
                    continue
        try:
            if lookup_override_art(title,artist,key):
                return
        except Exception:
            failed = True
        fallback_art(title,artist,key)
    except Exception:
        failed = True
    finally:
        with art_lock:
            if failed and not (art_dir/(key+".jpg")).exists():
                art_errors.add(key)
            else:
                art_errors.discard(key)
            art_pending.discard(key)
            if not (art_dir / (key+".jpg")).exists():
                art_misses[key] = time.monotonic()+3600
def album_art(title, artist):
    if not title or not artist:
        return None
    key = art_key(title, artist)
    if (art_dir / (key+".jpg")).is_file():
        return "/api/artwork/"+key
    with art_lock:
        if key not in art_pending and art_misses.get(key,0)<time.monotonic() and len(art_pending)<8:
            art_pending.add(key)
            art_pool.submit(fetch_art,title,artist,key)
    return None
@app.get("/api/artwork/{key}")
def artwork(key: str):
    if len(key)!=64 or any(c not in "0123456789abcdef" for c in key):
        raise HTTPException(404)
    path = art_dir / (key+".jpg")
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control":"private, max-age=86400"})

# Bulk artwork management: single rate-limited worker, persistent results.
from PIL import Image
import zipfile, tempfile
bulk_lock = threading.Lock()
bulk_state = {"running": False, "station": "all", "processed": 0, "total": 0}
bulk_pause = threading.Event()
bulk_results = art_dir / "bulk-results.json"
def artwork_library():
    idx = state.get("index")
    if not idx:
        raise HTTPException(503, "Index not ready")
    library = {}
    for name, station_data in idx["stations"].items():
        for track in station_data.get("songs", []):
            pairs = [(track.get("title"), track.get("artist"))]
            pairs += [(part[1],part[2]) for part in track.get("m",[]) if len(part)>=3]
            for title, artist in pairs:
                if not title or not artist:
                    continue
                key = art_key(title,artist)
                item = library.setdefault(key, {"title":title,"artist":artist,"stations":[]})
                if name not in item["stations"]:
                    item["stations"].append(name)
    return library
def bulk_load():
    try:
        return json.loads(bulk_results.read_text())
    except (OSError, ValueError):
        return {}
def bulk_save(results):
    art_dir.mkdir(parents=True,exist_ok=True)
    tmp = bulk_results.with_suffix(".tmp")
    tmp.write_text(json.dumps(results,ensure_ascii=False))
    os.replace(tmp,bulk_results)
def local_art_admin(request):
    if (request.headers.get("host","").lower()!=LOCAL_HOST.lower()
        or any(request.headers.get(h) for h in ("x-awc-remote","x-forwarded-for","forwarded"))
        or request.headers.get("origin")!="http://"+LOCAL_HOST):
        raise HTTPException(403,"Artwork management requires local access")
def bulk_run(items, retry):
    results = bulk_load()
    try:
        for key,item in items:
            if bulk_pause.is_set():
                break
            if not (art_dir/(key+".jpg")).is_file() and (retry or key not in results or results.get(key)=="error"):
                # Share the existing single lookup worker with live playback.
                with art_lock:
                    pending = key in art_pending
                    if not pending:
                        art_pending.add(key)
                        future = art_pool.submit(fetch_art,item["title"],item["artist"],key)
                if pending:
                    while True:
                        with art_lock:
                            if key not in art_pending:
                                break
                        time.sleep(.2)
                else:
                    future.result()
                results[key] = "cached" if (art_dir/(key+".jpg")).is_file() else ("error" if key in art_errors else "unmatched")
                bulk_save(results)
                if bulk_pause.wait(3.2):
                    break
            with bulk_lock:
                bulk_state["processed"] += 1
    finally:
        with bulk_lock:
            bulk_state["running"] = False
@app.get("/api/artwork-manager/status")
def bulk_status():
    library = artwork_library()
    results = bulk_load()
    cached = {k for k in library if (art_dir/(k+".jpg")).is_file()}
    missing = {k for k in library if k not in cached and results.get(k) in ("unmatched","error")}
    names = list(state["index"]["stations"])
    with bulk_lock:
        progress = dict(bulk_state)
    return dict(progress, unique=len(library), downloaded=len(cached), unmatched=len(missing),
                stations=[{"name":n,"total":sum(n in v["stations"] for v in library.values()),
                           "cached":sum(n in library[k]["stations"] for k in cached)} for n in names],
                missing=[dict(library[k],lookup_status=results.get(k)) for k in sorted(missing)],
                covers=[dict(library[k],key=k) for k in sorted(cached)[:100]],
                bytes=sum(p.stat().st_size for p in art_dir.glob("*.jpg")))
@app.post("/api/artwork-manager/{action}")
def bulk_control(action: str, request: Request, station: str="all"):
    local_art_admin(request)
    if action=="pause":
        bulk_pause.set()
        return {"ok":True}
    if action not in ("start","retry"):
        raise HTTPException(404)
    library = artwork_library()
    if station!="all" and station not in state["index"]["stations"]:
        raise HTTPException(400,"Unknown station")
    items = [(k,v) for k,v in library.items() if station=="all" or station in v["stations"]]
    with bulk_lock:
        if bulk_state["running"]:
            raise HTTPException(409,"Already running")
        bulk_pause.clear()
        bulk_state.update(running=True,station=station,processed=0,total=len(items))
        threading.Thread(target=bulk_run,args=(items,action=="retry"),daemon=True).start()
    return {"ok":True}
@app.get("/api/artwork-manager/export")
def bulk_export(request: Request):
    if request.headers.get("host","").lower()!=LOCAL_HOST.lower() or any(request.headers.get(h) for h in ("x-awc-remote","x-forwarded-for","forwarded")):
        raise HTTPException(403,"Local access required")
    from io import BytesIO
    from starlette.background import BackgroundTask
    library = artwork_library()
    fd, filename = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    manifest = {}
    try:
        with zipfile.ZipFile(filename,"w",zipfile.ZIP_STORED) as archive:
            for key,item in library.items():
                path = art_dir/(key+".jpg")
                if not path.is_file():
                    continue
                try:
                    with Image.open(path) as img:
                        img.thumbnail((160,160))
                        buf = BytesIO()
                        img.convert("RGB").save(buf,format="JPEG",quality=85)
                    archive.writestr("artwork/"+key+".jpg",buf.getvalue())
                    manifest[key] = dict(item,file="artwork/"+key+".jpg")
                except (OSError,ValueError):
                    continue
            archive.writestr("artwork.json",json.dumps(manifest,ensure_ascii=False))
        return FileResponse(filename,filename="AWC-Core2-Artwork.zip",media_type="application/zip",background=BackgroundTask(os.unlink,filename))
    except Exception:
        os.unlink(filename)
        raise
@app.get("/artwork")
def artwork_manager():
    return FileResponse(ROOT / "web/static/artwork.html")

@app.get("/api/stations/{name}/now")
def now_playing(name: str):
    idx, sched = station(name)
    now = time.time()
    with schedule_lock:
        upcoming = sched.items_from(now, 40)
    start, end, kind, track, _, _ = upcoming[0]
    seg = segment_at(track, max(0, now-start))
    next_song = None
    for part in track.get("m", []):
        if kind == "song" and now < start + part[0] < end and part[1]:
            next_song = {"title": part[1], "artist": part[2]}
            break
    if next_song is None:
        for a, b, k, tr, _, _ in upcoming[1:]:
            if k != "song":
                continue
            first = segment_at(tr, 0)
            title = (first[1] if first and first[1] else tr.get("title", ""))
            artist = (first[2] if first and first[1] else tr.get("artist", ""))
            if title:
                next_song = {"title": title, "artist": artist}
                break
    title = seg[1] if seg and seg[1] else track.get("title", "")
    artist = seg[2] if seg and seg[1] else track.get("artist", "")
    art = album_art(title, artist) if kind == "song" else None
    if next_song:
        album_art(next_song["title"], next_song["artist"])
    return {"station": name, "kind": kind, "title": title, "artist": artist,
            "start": start, "end": end, "next_song": next_song, "artwork": art}

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
        cmd.extend(["-ss", "%.3f" % offset, "-i", str(path)])
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
                       lo-begin, hi-lo, delay=lo-now, gain=10**(4/20))
    filters.append("".join(labels)+"amix=inputs=%d:duration=longest:normalize=0,alimiter=limit=0.89:level=disabled,apad[out]" % n)
    return cmd+["-filter_complex", ";".join(filters), "-map", "[out]",
                "-t", "%.3f" % duration, "-vn", "-ac", "2", "-ar", "44100",
                "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"]

def mp3_stream(name, stop):
    idx, sched = station(name)
    encoder = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-re", "-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0",
         "-c:a", "libmp3lame", "-b:a", BITRATE+"k", "-write_xing", "0",
         "-id3v2_version", "0", "-flush_packets", "1", "-f", "mp3", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    errors = []
    pcm = queue.Queue(maxsize=30)
    def enqueue(data):
        while not stop.is_set():
            try:
                pcm.put(data, timeout=.1)
                return
            except queue.Full:
                pass
    def feed():
        mixer = None
        cursor = time.time()
        try:
            while not stop.is_set():
                end, plan = programme_window(sched, cursor)
                if not plan or end-cursor < .001:
                    cursor = max(cursor+.001, end)
                    continue
                mixer = subprocess.Popen(mix_command(idx, plan, cursor, end),
                                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
                frames = 0
                target = round((end-cursor)*44100)
                while not stop.is_set() and frames < target:
                    data = mixer.stdout.read(min(17640, (target-frames)*4))
                    if not data:
                        break
                    enqueue(data)
                    frames += len(data)//4
                if stop.is_set():
                    mixer.kill()
                # Drain any rounding surplus before waiting for the mixer.
                if not stop.is_set():
                    mixer.stdout.read()
                rc = mixer.wait()
                mixer.stdout.close()
                mixer = None
                if rc and not stop.is_set():
                    raise RuntimeError("FFmpeg mixer exited with code %d" % rc)
                while frames < target and not stop.is_set():
                    count = min(4410, target-frames)
                    enqueue(bytes(count*4))
                    frames += count
                cursor = end
        except Exception as exc:
            if not stop.is_set():
                errors.append(exc)
        finally:
            if mixer is not None:
                mixer.kill()
                mixer.wait()
                mixer.stdout.close()
            enqueue(None)
    def encode():
        try:
            # Build up to three seconds of PCM before starting playback.
            while not stop.is_set() and pcm.qsize() < 30 and worker.is_alive():
                stop.wait(.02)
            while not stop.is_set():
                try:
                    data = pcm.get(timeout=.1)
                except queue.Empty:
                    if not worker.is_alive():
                        break
                    continue
                if data is None:
                    break
                encoder.stdin.write(data)
        except OSError as exc:
            if not stop.is_set():
                errors.append(exc)
        finally:
            try:
                encoder.stdin.close()
            except OSError:
                pass
    worker = threading.Thread(target=feed, daemon=True)
    worker.start()
    writer = threading.Thread(target=encode, daemon=True)
    writer.start()
    try:
        while not stop.is_set():
            chunk = encoder.stdout.read1(4096)
            if not chunk:
                break
            yield chunk
        if errors:
            raise errors[0]
    finally:
        stop.set()
        if encoder.poll() is None:
            encoder.kill()
        encoder.wait()
        encoder.stdout.close()
        worker.join(timeout=5)
        writer.join(timeout=5)

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

@app.get("/api/podcasts")
def podcasts():
    with state_lock:
        idx = state["index"]
    return [{"name": name, "episodes": [{"id": i, "title": e.get("name") or "Episode %d" % (i+1), "duration": e["sec"]} for i,e in enumerate(show["songs"])]} for name,show in (idx or {}).get("podcasts", {}).items()]

@app.get("/api/podcasts/{name}/{episode}/audio")
def podcast_audio(name: str, episode: int):
    with state_lock:
        idx = state["index"]
    if idx is None:
        raise HTTPException(503, "Indexing")
    songs = idx.get("podcasts", {}).get(name, {}).get("songs", [])
    if not 0 <= episode < len(songs):
        raise HTTPException(404, "Episode not found")
    src = track_file(idx, songs[episode])
    dest = src.with_suffix(".m4a")
    with podcast_lock:
        if not dest.exists() or not dest.stat().st_size:
            tmp = dest.with_suffix(".tmp.m4a")
            try:
                subprocess.run(["ffmpeg","-nostdin","-loglevel","error","-y","-i",str(src),"-vn","-c:a","aac","-b:a","128k","-movflags","+faststart",str(tmp)],check=True)
                os.replace(tmp,dest)
            finally:
                tmp.unlink(missing_ok=True)
    return FileResponse(dest, media_type="audio/mp4")

@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(ROOT / "web/static/manifest.webmanifest", media_type="application/manifest+json")

@app.get("/icon.svg")
def app_icon():
    return FileResponse(ROOT / "web/static/icon.svg", media_type="image/svg+xml")

@app.get("/api/settings")
def settings():
    return {"bitrate": BITRATE, "dj": DJ, "content": CONTENT}

podcast_lock = threading.Lock()
