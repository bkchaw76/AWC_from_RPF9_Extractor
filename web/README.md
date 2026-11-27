# AWC Radio Web — experimental Linux/Docker port

This branch contains an initial web-streaming implementation using the **original AWC archive scanner and deterministic station scheduler**. It is not yet a faithful replacement for the desktop app.

## Run (Linux Docker host)

Update the **host** path in `compose.web.yml` so it points to your own game folder. Keep the container mount at `/game:ro`.

From the repository root:

```bash
docker compose -f compose.web.yml up -d --build
docker compose -f compose.web.yml logs -f
```

On your phone, open `http://YOUR-N100-IP:8088` on the same LAN.

The mounted game directory should contain:

```text
GTA6/
├── audio.rpf
├── data.rpf
└── data/ttdb/ui/ttdb_ui.ttpf
```

Only `audio.rpf` is essential for the initial scan. `data.rpf` adds episode and mix titles; the `.ttpf` file adds station logos. The first indexing run may take minutes, and cached extracted Opus tracks consume extra space.

## Included in this first version

- Existing upstream game file parser and station discovery
- Existing clock-based station scheduling, with default adverts/DJ/jingle settings
- Station list, live title/artist API, and game-extracted logos
- FFmpeg MP3 streaming to Safari or other browser audio players
- Docker for x86-64 Linux (Debian), read-only game mount, persistent cache

## Limitations and safety

**Experimental / untested with actual game data.** The initial stream follows the scheduled recording sequentially, but does **not** yet reproduce overlapping crossfades, ducking, DJ voice overlays, or music beds. There can be gaps between tracks. Podcast and on-demand pages are not implemented. Concurrent listeners each start their own FFmpeg process. The whole station archive is not pre-extracted.

No user authentication or TLS is included. Use only on a trusted LAN or behind a protected HTTPS reverse proxy / VPN; **do not forward port 8088 to the public internet**. Keep game archives and extracted game audio private, and do not redistribute them.

## Endpoints

- `/api/status`
- `/api/stations`
- `/api/stations/{name}/now`
- `/api/stations/{name}/stream` (MP3)
- `/api/logo/{name}` (PNG)

Next: test on an owned archive, match the desktop mixing engine, expose podcasts and HLS.
