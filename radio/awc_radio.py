"""AWC Radio - a small web-radio style player for the game's stations.

Pick the game's audio.rpf and its texture pool once; the app finds the stations (names come from the public credits
list, see awc_credits.py), shows them with their logos and plays their songs one after the other, with a short
jingle after every 2-4 songs. The programme is calculated from the UTC clock, so every station keeps "running" in the
background: switching away and back, or a second person with the same clock, lands on the same song at the same second.

It uses the same scan cache as awc_tool.py and needs ffplay (next to the program or in PATH).
"""
import bisect
import hashlib
import json
import math
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import zlib
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "extractor")]   # the shared modules live with the extractor
import awc_album  # noqa: E402
import awc_config as gamecfg  # noqa: E402
import awc_core as core  # noqa: E402
import awc_credits as credits  # noqa: E402
import awc_dj  # noqa: E402
import awc_gamedata as gamedata  # noqa: E402
import awc_i18n  # noqa: E402
import awc_icons as icons  # noqa: E402
import awc_texts as texts  # noqa: E402

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
SONG_MIN, SONG_MAX = 60.0, 1500.0          # seconds: shorter is not a song, longer is a mix
JINGLE_MIN, JINGLE_MAX = 2.0, 25.0         # unnamed short streams of a station are used as jingles
MIN_SONGS = 5                              # a station needs at least this many songs to be listed
INDEX_VERSION = 14

BG, PANEL, CARD, CARD_SEL, FG, DIM, ACCENT = "#14161c", "#1c1f27", "#232733", "#2f3550", "#eceef4", "#8b91a3", "#ff5a36"

STR = {
    "en": {
        "title": "AWC Radio", "setup_title": "Choose your game files",
        "setup_intro": "Select the game's audio.rpf and its texture file (data\\ttdb\\ui\\ttdb_ui.ttpf). The first start reads the "
                       "archive once; later starts are instant.",
        "lbl_rpf": "audio.rpf", "lbl_pool": "Texture file (ttdb_ui.ttpf)", "btn_browse": "Browse…", "btn_start": "Start radio",
        "btn_settings": "Files…", "btn_rescan": "Re-read", "btn_android": "Android packs…", "st_scan": "Reading the archive … (the first time takes several minutes)",
        "st_credits": "Loading the song list …", "st_names": "Reading names from the game config …",
        "st_build": "Building the stations …", "st_beds": "Listening into the DJ recordings …", "st_titles": "Reading the titles from data.rpf …", "st_icons": "Reading the station logos …", "st_ready": "{n} stations",
        "err_rpf": "Please choose an existing audio.rpf.", "err_scan": "No music streams found in that file.",
        "err_ffplay": "ffplay was not found. Put ffplay.exe next to the program or into your PATH.",
        "err_credits": "The credits page could not be downloaded:\n{e}\n\nPick the page saved from your browser instead?",
        "err_html": "HTML page", "err_generic": "Something went wrong:\n{e}", "err_nostations": "No station with songs was found.",
        "kind_song": "SONG", "kind_jingle": "JINGLE", "kind_ad": "ADVERT", "advert": "Advert", "btn_content": "Content", "c_ads": "Adverts", "c_news": "News", "c_dj": "DJ talk between the songs",
        "c_talk": "Talk and show segments", "c_jingles": "Jingles and station idents", "chk_dj": "DJ over songs",
        "kind_news": "NEWS", "news": "News", "kind_talk": "TALK", "talk": "Talk",
        "dj_needs_ffmpeg": "The DJ needs FFmpeg (ffmpeg.exe) next to the program or in your PATH.",
        "lbl_fade": "Crossfade (clubs, mixes)", "fade_off": "Off", "fade_s": "{n} s",
        "st_fade_note": "The programme now differs from devices with another crossfade setting (standard: 3 s).", "btn_play": "▶ Play (live)", "btn_pause": "⏸ Pause",
        "sync_hint": "Live: the stations run by the clock, everyone with the same time and time zone hears the same.", "lbl_volume": "Volume", "n_songs": "{n} songs", "n_shows": "{n} shows", "n_segments": "{n} segments", "kind_show": "SHOW", "st_loading": "Loading …",
        "lbl_next": "Up next:", "tune": "Tune in to a station on the left", "no_logo_hint": "",
        "pod_header": "Podcasts", "pod_episode": "Episode {n}", "pod_summary": "{n} episodes · {m} min", "pod_heard": "heard",
        "pod_resume": "continue at {t}", "pod_back": "−{n} s", "pod_forward": "+{n} s", "pod_error": "cannot be played",
        "pod_count": "{n} episodes",
    },
    "de": {
        "title": "AWC Radio", "setup_title": "Spieldateien wählen",
        "setup_intro": "Wähle die audio.rpf des Spiels und seine Textur-Datei (data\\ttdb\\ui\\ttdb_ui.ttpf). Beim ersten Start wird "
                       "das Archiv einmal eingelesen; spätere Starts gehen sofort.",
        "lbl_rpf": "audio.rpf", "lbl_pool": "Textur-Datei (ttdb_ui.ttpf)", "btn_browse": "Durchsuchen…", "btn_start": "Radio starten",
        "btn_settings": "Dateien…", "btn_rescan": "Neu einlesen", "btn_android": "Android-Packs…", "st_scan": "Lese das Archiv … (beim ersten Mal dauert das mehrere Minuten)",
        "st_credits": "Lade die Songliste …", "st_names": "Lese Namen aus der Spiel-Konfiguration …",
        "st_build": "Baue die Sender …", "st_beds": "Höre in die DJ-Aufnahmen hinein …", "st_titles": "Lese die Titel aus data.rpf …", "st_icons": "Lese die Sender-Logos …", "st_ready": "{n} Sender",
        "err_rpf": "Bitte eine vorhandene audio.rpf wählen.", "err_scan": "In dieser Datei wurden keine Musik-Streams gefunden.",
        "err_ffplay": "ffplay wurde nicht gefunden. ffplay.exe neben das Programm legen oder in den PATH aufnehmen.",
        "err_credits": "Die Credits-Seite konnte nicht geladen werden:\n{e}\n\nStattdessen die im Browser gespeicherte Seite wählen?",
        "err_html": "HTML-Seite", "err_generic": "Es ist ein Fehler aufgetreten:\n{e}", "err_nostations": "Es wurde kein Sender mit Songs gefunden.",
        "kind_song": "SONG", "kind_jingle": "JINGLE", "kind_ad": "WERBUNG", "advert": "Werbung", "btn_content": "Inhalte", "c_ads": "Werbung", "c_news": "Nachrichten", "c_dj": "DJ-Beiträge zwischen den Songs",
        "c_talk": "Talk und Sendungsteile", "c_jingles": "Jingles und Senderkennungen", "chk_dj": "DJ über Songs",
        "kind_news": "NACHRICHTEN", "news": "Nachrichten", "kind_talk": "TALK", "talk": "Talk",
        "dj_needs_ffmpeg": "Für den DJ wird FFmpeg (ffmpeg.exe) neben dem Programm oder im PATH gebraucht.",
        "lbl_fade": "Überblenden (Clubs, Mixe)", "fade_off": "Aus", "fade_s": "{n} s",
        "st_fade_note": "Das Programm weicht jetzt von Geräten mit anderer Überblendung ab (Standard: 3 s).", "btn_play": "▶ Abspielen (live)", "btn_pause": "⏸ Pause",
        "sync_hint": "Live: Die Sender laufen nach der Uhr, alle mit derselben Zeit und Zeitzone hören dasselbe.", "lbl_volume": "Lautstärke", "n_songs": "{n} Songs", "n_shows": "{n} Sendungen", "n_segments": "{n} Beiträge", "kind_show": "SENDUNG", "st_loading": "Lade …",
        "lbl_next": "Als Nächstes:", "tune": "Links einen Sender wählen", "no_logo_hint": "",
        "pod_header": "Podcasts", "pod_episode": "Folge {n}", "pod_summary": "{n} Folgen · {m} Min.", "pod_heard": "gehört",
        "pod_resume": "weiter bei {t}", "pod_back": "−{n} s", "pod_forward": "+{n} s", "pod_error": "lässt sich nicht abspielen",
        "pod_count": "{n} Folgen",
    },
}


def t(key, **kw):
    lang = awc_i18n.get_lang()
    s = STR.get(lang, STR["en"]).get(key) or STR["en"].get(key, key)
    return s.format(**kw) if kw else s


# ------------------------------------------------------------------------------------------------ files
def appdata():
    d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "AWC-Tool")
    os.makedirs(d, exist_ok=True)
    return d


def settings_path():
    return os.path.join(appdata(), "radio.json")


def load_settings():
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(d):
    try:
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
    except OSError:
        pass


def index_path():
    return os.path.join(appdata(), "radio_index.json")


def credits_cache_path():
    return os.path.join(appdata(), "radio_credits.json")


def cache_dir():
    d = os.path.join(appdata(), "radio_cache")
    os.makedirs(d, exist_ok=True)
    return d


def find_tool(name):
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(sys.executable))
        if getattr(sys, "_MEIPASS", None):
            dirs.append(sys._MEIPASS)
    dirs.append(os.path.dirname(os.path.abspath(__file__)))
    for d in dirs:
        for cand in (os.path.join(d, name + ".exe"), os.path.join(d, name)):
            if os.path.isfile(cand):
                return cand
    from shutil import which
    return which(name)


def fingerprint(rpf, pool):
    st = os.stat(rpf)
    return {"v": INDEX_VERSION, "rpf": os.path.abspath(rpf).lower(), "size": st.st_size, "mtime": int(st.st_mtime),
            "pool": os.path.abspath(pool).lower() if pool else ""}


def load_index(rpf, pool):
    try:
        with open(index_path(), "r", encoding="utf-8") as f:
            idx = json.load(f)
        if idx.get("fp") == fingerprint(rpf, pool) and idx.get("stations"):
            return idx
    except (OSError, ValueError, KeyError):
        pass
    return None


def extract_track(index, rpf, tr_, dst):
    """Cut one item of a station out of the archive (a talk show: all its parts into one file; a recording of several
    layers mixed together, a part of a mix cut out - both with FFmpeg)."""
    def ref(base):
        return core.AwcRef(dict(index["items"][str(base)], path=rpf))
    parts = tr_.get("parts")
    if tr_.get("stems") or tr_.get("cut"):
        _render(ref(tr_["base"]), tr_, dst)
    elif parts:
        core.join_streams([(ref(b).open(), i) for b, i in parts], dst)
    else:
        ref(tr_["base"]).extract({tr_["idx"]: dst})


def _render(ref, tr_, dst):
    """extract_track() for a recording that FFmpeg makes: the layers tr_["stems"] added up, or the seconds tr_["cut"]
    of a mix. Raises OSError."""
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        raise OSError(t("dj_needs_ffmpeg"))
    wanted = tr_.get("stems") or [tr_["idx"]]
    parts = {i: "%s.%d.opus" % (dst, i) for i in wanted}
    try:
        ref.extract(parts)
        cmd = [ffmpeg, "-v", "error", "-y"]
        if tr_.get("cut"):
            cmd += ["-ss", "%.2f" % tr_["cut"][0], "-t", "%.2f" % (tr_["cut"][1] - tr_["cut"][0])]
        for path in parts.values():
            cmd += ["-i", path]
        if len(parts) > 1:
            cmd += ["-filter_complex", "amix=inputs=%d:duration=longest:normalize=0,alimiter=limit=0.89:level=disabled" % len(parts)]
        r = subprocess.run(cmd + ["-c:a", "libopus", "-b:a", "160k", "-f", "ogg", dst], capture_output=True,
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=600)
        if r.returncode != 0 or not os.path.isfile(dst):
            raise OSError("ffmpeg: " + r.stderr.decode("utf-8", "replace")[-300:])
    finally:
        for path in parts.values():
            try:
                os.remove(path)
            except OSError:
                pass


# ------------------------------------------------------------------------------------------------ model
def sort_key(name):
    if name == CLUB_STATION:
        return "￿"                      # the collection of club mixes goes last
    n = name.lower()
    return n[4:] if n.startswith("the ") else n


def build_stations(items, found, cfg_names, unnamed_out=None):
    """-> {station: {"songs": [track], "jingles": [track]}}.

    items      scan items (dicts with "base", "streams", optional "cues")
    found      {stream id: (title, artist, station)} from the credits list
    cfg_names  {stream id: title} from the game config (no artist, no station)
    unnamed_out  optional dict that collects {station: [track]} of song-length streams that have no name at all

    A track is {"base", "idx", "sid", "title", "artist", "sec"}. The stations lie in the archive one after the
    other: an AWC without a station inherits it when the nearest AWCs before and after it agree."""
    items = sorted(items, key=lambda x: x["base"])
    runs, group_votes = [], {}
    for it in items:
        votes = {}
        for s in it["streams"]:
            hit = found.get(s["id"])
            if hit and credits.is_radio(hit[2]):
                votes[hit[2]] = votes.get(hit[2], 0) + 1
        if votes:
            st = max(votes, key=votes.get)
            runs.append((it["base"], st))
            gv = group_votes.setdefault(it.get("g", 0), {})
            gv[st] = gv.get(st, 0) + 1
    bases = [b for b, _s in runs]
    # a group (cluster of AWCs in the archive) that is almost entirely one station belongs to that station
    group_station = {}
    for g, gv in group_votes.items():
        total = sum(gv.values())
        top = max(gv, key=gv.get)
        if g and total >= 3 and gv[top] >= 0.85 * total:
            group_station[g] = top

    def inferred(it):
        import bisect
        i = bisect.bisect_left(bases, it["base"])
        before = runs[i - 1][1] if i > 0 else None
        after = runs[i][1] if i < len(runs) else None
        if before and before == after:
            return before
        return group_station.get(it.get("g", 0))

    out = {}

    def add(station, kind, it, s, title="", artist=""):
        out.setdefault(station, {"songs": [], "jingles": []})[kind].append(
            {"base": it["base"], "idx": s["i"], "sid": s["id"], "title": title, "artist": artist, "sec": s["sec"]})

    for it in items:
        if it.get("cues"):
            continue                                   # a whole station mix, not a single track
        st_inf = None
        for s in it["streams"]:
            sec, sid = s["sec"], s["id"]
            hit = found.get(sid)
            if hit and credits.is_radio(hit[2]):
                if SONG_MIN <= sec <= SONG_MAX:
                    add(hit[2], "songs", it, s, hit[0], hit[1])
                continue
            is_song, is_jingle = SONG_MIN <= sec <= SONG_MAX, JINGLE_MIN <= sec <= JINGLE_MAX
            if sid in found or not (is_song or is_jingle):
                continue
            if st_inf is None:
                st_inf = inferred(it) or ""
            if not st_inf:
                continue
            if sid in cfg_names and is_song:
                add(st_inf, "songs", it, s, cfg_names[sid], "")
            elif is_jingle:
                if sid in cfg_names:                         # a short stream that has a config name is a sound effect
                    continue                                 # ("zombieb_vehicle_effect_chain..."), station jingles have none
                add(st_inf, "jingles", it, s)
            elif unnamed_out is not None:                    # song-length stream without any name
                unnamed_out.setdefault(st_inf, []).append(
                    {"base": it["base"], "idx": s["i"], "sid": sid, "title": "", "artist": "", "sec": sec})
    return {k: v for k, v in sorted(out.items(), key=lambda kv: sort_key(kv[0])) if len(v["songs"]) >= MIN_SONGS}


def _name_key(name):
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


CLUB_STATION = "Clubmixes"


def build_mixes(items, found, songs=()):
    """-> {station: {"songs": [track], "jingles": []}} for the long continuous mixes of the archive.

    A mix is a stream far longer than a song whose bank carries `trackid` markers. Its station is found in two ways:
    1. The markers hold the track numbers of single songs of the same station: the station that owns most of the songs it
       links to (at least 5 links, at least 70 % one station). It is listed as "<station> (Mix)" next to its songs.
    2. Without such links: the archive stores the folders of the stations in the alphabetical order of their internal names,
       so a mix that lies between the songs of station A and the songs of station B belongs to the station without any
       single song whose name sorts between A and B (there must be exactly one). That station IS the mix and keeps its name.
       A mix whose neighbours leave no such station (club music, DJ sets of venues) is not a radio station: all of these
       are played together as the station "Clubmixes" (to be removed again when they can be assigned).
    The mix is played like a one-song station: the programme just plays it over and over."""
    by_tid = {}
    for it in items:
        if it.get("tid") is not None and not it.get("cues"):
            by_tid.setdefault(it["tid"], []).append(it)
    ordered = sorted(items, key=lambda x: x["base"])
    owner = {}                                           # base -> station of the single songs found in that bank
    for it in ordered:
        for s in it["streams"]:
            if s["id"] in found and credits.is_radio(found[s["id"]][2]):
                owner[it["base"]] = found[s["id"]][2]
                break
    singles = {}
    for st in owner.values():
        singles[st] = singles.get(st, 0) + 1
    music = {}                                           # stations whose credits list real songs (talk radio lists shows)
    for s in songs:
        if credits.is_radio(s["station"]) and s["section"].upper() == "SONGS":
            music[s["station"]] = music.get(s["station"], 0) + 1
    no_songs = sorted(st for st, n in music.items() if n >= 5 and singles.get(st, 0) < 3)
    out = {}

    def add(name, it, s, title="Mix"):
        tr_ = {"base": it["base"], "idx": s["i"], "sid": s["id"], "title": title, "artist": "", "sec": s["sec"]}
        segs = []                                        # [[start second, title, artist]] from the markers of the mix
        for t_, v in it.get("cues") or ():
            hit = None
            for o in by_tid.get(v, ()):
                hit = next((found[x["id"]] for x in o["streams"] if x["id"] in found), None)
                if hit:
                    break
            segs.append([round(t_, 1), hit[0] if hit else "", hit[1] if hit else ""])
        if any(x[1] for x in segs):
            tr_["m"] = segs
        out.setdefault(name, {"songs": [], "jingles": []})["songs"].append(tr_)

    for pos, it in enumerate(ordered):
        if not it.get("cues"):
            continue
        votes = {}
        for _t, v in it["cues"]:
            for o in by_tid.get(v, ()):
                hit = next((found[s["id"]] for s in o["streams"] if s["id"] in found), None)
                if hit and credits.is_radio(hit[2]):
                    votes[hit[2]] = votes.get(hit[2], 0) + 1
                    break
        total = sum(votes.values())
        name = None
        if total >= 5 and max(votes.values()) >= 0.7 * total:
            name = max(votes, key=votes.get) + " (Mix)"
        else:
            before = next((owner[o["base"]] for o in reversed(ordered[:pos]) if o["base"] in owner), None)
            after = next((owner[o["base"]] for o in ordered[pos + 1:] if o["base"] in owner), None)
            if before and after:
                lo, hi = sorted((_name_key(before), _name_key(after)))
                between = [st for st in no_songs if lo < _name_key(st) < hi]
                if len(between) == 1:
                    name = between[0]
        if not name:
            continue
        for s in it["streams"]:
            if s["sec"] > SONG_MAX:
                add(name, it, s)
    out = {k: v for k, v in out.items() if len(v["songs"]) == 1}     # several candidates for one station: unclear, leave out
    kept = {tr_["base"] for v in out.values() for tr_ in v["songs"]}
    n = 0
    for it in ordered:                                   # every other long mix: club and DJ-venue music, played as one station
        longs = [s for s in it["streams"] if s["sec"] > SONG_MAX]
        if longs and it["base"] not in kept:
            n += 1                                       # (a bank with several long streams: the others are quiet crowd layers)
            add(CLUB_STATION, it, longs[0], "Club mix %d" % n)
    if CLUB_STATION in out:
        out[CLUB_STATION]["songs"].sort(key=lambda x: x["base"])
    return out


UNKNOWN_TRACK = "Unknown track"


def fill_unnamed(stations, unnamed, songs, found):
    """A station whose credits list more songs than were found may hold song-length streams without any name. When there are no
    more of them than songs still missing, they are very probably those songs: add them as "Unknown track". (With more
    candidates than missing songs some of them are something else, so none is added.)"""
    taken = {(v[0], v[1], v[2]) for v in found.values()}
    added = {}
    for name, st in stations.items():
        cand = unnamed.get(name, [])
        missing = sum(1 for s in songs if s["station"] == name and s["section"].upper() == "SONGS"
                      and (credits.display_title(s["title"]), s["artist"], s["station"]) not in taken)
        if cand and len(cand) <= missing:
            for tr_ in cand:
                st["songs"].append(dict(tr_, title=UNKNOWN_TRACK))
            added[name] = len(cand)
    return added


def make_index(rpf, pool, get_songs, status=None, progress=None):
    """Scan the archive, match the songs of the credits list, build the stations and save the index (radio_index.json).

    get_songs() is called after the scan and returns the credits songs. Returns the index, or None when nothing was found."""
    res = core.scan_files([rpf], progress=(lambda f, n, nt: progress(f * 0.7)) if progress else None)
    items = [it for c in res for it in c["items"]]
    if not items:
        return None
    if status:
        status(t("st_credits"), 0.72)
    songs = get_songs()
    ids = {s["id"] for it in items for s in it["streams"]}
    found = credits.match(songs, ids)
    if status:
        status(t("st_names"), 0.8)
    try:
        cfg_names = gamecfg.read_names(rpf, ids)[0]
    except (OSError, ValueError):
        cfg_names = {}
    # streams whose game name only resembles a title of the credits list ("IM_IN_LUV" ~ "I'm N Luv (Wit a Stripper)")
    similar = credits.match_names(songs, found, {i: n for i, n in cfg_names.items() if i not in found})
    found.update(similar)
    matched = dict(found)
    # the stations as the game config itself defines them: complete track lists, also for talk radio
    try:
        game = gamedata.read(rpf)
    except (OSError, ValueError):
        game = {}
    for sid, (title, artists, station) in gamedata.hits(game).items():
        if sid not in found:                                      # for the parts of the long mixes
            hit = credits.lookup(songs, station, title, artists)
            found[sid] = (hit or (gamecfg.pretty(title), ", ".join(gamecfg.pretty(a) for a in artists))) + (station,)
    try:
        rules = gamedata.read_rules(rpf)                      # what the game lets follow what, and how often
    except (OSError, ValueError):
        rules = {}
    try:
        news = gamedata.build_news(items, gamedata.read_news(rpf))
    except (OSError, ValueError):
        news = []
    if status:
        status(t("st_build"), 0.88)
    unnamed = {}
    stations = build_stations(items, found, cfg_names, unnamed)
    fill_unnamed(stations, unnamed, songs, found)
    stations.update(build_mixes(items, found, songs))
    for name, st in gamedata.build(items, game, songs, matched, cfg_names, rules).items():
        if not news:
            st.pop("news", None)
        if not st["jingles"] and "rules" not in st and name in stations:  # a bare playlist: keep the jingles found for it
            songs_ = {_tkey(x) for x in st["songs"]}
            st["jingles"] = [j for j in stations[name]["jingles"] if _tkey(j) not in songs_]
        stations[name] = st                                       # replaces the guessed station of the same name
    for name, st in stations.items():
        for tr_ in st["songs"]:                                   # "Six Four Five" -> "6:45" with its artist
            if not tr_["artist"] and not tr_.get("parts") and not st.get("rules", {}).get("talkradio"):
                hit = credits.lookup(songs, name, tr_["title"], own_only=True)
                if hit:
                    tr_["title"], tr_["artist"] = hit
    add_venues(stations, items, rpf, songs, matched, cfg_names)
    adverts = gamedata.advert_keys(items, game)
    for st in stations.values():                        # an advert is never a jingle: it only runs when adverts are wanted
        if "rules" not in st:
            st["jingles"] = [j for j in st["jingles"] if (j["base"], j["idx"]) not in adverts]
    stations = dict(sorted(stations.items(), key=lambda kv: sort_key(kv[0])))
    try:
        spoken = awc_dj.collect(rpf, items, stations)            # the DJ's windows and lines of every song
    except (OSError, ValueError, struct.error):
        spoken = {}
    try:
        awc_dj.cues(rpf, stations)                               # where every song is cued in
    except (OSError, ValueError, struct.error):
        pass
    if status:
        status(t("st_beds"), 0.9)
    awc_dj.beds(rpf, stations, {it["base"]: it for it in items}, find_tool("ffmpeg"))
    try:
        podcasts = gamedata.build_podcasts(items, gamedata.read_podcasts(rpf))     # for the app: played on demand
    except (OSError, ValueError):
        podcasts = {}
    if status:
        status(t("st_titles"), 0.93)
    numbered = {it["base"]: it.get("tid") for it in items}
    runs = [[numbered.get(e["base"]) for e in st["songs"]] for st in podcasts.values()]
    wanted = texts.numbers(items) | {n for run in runs for n in texts.fill_gaps(run) if n is not None}
    shows = {}
    titles = texts.read_titles(rpf, wanted, shows)
    apply_titles(stations, podcasts, items, titles, songs, shows)
    awc_album.name_known(stations)                               # recognised by their sound
    album = awc_album.find(items, stations)                      # the soundtrack, for a playlist
    if not any(st.get("news") for st in stations.values()):
        news = []
    used = {}
    for tr_ in [x for st in list(stations.values()) + list(podcasts.values()) for x in gamedata.tracks_of(st)] + news + album:
        for base, _idx in tr_.get("parts") or [(tr_["base"], tr_["idx"])]:
            used[base] = None
    by_base = {it["base"]: it for it in items}
    by_base.update(spoken)                                      # the speech containers of the DJ lines
    for st in stations.values():
        for line in awc_dj.station_lines(st):
            used[line["base"]] = None
    idx = {"fp": fingerprint(rpf, pool), "stations": stations, "podcasts": podcasts, "news": news, "album": album,
           "items": {str(b): by_base[b] for b in used}}
    with open(index_path(), "w", encoding="utf-8") as f:
        json.dump(idx, f)
    return idx


def apply_titles(stations, podcasts, items, titles, songs, shows=None):
    """Names from the game's own table (awc_texts), by the track number every recording carries: songs that had no or
    another name, the parts of the long mixes, the titles of the podcast episodes and (from `shows`) the name of a show
    its spoken parts belong to. `titles` is empty when there is no data.rpf next to the archive: then everything stays
    as it is."""
    if not titles:
        return
    by_base = {it["base"]: it for it in items}
    for name, st in stations.items():
        station = name[:-6] if name.endswith(" (Mix)") else name
        for tr_ in st.get("talk", []) + st["jingles"]:
            number = (by_base.get(tr_["base"]) or {}).get("tid")
            if shows and number in shows:
                tr_["title"] = texts.pretty(shows[number])
        if st.get("rules", {}).get("talkradio"):                  # its items are shows, named after their streams
            continue
        for tr_ in st["songs"]:
            it = by_base.get(tr_["base"])
            if tr_.get("parts") or not it:
                continue
            if it.get("cues"):                                    # a long mix: every marker starts a part
                old, parts = tr_.get("m") or [], []
                for k, (at, number) in enumerate(it["cues"]):
                    if number in titles:
                        title, artist = texts.spell(*titles[number], songs, station)
                    else:
                        title, artist = old[k][1:3] if k < len(old) else ("", "")
                    parts.append([round(at, 1), title, artist])
                if any(p[1] for p in parts):
                    tr_["m"] = parts
            elif it.get("tid") in titles and not texts.agrees(tr_["title"], tr_["artist"], titles[it["tid"]]):
                tr_["title"], tr_["artist"] = texts.spell(*titles[it["tid"]], songs, station)
    for st in podcasts.values():
        run = texts.fill_gaps([(by_base.get(e["base"]) or {}).get("tid") for e in st["songs"]])
        for episode, number in zip(st["songs"], run):
            if number in titles:
                episode["name"] = titles[number][0]


def add_venues(stations, items, rpf, songs, found, cfg_names):
    """Places of the game that play their own music become stations: venues with a playlist, and clubs whose DJ set
    leaves the "Clubmixes" collection (that keeps the mixes which belong to no known venue)."""
    try:
        sets, playlists = gamedata.read_venues(rpf)
        clubs = gamedata.build_clubs(items, sets)
        stations.update(gamedata.build_venues(items, playlists, songs, found, cfg_names))
    except (OSError, ValueError):
        return
    used = {base for st in clubs.values() for tr_ in st["songs"] for base, _i in tr_.get("parts") or [(tr_["base"], tr_["idx"])]}
    mixes = stations.get(CLUB_STATION)
    if mixes:
        left = [tr_ for tr_ in mixes["songs"] if tr_["base"] not in used]
        for n, tr_ in enumerate(left, 1):
            tr_["title"] = "Club mix %d" % n if len(left) > 1 else "Club mix"
        if left:
            mixes["songs"] = left
        else:
            del stations[CLUB_STATION]
    stations.update(clubs)


DAY = 86400.0
FADE = 3.0                                 # standard seconds of crossfade between two songs
FADE_MAX = 12
LEAD = 0.4                                 # ffplay needs about this long to start; its start position is moved on by it
SHAPES = ("tri", "qsin")                   # the shapes of a fade as FFmpeg names them: straight, equal power
TAIL = 0.3                                 # an item that is not faded out may play this much longer than scheduled
AD_AFTER = (540, 361)                      # with adverts: a break after 9 to 15 minutes of programme (540 + 0..360 s)
DJ_INTRO, DJ_OUTRO, DJ_BEFORE_ADS = 50, 35, 70      # per cent: how often the DJ talks over an intro, an outro, into a break
SPOKEN_FROM = 25.0                         # seconds: a station's longer spoken item is talk, a shorter one a jingle
# what the listener can switch on and off, besides the songs and a talk station's shows: adverts, the news, the DJ's
# talk between the songs, other talk and show segments, jingles and station idents
CONTENT = ("ads", "news", "dj", "talk", "jingles")
CONTENT_DEFAULT = ("dj", "jingles")


def content_set(value):
    """The switched-on kinds: True everything, False or None the standard ones, else the names given."""
    if value is True:
        return frozenset(CONTENT)
    if value is False or value is None:
        return frozenset(CONTENT_DEFAULT)
    return frozenset(x for x in value if x in CONTENT)


def hour_of(now):
    """The hour of the listener's clock as the game's curves are asked for it: 0.0, 0.5, ... 23.5. The game's clock runs
    much faster than a real one, so an item there carries it across a change from day to night; counting in
    half hours keeps that: a night show does not open shortly before its time only to close again."""
    lt = time.localtime(math.floor(now))
    return lt.tm_hour + (0.5 if lt.tm_min >= 30 else 0.0)


def curve_value(curve, x):
    """A curve of the rules at x: straight lines between [x, y] points, or a table by the number of items in a row."""
    if not isinstance(curve[0], list):
        return curve[min(max(int(x) - 1, 0), len(curve) - 1)]
    if x < curve[0][0]:
        return curve[0][1]
    i = 0
    while i + 1 < len(curve) and curve[i + 1][0] <= x:
        i += 1
    if i + 1 == len(curve):
        return curve[i][1]
    (x0, y0), (x1, y1) = curve[i], curve[i + 1]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def segment_at(tr_, pos):
    """[start, title, artist] of the part of a mix that is playing at `pos` seconds (tr_["m"] holds the parts), or None."""
    m = tr_.get("m")
    if not m:
        return None
    k = bisect.bisect_right([x[0] for x in m], pos) - 1
    return m[k] if k >= 0 else None


def _tkey(tr_):
    """Identifies an item; the same as awc_android.track_key (the programme hashes it)."""
    parts = tr_.get("parts")
    if tr_.get("cut"):
        return "%s_%sc%d" % (tr_["base"], tr_["idx"], int(tr_["cut"][0]))
    if tr_.get("stems"):
        return "%s_%ss%d" % (tr_["base"], tr_["idx"], len(tr_["stems"]))
    return "%s_%s" % (tr_["base"], tr_["idx"]) + ("x%d" % len(parts) if parts else "")


def _is_show(tr_):
    """A talk show (made of parts); a club's DJ set is made of parts too but counts as music."""
    return bool(tr_.get("parts")) and not tr_.get("set")


def _h(*parts):
    """Stable hash (identical on every machine and Python version, unlike random.shuffle)."""
    return zlib.crc32("|".join(str(p) for p in parts).encode("utf-8"))


def is_speech(category, kind):
    """Somebody talks: such an item is never faded. (The other items besides songs are jingles.)"""
    return kind in ("talk", "ad", "news") or category.startswith(("dj_banter", "show"))


def switches_of(category, kind, talk_radio):
    """The switches an item of this category and kind needs, all of them, to go on air; "song" and "show" are always on.
    The DJ coming back from the adverts or the news needs the break itself as well."""
    if kind == "song":
        return frozenset(("song",))
    if kind in ("ad", "news"):
        return frozenset(("ads" if kind == "ad" else "news",))
    if talk_radio:
        return frozenset(("jingles" if kind == "jingle" else "show",))
    if category.startswith("dj_banter"):
        after = {"dj_banter_from_ads": ("ads",), "dj_banter_from_news": ("news",)}.get(category, ())
        return frozenset(("dj",) + after)
    if category.startswith("show") or category in gamedata.TALK_CATEGORIES:
        return frozenset(("talk",))
    return frozenset(("talk" if kind == "talk" else "jingles",))


class Schedule:
    """The programme of one station, a pure function of the station and the UTC clock.

    Every UTC day gets its own order and is filled from 00:00:00 UTC onwards. Nothing random and nothing stored (every
    choice is a hash of station, day and place), so everybody with the same game files and the same clock hears the same
    item at the same second. The item that would cross midnight UTC is cut there.

    How one item hands over to the next is part of the timetable. A station with the game's rules is mixed as they say
    (rules["mix"]): by the category of the item that ends and of the one that follows, the first may end a little early
    and fade out, the second starts before that end and may fade in; a song is placed so that its cue-in point
    (track["cue"]) lands there. Where the rules say nothing, items follow back to back.

    Only music is faded, though: what is spoken (see is_speech) never is, and a jingle only where it meets something
    spoken or another jingle, not where it meets a song. Instead a song steps back while something is said over it, see
    talk_over().

    Without them two songs that follow each other overlap by `fade` seconds (a crossfade like on a real radio) and
    nothing else does; listeners then hear the same only with the same crossfade (standard: FADE).

    Under the DJ's talk a station with music beds (station["beds"]) plays one of them, see bed_of().

    A station that brings the game's programme rules (station["rules"]) follows them: after an item of one category the
    rules weigh which categories may follow, by the seconds since each was last heard, the number of its items in a row
    and the hour of the listener's clock (so a night show comes at night; listeners in one time zone hear the same). Which
    kinds of content go on air is the listener's choice (content, see CONTENT): the rules run as in the game, and what is
    switched off is stepped over. Songs, and a talk station's shows, always play. Listeners hear the same only with the
    same switches.

    A station without rules plays its songs in a shuffled order with a jingle after every 2-4 songs.

    With the DJ switched on, voices() says what the DJ says over an item; that changes nothing in the timetable."""

    def __init__(self, name, station, content=False, fade=FADE, news=()):
        self.name = name
        self.fade = min(float(FADE_MAX), max(0.0, float(fade)))
        self.talk = any(_is_show(s) for s in station["songs"])        # items that are whole talk shows: never faded
        self.songs = sorted(station["songs"], key=_tkey)
        self.jingles = sorted(station["jingles"], key=_tkey)
        self.rules = station.get("rules")
        self.mix = self.rules.get("mix") if self.rules else None      # the game's mixing; None: the plain crossfade
        self.beds = sorted(station.get("beds", []), key=_tkey)
        self.on = content_set(content)
        self.toads = station.get("toads", [])                          # the DJ's lines into an advert break
        self.tonews = station.get("tonews", [])                        # ... and into the news
        self.pools, self.hosted = {}, set()
        self.enabled = self.on | {"song", "show"}                      # what may go on air
        if self.rules:
            self._gather(station, news)
        self.days = {}

    def _gather(self, station, news):
        """The recordings of every category of the rules, each with its kind and with the switch it belongs to."""
        tracks = {_tkey(x): x for x in station["songs"] + station["jingles"] + station.get("talk", []) + station.get("ads", [])}
        flagged = set(self.rules.get("ads", ()))
        talk_radio = bool(self.rules.get("talkradio"))

        def entry(category, key):
            if category in gamedata.SONG_CATEGORIES:
                kind = "song"
            elif category == "advert" or key in flagged:
                kind = "ad"
            else:
                kind = "talk" if category in gamedata.TALK_CATEGORIES or tracks[key]["sec"] > SPOKEN_FROM else "jingle"
            return kind, tracks[key], switches_of(category, kind, talk_radio)
        pools = {c: [entry(c, k) for k in keys if k in tracks] for c, keys in self.rules["pools"].items()}
        if station.get("news") and news:
            pools["news"] = [("news", x, frozenset(("news",))) for x in sorted(news, key=_tkey)]
        self.pools = {c: v for c, v in pools.items() if v}
        if "showintro_night" in self.rules["pools"]:           # a night show has a host of its own
            self.hosted = {k for c in ("music_night", "music_night2") for k in self.rules["pools"].get(c, ())}

    def _day(self, d):
        """-> ([start offsets], [[kind, track, fade_in, fade_out, seconds on air]]) of UTC day number d.

        A fade is None or (second in the item at which it starts, its length, its shape)."""
        if d not in self.days:
            made = self._ruled_day(d) if self.rules else self._plain_day(d)
            if len(self.days) > 4:
                self.days.clear()
            self.days[d] = made
        return self.days[d]

    def _mixing(self, d, step, before, after):
        """[weight, move, fade, shape, start, fade, shape] as the rules mix the end of a `before` item with an `after`
        item (milliseconds, see gamedata.parse_rules), or None."""
        by = self.mix.get(before, {})
        found = by.get(after) or by.get("")
        if not found:
            return None
        at = _h(self.name, d, "mix", step) % sum(v[0] for v in found)
        for values in found:
            at -= values[0]
            if at < 0:
                break
        return values

    def _add(self, starts, entries, kind, tr_, d=0, before_category="", category=""):
        """Put an item behind the last one; False when it would start after the day."""
        sec = max(1.0, tr_["sec"])
        start, fade_in = 0.0, None
        if entries:
            last = entries[-1]
            before = last[4]
            began = starts[-1]
            start, play, fade_out = began + before, before, None
            if self.mix is not None:
                values = self._mixing(d, len(entries), before_category, category)
                if values:
                    _w, move, out_ms, out_shape, start_ms, in_ms, in_shape = values
                    end = began + max(0.5 * before, before + move / 1000.0)
                    cue = tr_.get("cue", 0.0)
                    # never more than half of either item over the other, and never before the one that ends has begun
                    lead = max(start_ms / 1000.0, -0.5 * sec)
                    start = max(end + lead - cue, began + max(1.0, 0.5 * before))
                    play = end - began
                    if out_ms > 0:
                        length = min(out_ms / 1000.0, play)
                        fade_out = (play - length, length, out_shape)
                    if in_ms > 0:
                        fade_in = (cue, min(in_ms / 1000.0, 0.5 * sec), in_shape)
                    # what is said is heard whole: no fade-out or fade-in for it, nor for a jingle against a song
                    said = is_speech(before_category, last[0]), is_speech(category, kind)
                    if last[0] != "song" and (kind == "song" or said[0]):
                        fade_out = None
                    if kind != "song" and (last[0] == "song" or said[1]):
                        fade_in = None
            elif kind == "song" and last[0] == "song" and not self.talk:        # crossfade only from song to song
                overlap = min(self.fade, 0.25 * before, 0.25 * sec)
                if overlap > 0:
                    start = began + before - overlap
                    fade_out, fade_in = (before - overlap, overlap, 1), (0.0, overlap, 1)
            if start >= DAY:
                return False
            last[3], last[4] = fade_out, play
        starts.append(start)
        entries.append([kind, tr_, fade_in, None, sec])
        return True

    def _plain_day(self, d):
        name = self.name
        starts, entries = [], []
        deck, npass, last_song, last_jingle, jn = [], 0, None, None, 0
        count, target = 0, 2 + _h(name, d, "target", 0) % 3
        while True:
            if self.jingles and "jingles" in self.on and count >= target:
                jn += 1
                count, target = 0, 2 + _h(name, d, "target", jn) % 3
                pool = [j for j in self.jingles if j is not last_jingle] or self.jingles
                last_jingle = tr_ = pool[_h(name, d, "jingle", jn) % len(pool)]
                kind = "jingle"
            else:
                if not deck:
                    npass += 1
                    deck = sorted(self.songs, key=lambda s: _h(name, d, npass, _tkey(s)), reverse=True)
                    if len(deck) > 1 and deck[-1] is last_song:         # no repeat across two passes
                        deck[-1], deck[0] = deck[0], deck[-1]
                last_song = tr_ = deck.pop()
                count += 1
                kind = "song"
            if not self._add(starts, entries, kind, tr_):
                return starts, entries

    def _next_category(self, d, step, current, run, heard, now, hour, allowed=None):
        """The category that follows, as the rules weigh them at second `now` of the day."""
        table = self.rules["table"]

        def value(factor):
            if not isinstance(factor, list):
                return factor
            what, category, curve = factor
            if what == "h":
                return curve_value(curve, hour)
            if what == "s":                                   # seconds since it was last heard
                return curve_value(curve, now - heard.get(category, -1e9))
            return curve_value(curve, run if category == current else 0)

        def weigh(rows):
            found = []
            for follower, factors in rows:
                if follower in self.pools and (allowed is None or follower in allowed):
                    weight = 1.0
                    for factor in factors:
                        weight *= value(factor)
                        if weight <= 0:
                            break
                    if weight > 0:
                        found.append((follower, weight))
            return found
        found = weigh(table[current]) if current in table else []
        found = found or weigh(table.get("", []))
        if not found:                                          # nothing is allowed: music (else whatever there is)
            for category in ("music", "music_night", "music_dj2"):
                if category in self.pools and (allowed is None or category in allowed):
                    return category
            ordered = sorted(self.pools if allowed is None else allowed)
            return ordered[_h(self.name, d, "any", step) % len(ordered)]
        at = _h(self.name, d, "next", step) / 4294967296.0 * sum(w for _c, w in found)
        for category, weight in found:
            at -= weight
            if at < 0:
                return category
        return found[-1][0]

    def _ruled_day(self, d):
        name = self.name
        history, sequence = self.rules.get("history", {}), set(self.rules.get("sequence", ()))
        heard = {}                                             # category -> second of the day at which it last ended
        for rows in self.rules["table"].values():
            for _follower, factors in rows:
                for f in factors:                              # the day begins in the middle of a programme
                    if isinstance(f, list) and f[0] == "s":
                        heard[f[1]] = -float(_h(name, d, "heard", f[1]) % 3600)
        starts, entries = [], []
        talk_heard = {}  # recording key -> last scheduled end, within UTC day
        recent, place = {}, {}                                 # category -> keys just heard / the next in its order
        current, run, previous = "", 0, ""
        aired = ""                                             # the category of the last item that went on air
        now, step, passed = 0.0, 0, 0
        while self.pools:
            # after a long row of things that are switched off, only what may go on air is asked for
            allowed = {c for c, v in self.pools.items() if any(x[2] <= self.enabled for x in v)} if passed >= 20 else None
            if allowed is not None and not allowed:
                break
            # Only standalone talk is subject to the two-hour cooldown.
            eligible = {
                c: [x for x in v if (allowed is None or x[2] <= self.enabled)
                    and (x[0] != "talk" or now - talk_heard.get(_tkey(x[1]), -1e9) >= 7200)]
                for c, v in self.pools.items() if allowed is None or c in allowed
            }
            eligible = {c: v for c, v in eligible.items() if v}
            if not eligible:
                break
            if len(eligible) != len(self.pools) or allowed is not None:
                allowed = set(eligible)
            category = self._next_category(d, step, current, run, heard, now, hour_of(d * DAY + now), allowed)
            pool = eligible[category]
            if allowed is not None:
                pool = [x for x in pool if x[2] <= self.enabled]
            if category in sequence:
                at = place.get(category)
                if at is None:
                    at = _h(name, d, "order", category) % len(pool)
                kind, tr_, what = pool[at % len(pool)]
                place[category] = at + 1
            else:
                keep = min(history.get(category, len(pool) * 2 // 3), len(pool) - 1)
                last = recent.setdefault(category, [])
                free = [x for x in pool if _tkey(x[1]) not in last]
                free = free or pool  # cooldown can leave only items in category history
                free = [x for x in free if _tkey(x[1]) != previous] or free
                kind, tr_, what = free[_h(name, d, "item", category, step) % len(free)]
                last.append(_tkey(tr_))
                del last[:max(0, len(last) - keep)]
            step += 1
            if not what <= self.enabled:                       # switched off: the rules go on past it
                passed += 1
            else:
                passed = 0
                if not self._add(starts, entries, kind, tr_, d, aired, category):
                    break
                now = starts[-1] + max(1.0, tr_["sec"])
                if kind == "talk":
                    talk_heard[_tkey(tr_)] = now
                previous, aired = _tkey(tr_), category
                if now >= DAY:
                    break
            run = run + 1 if category == current else 1
            current = category
            heard[category] = now
        return starts, entries

    def voices(self, item):
        """What the DJ says over an item of items_from(): [(second in the item, [bank position, stream, length])].

        Over a song the DJ talks in its marked intro (every second time) and outro (every third time); before an advert
        break or the news the line that leads into it replaces the outro. The same for everybody at the same second."""
        start, end, kind, tr_, fin, _fout = item
        dj = tr_.get("dj")
        if kind != "song" or not dj:
            return []
        h = _h(self.name, "dj", int(start), _tkey(tr_))
        out, length = [], end - start
        window = dj.get("i")
        if window and h % 100 < DJ_INTRO:
            line = window[2][(h >> 8) % len(window[2])]
            if window[0] >= (fin[0] + fin[1] if fin else 0.0) and window[0] + line[2] <= length:
                out.append((window[0], line))
        window = dj.get("o")
        if window:
            lines, chance = window[2], DJ_OUTRO
            if _tkey(tr_) not in self.hosted:                            # (not into a night show's breaks)
                following = self.items_from(start + 0.001, 2)[1][2]
                lead = self.toads if following == "ad" else self.tonews if following == "news" else []
                fit = [x for x in lead if x[2] <= window[1] - window[0] + 0.6]
                if fit:
                    lines, chance = fit, DJ_BEFORE_ADS
            if (h >> 16) % 100 < chance:
                line = lines[(h >> 20) % len(lines)]
                free = not out or window[0] >= out[0][0] + out[0][1][2] + 1.0
                if free and window[0] + line[2] <= length:
                    out.append((window[0], line))
        return out

    def talk_over(self, item, dj):
        """When something is said over a song by the items around it: [(from, to)] in seconds of the song - a jingle,
        advert, news or talk that overlaps it, and with dj what the DJ says over another song and still says over this
        one. The song steps back for it as it does for the DJ's lines over itself (voices())."""
        start, end, kind = item[:3]
        if kind != "song" or self.talk or (self.rules and self.rules.get("talkradio")):
            return []
        around, at = [], start
        for _ in range(2):                                    # the two items before it may still sound
            before = self.items_from(at - 0.001, 1)[0]
            if before[0] >= at:
                break
            around.append(before)
            at = before[0]
        around += self.items_from(start + 0.001, 4)[1:]
        out = []
        for other in around:
            if other[2] != "song":
                spans = [(other[0], other[1])]
            elif dj:
                spans = [(other[0] + at, other[0] + at + line[2]) for at, line in self.voices(other)]
            else:
                spans = []
            for a, b in spans:
                a, b = max(a, start), min(b, end)
                if b - a > 0.05:
                    out.append((a - start, b - start))
        return sorted(out)

    def bed_of(self, item):
        """The music bed under an item of items_from(), or None. The same for everybody at the same second."""
        start, _end, kind, tr_, _fin, _fout = item
        if not self.beds or not tr_.get("bed") or kind not in ("talk", "jingle"):
            return None
        return self.beds[_h(self.name, "bed", int(start), _tkey(tr_)) % len(self.beds)]

    def items_from(self, now, n):
        """The item that started last at UNIX time `now` and the n-1 after it:
        [(start, end, kind, track, fade_in, fade_out)]. end is when its audio ends (the item that crosses midnight
        UTC ends there). The next item may start before that end (they overlap) or a little after it."""
        import bisect
        d = int(now // DAY)
        starts, entries = self._day(d)
        i = max(0, bisect.bisect_right(starts, now - d * DAY) - 1)
        out = []
        while len(out) < n:
            if i >= len(entries):
                d, i = d + 1, 0
                starts, entries = self._day(d)
            kind, tr_, fin, fout, play = entries[i]
            end = min(starts[i] + play, DAY)
            out.append((d * DAY + starts[i], d * DAY + end, kind, tr_, fin, fout))
            i += 1
        return out


# ------------------------------------------------------------------------------------------------ app
class NeedCredits(Exception):
    pass


def fmt_time(sec):
    sec = max(0, int(sec))
    return "%d:%02d" % (sec // 60, sec % 60)


class RadioApp(tk.Tk):
    def __init__(self):
        super().__init__()
        awc_i18n.load_saved_lang()
        self.title(t("title"))
        self.geometry("1020x640")
        self.minsize(820, 520)
        self.configure(bg=BG)
        self.q = queue.Queue()
        self.cfg = load_settings()
        self.ffplay = find_tool("ffplay")
        self.ffmpeg = find_tool("ffmpeg")                     # only needed to mix the DJ into the songs
        self.index = None
        self.stations = {}
        self.cards = {}
        self.podcast = None             # the podcast that is shown instead of a station, or None
        self.pod_panel = None
        self.logo_refs = []
        self.station = None
        self.sched = None
        self.upcoming = []              # [(kind, track)] that follow the current item
        self.cur = None                 # (kind, track)
        self.cur_item = None            # the same as a schedule item (start, end, kind, track, fade_in, fade_out)
        self.old = []                   # [(proc, end)]: earlier items that are still fading out
        self.cur_start = 0.0           # UNIX times of the scheduled start / end of the current item
        self.cur_end = 0.0
        self.started = 0.0              # when ffplay was last launched
        self.proc = None
        self.state = "idle"             # idle | loading | playing | paused
        self.pos = 0.0
        self.fail_count = 0
        self.want = queue.Queue()       # tracks to extract
        self.ready = {}                 # key -> path
        self.baked = set()                 # ... of them those that carry the DJ and the music's shape
        self.pending_html = None
        self._style()
        self._build()
        threading.Thread(target=self._prepare_loop, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._poll)
        self.after(250, self._tick)
        self.after(50, self._startup)

    # -------------------------------------------------------------------------------- ui
    def _style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, borderwidth=0)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Dim.TLabel", foreground=DIM)
        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("TButton", background=CARD, foreground=FG, padding=(12, 6), borderwidth=0)
        s.map("TButton", background=[("active", CARD_SEL), ("disabled", PANEL)])
        s.configure("TMenubutton", background=CARD, foreground=FG, arrowcolor=FG, padding=(12, 6), borderwidth=0)
        s.map("TMenubutton", background=[("active", CARD_SEL)])
        s.configure("Accent.TButton", background=ACCENT, foreground="white", padding=(18, 8), font=("Segoe UI", 11, "bold"))
        s.map("Accent.TButton", background=[("active", "#ff7a58")])
        s.configure("TEntry", fieldbackground=PANEL, foreground=FG, insertcolor=FG)
        s.configure("TCombobox", fieldbackground=CARD, background=CARD, foreground=FG, arrowcolor=FG, bordercolor=CARD,
                    lightcolor=CARD, darkcolor=CARD, padding=(6, 2))
        s.map("TCombobox", fieldbackground=[("readonly", CARD)], foreground=[("readonly", FG)],
              selectbackground=[("readonly", CARD)], selectforeground=[("readonly", FG)], background=[("active", CARD_SEL)])
        for option, colour in (("background", PANEL), ("foreground", FG), ("selectBackground", CARD_SEL), ("selectForeground", FG)):
            self.option_add("*TCombobox*Listbox.%s" % option, colour)          # the list that drops down
        s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL)
        s.configure("Horizontal.TScale", background=BG, troughcolor=PANEL)
        s.configure("Vertical.TScrollbar", background=CARD, troughcolor=BG, arrowcolor=DIM, bordercolor=BG, lightcolor=CARD, darkcolor=CARD)
        s.map("Vertical.TScrollbar", background=[("active", CARD_SEL)])

    def _build(self):
        self.setup = tk.Frame(self, bg=BG)
        self.radio = tk.Frame(self, bg=BG)
        self._build_setup()
        self._build_radio()

    def _build_setup(self):
        f = tk.Frame(self.setup, bg=BG)
        f.place(relx=0.5, rely=0.45, anchor="center")
        tk.Label(f, text=t("title"), bg=BG, fg=ACCENT, font=("Segoe UI", 28, "bold")).pack(anchor="w")
        tk.Label(f, text=t("setup_title"), bg=BG, fg=FG, font=("Segoe UI", 14)).pack(anchor="w", pady=(0, 6))
        tk.Label(f, text=t("setup_intro"), bg=BG, fg=DIM, wraplength=620, justify="left").pack(anchor="w", pady=(0, 14))
        self.var_rpf = tk.StringVar(value=self.cfg.get("rpf", ""))
        self.var_pool = tk.StringVar(value=self.cfg.get("pool", ""))
        for label, var, kind in ((t("lbl_rpf"), self.var_rpf, "rpf"), (t("lbl_pool"), self.var_pool, "pool")):
            ttk.Label(f, text=label, style="Dim.TLabel").pack(anchor="w")
            row = tk.Frame(f, bg=BG)
            row.pack(fill="x", pady=(0, 10))
            ttk.Entry(row, textvariable=var, width=70).pack(side="left", fill="x", expand=True)
            ttk.Button(row, text=t("btn_browse"), command=lambda v=var, k=kind: self._browse(v, k)).pack(side="left", padx=(8, 0))
        self.btn_start = ttk.Button(f, text=t("btn_start"), style="Accent.TButton", command=self._start_clicked)
        self.btn_start.pack(anchor="w", pady=(6, 10))
        self.setup_bar = ttk.Progressbar(f, maximum=1.0, length=620)
        self.setup_bar.pack(anchor="w")
        self.setup_status = tk.Label(f, text="", bg=BG, fg=DIM, wraplength=620, justify="left")
        self.setup_status.pack(anchor="w", pady=(6, 0))

    def _build_radio(self):
        top = tk.Frame(self.radio, bg=PANEL)
        top.pack(fill="x")
        tk.Label(top, text=t("title"), bg=PANEL, fg=ACCENT, font=("Segoe UI", 15, "bold")).pack(side="left", padx=14, pady=8)
        ttk.Button(top, text=t("btn_settings"), command=self._show_setup).pack(side="right", padx=8, pady=6)
        ttk.Button(top, text=t("btn_rescan"), command=lambda: self._start_clicked(force=True)).pack(side="right")
        ttk.Button(top, text=t("btn_android"), command=self._android).pack(side="right", padx=8)
        chosen = content_set(self.cfg.get("content", bool(self.cfg.get("ads", False))))   # (older settings: one switch)
        self.var_content = {k: tk.BooleanVar(value=k in chosen) for k in CONTENT}
        button = ttk.Menubutton(top, text=t("btn_content"))
        menu = tk.Menu(button, tearoff=False, bg=PANEL, fg=FG, activebackground=CARD_SEL, activeforeground=FG,
                       selectcolor=FG, borderwidth=0)
        for key in CONTENT:
            menu.add_checkbutton(label=t("c_" + key), variable=self.var_content[key], command=self._content_changed)
        button["menu"] = menu
        button.pack(side="right", padx=8)
        self.var_dj = tk.BooleanVar(value=bool(self.cfg.get("dj", False)))           # the DJ's intros and outros: off too
        ttk.Checkbutton(top, text=t("chk_dj"), variable=self.var_dj, command=self._toggle_dj).pack(side="right", padx=(8, 0))
        self.box_fade = ttk.Combobox(top, state="readonly", width=5,                  # between two songs: off or 1-12 s
                                     values=[t("fade_off")] + [t("fade_s", n=n) for n in range(1, FADE_MAX + 1)])
        self.box_fade.current(self._fade())
        self.box_fade.pack(side="right")
        self.box_fade.bind("<<ComboboxSelected>>", self._fade_chosen)
        tk.Label(top, text=t("lbl_fade"), bg=PANEL, fg=FG).pack(side="right", padx=(8, 4))
        body = tk.Frame(self.radio, bg=BG)
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg=BG, width=390)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        self.canvas = tk.Canvas(left, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.list_frame = tk.Frame(self.canvas, bg=BG)
        self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw", tags="lf")
        self.list_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure("lf", width=e.width))
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120) * 2, "units"))

        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(10, 20), pady=16)
        self.body, self.right = body, right
        self.big = tk.Canvas(right, width=150, height=150, bg=BG, highlightthickness=0)
        self.big.pack(anchor="w")
        self.lbl_station = tk.Label(right, text=t("tune"), bg=BG, fg=FG, font=("Segoe UI", 22, "bold"), anchor="w")
        self.lbl_station.pack(fill="x", pady=(10, 0))
        self.lbl_kind = tk.Label(right, text="", bg=BG, fg=ACCENT, font=("Segoe UI", 9, "bold"), anchor="w")
        self.lbl_kind.pack(fill="x", pady=(14, 0))
        self.lbl_title = tk.Label(right, text="", bg=BG, fg=FG, font=("Segoe UI", 17), anchor="w", wraplength=520, justify="left")
        self.lbl_title.pack(fill="x")
        self.lbl_artist = tk.Label(right, text="", bg=BG, fg=DIM, font=("Segoe UI", 12), anchor="w")
        self.lbl_artist.pack(fill="x")
        bar = tk.Frame(right, bg=BG)
        bar.pack(fill="x", pady=(16, 4))
        self.lbl_pos = tk.Label(bar, text="0:00", bg=BG, fg=DIM, width=5)
        self.lbl_pos.pack(side="left")
        self.bar = ttk.Progressbar(bar, maximum=1.0)
        self.bar.pack(side="left", fill="x", expand=True, padx=6)
        self.lbl_len = tk.Label(bar, text="0:00", bg=BG, fg=DIM, width=5)
        self.lbl_len.pack(side="left")
        ctl = tk.Frame(right, bg=BG)
        ctl.pack(fill="x", pady=8)
        self.btn_play = ttk.Button(ctl, text=t("btn_pause"), command=self._toggle)
        self.btn_play.pack(side="left")
        ttk.Label(ctl, text=t("lbl_volume"), style="Dim.TLabel").pack(side="left", padx=(20, 6))
        self.vol = ttk.Scale(ctl, from_=0, to=100, orient="horizontal", length=140)
        self.vol.set(self.cfg.get("vol", 80))
        self.vol.pack(side="left")
        self.vol.bind("<ButtonRelease-1>", self._vol_changed)
        tk.Label(right, text=t("sync_hint"), bg=BG, fg=DIM, anchor="w", wraplength=520, justify="left").pack(fill="x", pady=(4, 0))
        self.lbl_next = tk.Label(right, text="", bg=BG, fg=DIM, anchor="w")
        self.lbl_next.pack(fill="x", pady=(18, 0))
        self.status = tk.Label(self.radio, text="", bg=PANEL, fg=DIM, anchor="w")
        self.status.pack(fill="x", side="bottom")

    def _android(self):
        """Dialog that writes the station packs (.radiopak) for the Android app."""
        if not self.stations or not self.index:
            return
        try:
            import awc_android  # noqa: PLC0415
        except ImportError as e:
            messagebox.showerror(t("title"), t("err_generic", e=e))
            return
        awc_android.open_dialog(self)

    def _show_setup(self):
        self._stop()
        self._close_podcasts()
        self.radio.pack_forget()
        self.setup.pack(fill="both", expand=True)

    def _show_radio(self):
        self.setup.pack_forget()
        self.radio.pack(fill="both", expand=True)

    # -------------------------------------------------------------------------------- setup flow
    def _browse(self, var, kind):
        init = os.path.dirname(var.get()) if var.get() else ""
        if kind == "rpf":
            p = filedialog.askopenfilename(title=t("lbl_rpf"), initialdir=init or None, filetypes=[("RPF", "*.rpf"), ("*", "*.*")])
        else:
            p = filedialog.askopenfilename(title=t("lbl_pool"), initialdir=init or None, filetypes=[("TTPF", "*.ttpf"), ("*", "*.*")])
        if p:
            var.set(os.path.normpath(p))
            if kind == "rpf" and not self.var_pool.get().strip():
                guess = icons.find_pool(p)
                if guess:
                    self.var_pool.set(guess)

    def _startup(self):
        if not self.ffplay:
            messagebox.showwarning(t("title"), t("err_ffplay"))
        rpf, pool = self.cfg.get("rpf", ""), self.cfg.get("pool", "")
        if rpf and os.path.isfile(rpf):
            idx = load_index(rpf, pool)
            if idx:
                self._open_index(idx, pool, background_icons=True)
                return
        self.setup.pack(fill="both", expand=True)

    def _start_clicked(self, force=False):
        rpf, pool = self.var_rpf.get().strip(), self.var_pool.get().strip()
        if force and self.radio.winfo_ismapped():
            rpf, pool = self.cfg.get("rpf", rpf), self.cfg.get("pool", pool)
            self._show_setup()
        if not os.path.isfile(rpf):
            messagebox.showerror(t("title"), t("err_rpf"))
            return
        if pool and not os.path.isfile(pool):
            pool = ""
        self.cfg.update({"rpf": rpf, "pool": pool})
        save_settings(self.cfg)
        if not force:
            idx = load_index(rpf, pool)
            if idx:
                self._open_index(idx, pool, background_icons=True)
                return
        self.btn_start.config(state="disabled")
        self._setup_status(t("st_scan"), 0)
        threading.Thread(target=self._setup_worker, args=(rpf, pool, self.pending_html), daemon=True).start()

    def _setup_status(self, text, frac=None):
        self.setup_status.config(text=text)
        if frac is not None:
            self.setup_bar["value"] = frac

    def _load_credits(self, html_path):
        if html_path:
            with open(html_path, "r", encoding="utf-8", errors="replace") as f:
                songs = credits.parse(f.read())
        elif os.path.isfile(credits_cache_path()):
            with open(credits_cache_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        else:
            try:
                songs = credits.parse(credits.download())
            except (OSError, ValueError) as e:
                raise NeedCredits(str(e)) from e
        if not songs:
            raise NeedCredits("no songs found in the page")
        with open(credits_cache_path(), "w", encoding="utf-8") as f:
            json.dump(songs, f, ensure_ascii=False)
        return songs

    def _setup_worker(self, rpf, pool, html_path):
        q = self.q
        try:
            songs = []

            def status(text, frac):
                q.put(("setup_status", text, frac))
                if frac == 0.72:                              # the credits are loaded between scan and name matching
                    songs[:] = self._load_credits(html_path)
            idx = make_index(rpf, pool, lambda: songs, status, lambda f: q.put(("setup_prog", f)))
            if idx is None:
                q.put(("setup_fail", t("err_scan")))
                return
            if not idx["stations"]:
                q.put(("setup_fail", t("err_nostations")))
                return
            q.put(("setup_status", t("st_icons"), 0.94))
            self._make_icons(pool)
            q.put(("setup_done", idx, pool))
        except NeedCredits as e:
            q.put(("need_credits", str(e)))
        except InterruptedError:
            return
        except Exception as e:  # noqa: BLE001
            q.put(("setup_fail", t("err_generic", e=e)))

    @staticmethod
    def _make_icons(pool):
        """Cut the logos out of the texture pool; failures are not fatal."""
        try:
            if pool and os.path.isfile(pool) and icons.missing_stations():
                icons.extract(pool)
        except Exception:  # noqa: BLE001
            pass

    def _open_index(self, idx, pool, background_icons=False):
        self._close_podcasts()
        self.index = idx
        self.stations = idx["stations"]
        self._fill_stations()
        self._show_radio()
        self.status.config(text=self._ready_text())
        if background_icons and pool and os.path.isfile(pool) and icons.missing_stations():
            def work():
                self._make_icons(pool)
                self.q.put(("icons_ready",))
            threading.Thread(target=work, daemon=True).start()

    # -------------------------------------------------------------------------------- station list
    def _logo(self, station, size):
        """The logo scaled (smoothly, Pillow) to size x size pixels; the file itself is 256 x 256."""
        p = icons.icon_path(station)
        if not p:
            return None
        try:
            try:
                import base64  # noqa: PLC0415
                import io  # noqa: PLC0415
                from PIL import Image  # noqa: PLC0415
                im = Image.open(p).convert("RGBA")
                if im.size != (size, size):
                    im = im.resize((size, size), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "PNG")
                img = tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
            except ImportError:
                img = tk.PhotoImage(file=p)
                w = max(1, img.width() // size)                   # without Pillow: crude whole-number shrink
                img = img.subsample(w, w) if w > 1 else img
            self.logo_refs.append(img)
            return img
        except (tk.TclError, OSError):
            return None

    @staticmethod
    def _color(name):
        h = zlib.crc32(name.encode("utf-8"))
        return "#%02x%02x%02x" % (60 + (h & 0x7F), 60 + ((h >> 8) & 0x7F), 80 + ((h >> 16) & 0x7F))

    def _draw_logo(self, cv, station, size):
        cv.delete("all")
        img = self._logo(station, size)
        if img:
            cv.create_image(size // 2, size // 2, image=img)
        else:
            cv.create_rectangle(0, 0, size, size, fill=self._color(station), outline="")
            initials = "".join(w[0] for w in station.replace("-", " ").split()[:3]).upper()
            cv.create_text(size // 2, size // 2, text=initials, fill="white", font=("Segoe UI", max(9, size // 4), "bold"))

    def _fill_stations(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.cards.clear()
        self.logo_refs.clear()
        podcasts = (self.index or {}).get("podcasts", {})
        for name, st in list(self.stations.items()) + list(podcasts.items()):
            if podcasts and name == next(iter(podcasts)):
                tk.Label(self.list_frame, text=t("pod_header"), bg=BG, fg=ACCENT, font=("Segoe UI", 12, "bold"),
                         anchor="w").pack(fill="x", padx=12, pady=(14, 2))
            pod = name in podcasts
            card = tk.Frame(self.list_frame, bg=CARD, cursor="hand2")
            card.pack(fill="x", padx=(10, 6), pady=3)
            cv = tk.Canvas(card, width=44, height=44, bg=CARD, highlightthickness=0)
            cv.pack(side="left", padx=8, pady=6)
            self._draw_logo(cv, name, 44)
            box = tk.Frame(card, bg=CARD)
            box.pack(side="left", fill="x", expand=True)
            lab = tk.Label(box, text=name, bg=CARD, fg=FG, font=("Segoe UI", 11, "bold"), anchor="w")
            lab.pack(fill="x")
            count = "pod_count" if pod else "n_segments" if st.get("rules", {}).get("talkradio") \
                else "n_shows" if any(_is_show(s) for s in st["songs"]) else "n_songs"
            sub = tk.Label(box, text=t(count, n=len(st["songs"])), bg=CARD, fg=DIM, anchor="w")
            sub.pack(fill="x")
            for w in (card, cv, box, lab, sub):
                w.bind("<Button-1>", lambda e, n=name, p=pod: self._open_podcast(n) if p else self._tune(n))
            self.cards[name] = (card, cv, box, lab, sub)
        self._mark_selected()

    def _mark_selected(self):
        for name, widgets in self.cards.items():
            col = CARD_SEL if name == (self.podcast or self.station) else CARD
            for w in widgets:
                w.config(bg=col)

    def _see_card(self, name):
        """Scroll the station list so that the chosen card is visible."""
        card = self.cards.get(name, (None,))[0]
        if card is None:
            return
        self.update_idletasks()
        total = max(1, self.list_frame.winfo_height())
        top, bottom = card.winfo_y(), card.winfo_y() + card.winfo_height()
        view_top = self.canvas.yview()[0] * total
        view_bottom = self.canvas.yview()[1] * total
        if top < view_top or bottom > view_bottom:
            self.canvas.yview_moveto(max(0.0, (top - 12) / total))

    def _refresh_logos(self):
        self.logo_refs.clear()
        for name, widgets in self.cards.items():
            self._draw_logo(widgets[1], name, 44)
        if self.station:
            self._draw_logo(self.big, self.station, 150)

    # -------------------------------------------------------------------------------- podcasts
    def _open_podcast(self, name):
        """A podcast takes the place of the station: the radio stops, the episodes are listed."""
        if not self.ffplay:
            messagebox.showerror(t("title"), t("err_ffplay"))
            return
        import awc_radio_podcasts  # noqa: PLC0415
        self._stop()
        self.station = None
        self.podcast = name
        if self.pod_panel is None:
            self.pod_panel = awc_radio_podcasts.PodcastPanel(self, sys.modules[__name__], self.body)
        self.right.pack_forget()
        self.pod_panel.pack(side="left", fill="both", expand=True, padx=(10, 20), pady=16)
        self._mark_selected()
        self._see_card(name)
        self.pod_panel.open_show(name)

    def _leave_podcast(self):
        """Back to the radio's side of the window; a running episode pauses and keeps its place."""
        if self.pod_panel is not None and self.podcast is not None:
            self.pod_panel.stop()
            self.pod_panel.pack_forget()
            self.right.pack(side="left", fill="both", expand=True, padx=(10, 20), pady=16)
        self.podcast = None

    def _close_podcasts(self):
        """Another set of files: the podcast side starts anew."""
        self._leave_podcast()
        if self.pod_panel is not None:
            self.pod_panel.stop()
            self.pod_panel.destroy()
            self.pod_panel = None

    # -------------------------------------------------------------------------------- playback
    def _save_prefs(self):
        self.cfg["vol"] = int(self.vol.get())
        save_settings(self.cfg)

    def _tune(self, name):
        if not self.ffplay:
            messagebox.showerror(t("title"), t("err_ffplay"))
            return
        self._leave_podcast()
        self._stop()
        self.station = name
        with self.want.mutex:               # drop work for the previous station
            self.want.queue.clear()
        self.upcoming = []
        self.fail_count = 0
        self.sched = self._programme(name)
        self.box_fade.config(state="disabled" if self.sched.mix is not None else "readonly")      # mixed as in the game
        self.cur = None
        self._mark_selected()
        self._see_card(name)
        self._draw_logo(self.big, name, 150)
        self.lbl_station.config(text=name)
        self._sync()

    def _fade(self):
        """The chosen seconds of crossfade between two songs; 0 = off."""
        try:
            return min(FADE_MAX, max(0, int(self.cfg.get("crossfade", int(FADE)))))
        except (TypeError, ValueError):
            return int(FADE)

    def _ready_text(self):
        """The status line; with another crossfade than the standard one it says what that means."""
        ruled = self.sched is not None and self.sched.mix is not None
        return t("st_fade_note") if self._fade() != int(FADE) and not ruled else t("st_ready", n=len(self.stations))

    def _programme(self, name):
        content = [k for k in CONTENT if self.var_content[k].get()]
        return Schedule(name, self.stations[name], content, self._fade(), self.index.get("news", []))

    def _content_changed(self):
        """The other programme of the station: something between the songs was switched on or off."""
        self.cfg["content"] = [k for k in CONTENT if self.var_content[k].get()]
        save_settings(self.cfg)
        self._retune()

    def _toggle_dj(self):
        """The DJ talks over the songs, or not; the timetable stays the same."""
        if self.var_dj.get() and not self.ffmpeg:
            self.var_dj.set(False)
            messagebox.showinfo(t("title"), t("dj_needs_ffmpeg"))
            return
        self.cfg["dj"] = bool(self.var_dj.get())
        save_settings(self.cfg)
        if self.station and self.state in ("playing", "loading"):
            with self.want.mutex:
                self.want.queue.clear()
            self._sync()

    def _voices(self, item):
        return self.sched.voices(item) if self.sched is not None and self.var_dj.get() else []

    def _bed(self, item):
        return self.sched.bed_of(item) if self.sched is not None and self.ffmpeg else None

    def _ducks(self, item):
        return self.sched.talk_over(item, bool(self.var_dj.get())) if self.sched is not None else []

    def _key(self, item):
        """The name of the file that plays an item: the track, or the track with what is mixed to it (what the DJ says
        over a song, the music bed under the DJ's talk). With the DJ in it, its fades and the places where it steps back
        are part of the file and of the name."""
        voices, bed = self._voices(item), self._bed(item)
        key = _tkey(item[3])
        if bed:
            return key + "bed" + _tkey(bed)
        if not voices:
            return key
        music = repr((item[4], item[5], [(round(a, 1), round(b, 1)) for a, b in self._ducks(item)]))
        return key + "dj" + "-".join("%d_%d@%d" % (v[1][0], v[1][1], int(v[0] * 10)) for v in voices) + "m%08x" % _h(music)

    def _fade_chosen(self, _e=None):
        self.cfg["crossfade"] = self.box_fade.current()
        save_settings(self.cfg)
        self.box_fade.selection_clear()
        self._retune()
        self.status.config(text=self._ready_text())

    def _retune(self):
        """A setting that shapes the programme changed."""
        if self.station and self.state in ("playing", "loading"):
            self._tune(self.station)
        elif self.station:
            self.sched = self._programme(self.station)

    def _show_next(self):
        if self.upcoming:
            _s, _e, kind, tr_, _fi, _fo = self.upcoming[0]
            pair = (tr_["title"], tr_["artist"]) if _is_show(tr_) else (tr_["artist"], tr_["title"])
            if kind == "ad":
                label = " - ".join(x for x in (t("advert"), tr_["title"]) if x)
            elif kind in ("news", "talk"):
                label = tr_["title"] or t(kind)
            else:
                label = t("kind_jingle") if kind == "jingle" else "%s - %s" % pair if tr_["artist"] else tr_["title"]
            self.lbl_next.config(text="%s %s" % (t("lbl_next"), label))

    def _set_cur(self, item):
        """Make `item` (start, end, kind, track, fade_in, fade_out) the current one and show it."""
        self.cur_item = item
        self.cur_start, self.cur_end, kind, tr_, _fi, _fo = item
        self.cur = (kind, tr_)
        spoken = {"jingle": "kind_jingle", "ad": "kind_ad", "news": "kind_news", "talk": "kind_talk"}.get(kind)
        self.lbl_kind.config(text=t(spoken or ("kind_show" if _is_show(tr_) else "kind_song")))
        self.lbl_title.config(text=("♪ " + t("kind_jingle")) if kind == "jingle" else (tr_["title"] or t("advert")) if kind == "ad"
                              else (tr_["title"] or t(kind)) if kind in ("news", "talk") else tr_["title"])
        self.lbl_artist.config(text=tr_["artist"] if kind == "song" else (kind == "talk" and tr_["artist"]) or self.station or "")
        self.lbl_len.config(text=fmt_time(tr_["sec"]))
        self.seg_shown = None

    def _show_segment(self, pos):
        """A mix shows the title of the part that is playing (from its markers)."""
        if not self.cur or self.cur[0] != "song" or not self.cur[1].get("m"):
            return
        seg = segment_at(self.cur[1], pos)
        key = seg[0] if seg else None
        if key == self.seg_shown:
            return
        self.seg_shown = key
        self.lbl_title.config(text=(seg[1] if seg and seg[1] else self.cur[1]["title"]))
        self.lbl_artist.config(text=seg[2] if seg and seg[1] else self.cur[1]["artist"])

    def _queue_tracks(self):
        for item in [self.cur_item] + self.upcoming:             # extract in playing order
            key = self._key(item)
            if key not in self.ready:
                self.want.put((key, item[3], self._voices(item), self._bed(item), self._music(item)))

    def _music(self, item):
        """The FFmpeg filters that shape the sound of an item as the programme has it: its fades, and the places where it
        steps back for what is said over it (the DJ's own lines are seen to where they are mixed in)."""
        fin, fout = item[4], item[5]
        out = []
        ducks = self._ducks(item)
        if ducks:
            out.append("volume=eval=frame:volume='%s'" % awc_dj.duck_filter(ducks))
        if fin:
            out.append("afade=t=in:st=%.3f:d=%.3f:curve=%s" % (fin[0], fin[1], SHAPES[fin[2]]))
        if fout:
            out.append("afade=t=out:st=%.3f:d=%.3f:curve=%s" % (fout[0], fout[1], SHAPES[fout[2]]))
        return out

    def _sync(self, skip_current=False):
        """Jump to whatever the station has on air right now (the same for everybody with the same clock).

        skip_current: the current item is broken, take the one after it."""
        self._kill()
        if not self.sched:
            return
        items = self.sched.items_from(time.time() + LEAD, 5)
        if skip_current and self.cur and _tkey(items[0][3]) == _tkey(self.cur[1]):
            items = items[1:]
        self._set_cur(items[0])
        self.upcoming = items[1:3]
        self._queue_tracks()
        self._show_next()
        self.state = "loading"
        self.status.config(text=t("st_loading"))

    def _spawn(self, item):
        """Start ffplay for `item` at the position the station has reached by now, with its crossfade in/out."""
        start, _end, _kind, _tr, fin, _fout = item
        key = self._key(item)
        path = self.ready.get(key)
        if not path or not os.path.isfile(path):
            return None
        pos = max(0.0, time.time() + LEAD - start)               # where the station is by the time ffplay is up
        pos = pos if pos > 0.5 else 0.0
        cmd = [self.ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", "-volume", str(int(self.vol.get()))]
        if pos:
            cmd += ["-ss", "%.2f" % pos]
        shape = [] if key in self.baked else self._music(item)   # (in a file with the DJ it is done already)
        if fin and pos >= fin[0] + fin[1]:
            shape = [x for x in shape if not x.startswith("afade=t=in")]
        if shape:                                                # asetpts: the filters see the position within the track
            cmd += ["-af", ",".join(["asetpts=N/SR/TB+%.3f/TB" % pos] + shape)]
        cmd.append(path)
        try:
            return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except OSError as e:
            messagebox.showerror(t("title"), t("err_generic", e=e))
            self.state = "idle"
            return None

    def _start_proc(self):
        proc = self._spawn(self.cur_item)
        if proc is None:
            return False
        self.proc = proc
        self.started = time.time()
        self.state = "playing"
        self.btn_play.config(text=t("btn_pause"))
        self.status.config(text=self._ready_text())
        return True

    def _kill(self):
        for p in [self.proc] + [p for p, _end in self.old]:
            if p and p.poll() is None:
                try:
                    p.kill()
                    p.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        self.proc = None
        self.old = []

    def _reap(self, now):
        """Stop the faded-out processes of earlier items once their audio is over."""
        keep = []
        for p, end in self.old:
            if p.poll() is not None:
                continue
            if now >= end:
                try:
                    p.kill()
                except OSError:
                    pass
            else:
                keep.append((p, end))
        self.old = keep

    def _stop(self):
        self._kill()
        self.state = "idle"
        self.cur = None
        self.sched = None
        self.upcoming = []
        self.bar["value"] = 0

    def _toggle(self):
        if self.state in ("playing", "loading"):
            self._kill()
            self.state = "paused"
            self.btn_play.config(text=t("btn_play"))
        elif self.state == "paused":
            self._sync()                      # a radio keeps running while paused: resume live
        elif self.state == "idle" and self.station:
            self._tune(self.station)

    def _vol_changed(self, _e=None):
        self._save_prefs()
        if self.state == "playing":           # ffplay cannot change its volume from outside: restart at the live position
            self._sync()

    def _cut(self, tr_):
        """The file of one track in the cache (made if it is not there)."""
        dst = os.path.join(cache_dir(), "r_%s.opus" % _tkey(tr_))
        if not (os.path.isfile(dst) and os.path.getsize(dst) > 0):
            extract_track(self.index, self.cfg["rpf"], tr_, dst + ".part")
            os.replace(dst + ".part", dst)
        return dst

    def _prepare_loop(self):
        """Background thread: cut the tracks out of the archive into small Opus files that ffplay can play, and mix the
        DJ into those he talks over."""
        while True:
            key, tr_, voices, bed, music = self.want.get()
            if key in self.ready:
                continue
            try:
                path = self._cut(tr_)
                if bed:
                    mixed = os.path.join(cache_dir(), "b_%s.opus" % hashlib.sha1(key.encode()).hexdigest()[:20])
                    if not (os.path.isfile(mixed) and os.path.getsize(mixed) > 0):
                        try:
                            awc_dj.lay_bed(path, self._cut(bed), tr_["sec"], mixed + ".part", self.ffmpeg)
                            os.replace(mixed + ".part", mixed)
                        except Exception:  # noqa: BLE001   (the talk alone rather than silence)
                            mixed = path
                    path = mixed
                if voices:
                    mixed = os.path.join(cache_dir(), "d_%s.opus" % hashlib.sha1(key.encode()).hexdigest()[:20])
                    if not (os.path.isfile(mixed) and os.path.getsize(mixed) > 0):
                        try:
                            lines = [(at, self._cut(awc_dj.line_track(x)), x[2]) for at, x in voices]
                            awc_dj.mix(path, lines, mixed + ".part", self.ffmpeg, music)
                            os.replace(mixed + ".part", mixed)
                        except Exception:  # noqa: BLE001   (without the DJ rather than silence)
                            mixed = path
                    if mixed != path:
                        self.baked.add(key)
                    path = mixed
                self.ready[key] = path
            except Exception:  # noqa: BLE001
                self.ready[key] = ""

    def _tick(self):
        try:
            now = time.time()
            if self.state == "loading" and self.cur:
                key = self._key(self.cur_item)
                if self._over(now, 0.0):          # extraction took so long that the item is over: catch up
                    self._sync()
                elif key in self.ready:
                    if self.ready[key]:
                        self._start_proc()
                    else:                         # extraction failed: skip this one
                        self._count_failure()
            elif self.state == "playing" and self.cur:
                pos = max(0.0, now - self.cur_start)
                sec = max(1.0, self.cur_end - self.cur_start)
                self.bar["value"] = min(1.0, pos / sec)
                self.lbl_pos.config(text=fmt_time(pos))
                self._show_segment(pos)
                self._reap(now)
                nxt = self.upcoming[0] if self.upcoming else None
                if nxt and now + LEAD >= nxt[0]:  # time for the next item: it fades in while this one fades out
                    key = self._key(nxt)
                    if self.ready.get(key):
                        proc = self._spawn(nxt)
                        if proc is not None:
                            if self.proc is not None:
                                faded = bool(self.cur_item[5])          # (still the item that is ending)
                                self.old.append((self.proc, self.cur_end + (0.0 if faded else TAIL)))
                            self.proc, self.started, self.fail_count = proc, now, 0
                            self._set_cur(nxt)
                            self.upcoming = self.sched.items_from(nxt[0] + 0.001, 4)[1:3]
                            self._queue_tracks()
                            self._show_next()
                    elif key in self.ready:       # extraction failed
                        self._sync()
                if self.state == "playing" and self._over(now, 1.0):
                    self._sync()                  # nothing followed (next file not ready): wait for it
                elif self.state == "playing" and self.proc is not None and self.proc.poll() is not None \
                        and now - self.started < 2.0 and self.cur_end - now > 3.0:
                    self._count_failure()         # ended right away: probably broken
        finally:
            self.after(100, self._tick)

    def _over(self, now, patience):
        """The current item has ended and the next one is not just about to start."""
        if now < self.cur_end:
            return False
        return not self.upcoming or now >= self.upcoming[0][0] + patience

    def _count_failure(self):
        self.fail_count += 1
        if self.fail_count >= 4:
            self._stop()
            self.status.config(text=t("err_generic", e="playback failed"))
            return
        self._sync(skip_current=True)

    # -------------------------------------------------------------------------------- messages
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "setup_prog":
                    self._setup_status(t("st_scan"), msg[1])
                elif kind == "setup_status":
                    self._setup_status(msg[1], msg[2])
                elif kind == "setup_fail":
                    self.btn_start.config(state="normal")
                    self._setup_status("", 0)
                    messagebox.showerror(t("title"), msg[1])
                elif kind == "need_credits":
                    self.btn_start.config(state="normal")
                    self._setup_status("", 0)
                    if messagebox.askyesno(t("title"), t("err_credits", e=msg[1])):
                        p = filedialog.askopenfilename(title=t("err_html"), filetypes=[("HTML", "*.html *.htm"), ("*", "*.*")])
                        if p:
                            self.pending_html = p
                            self._start_clicked()
                elif kind == "setup_done":
                    self.btn_start.config(state="normal")
                    self.pending_html = None
                    self._setup_status("", 0)
                    self._open_index(msg[1], msg[2])
                elif kind == "icons_ready":
                    self._refresh_logos()
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _close(self):
        self._kill()
        if self.pod_panel is not None:
            self.pod_panel.stop()
        self._save_prefs()
        self.destroy()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--build-index":           # no window: rebuild the station index from the saved settings
        cfg = load_settings()
        with open(credits_cache_path(), "r", encoding="utf-8") as f:
            songs = json.load(f)
        idx = make_index(cfg["rpf"], cfg.get("pool", ""), lambda: songs, lambda text, frac: print("%3d%% %s" % (frac * 100, text), flush=True),
                         lambda f: print("scan %3d%%" % (f * 100 / 0.7), flush=True) if int(f * 1000) % 50 == 0 else None)
        print("stations:", len(idx["stations"]) if idx else 0)
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--export-pack":           # no window: python awc_radio.py --export-pack <folder> [...]
        import awc_android  # noqa: PLC0415
        awc_android.main(sys.argv[2:])
        return
    RadioApp().mainloop()


if __name__ == "__main__":
    main()
