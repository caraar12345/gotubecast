# GoTubeCast — Codebase Guide

## What this is

GoTubeCast emulates a YouTube Leanback "cast device". It registers with YouTube's Lounge API so phones and tablets can discover it and send playback commands to it — the same protocol used by Chromecast and smart TVs.

The `examples/cast.py` integration script turns this into a full playback device: it drives `mpv` via yt-dlp and forwards all Lounge commands (play, pause, seek, volume, subtitles) to mpv over a UNIX socket.

## Build & run

```bash
go build .                      # produces ./gotubecast binary
./gotubecast -n "My TV"         # stream commands on stdout
python3 examples/cast.py        # full Pi integration (runs gotubecast + mpv)
```

**Go version:** 1.16+ required (go.mod). Tested on 1.19 (Raspberry Pi OS).  
**Python version:** 3.9+ required (cast.py uses `Optional[]` for 3.9 compat, not `X | Y`).

## Files

| File | Purpose |
|---|---|
| `main.go` | Lounge API client — registers screen, long-polls bind stream, dispatches commands to stdout |
| `dial.go` | DIAL/SSDP server — auto-discovery so phones find the device without a PIN |
| `httpclient.go` | Shared `outboundHTTP` client; supports custom root CA for traffic inspection |
| `examples/cast.py` | Python integration: spawns gotubecast, routes commands to mpv via IPC |
| `examples/setup.sh` | One-shot Pi installer: packages, uosc, touch-gestures, binary, service |
| `examples/gotubecast-cast.service` | systemd user unit for auto-start on login |
| `examples/labwc-session.target` | Supplies the systemd user target labwc's package omits (see gotchas) |

## Architecture

```
YouTube Lounge API  ←──────────────────────────────┐
       │  (long-poll /bc/bind)                      │
       ▼                                            │ postBind()
  main.go  ──── stdout text commands ────►  cast.py │
  dial.go  ◄─── SSDP/DIAL discovery ───  Phone app  │
                                                    │
  cast.py  ──── JSON IPC (UNIX socket) ───►  mpv   ─┘
  cast.py  ──── subprocess ──────────────►  yt-dlp
```

## Lounge command protocol (stdout)

`main.go` writes one command per line to stdout. `cast.py` reads these:

| Line | Meaning |
|---|---|
| `video_id <id>` | Start playing this video |
| `play` | Unpause |
| `pause` | Pause |
| `stop` | Stop playback |
| `seek_to <seconds>` | Seek to absolute position |
| `set_volume <0-100>` | Set volume |
| `set_subtitles <id> <lang>` | Load subtitle track |
| `set_subtitles off` | Disable subtitles |
| `next` / `previous` | Playlist navigation |
| `pairing_code <code>` | 12-digit manual pairing code (shown to user) |
| `dial_url <url>` | DIAL endpoint URL (for debugging) |
| `remote_join <id> <name>` | Phone connected |
| `remote_leave <id>` | Phone disconnected |

## Key concurrency rules

Three mutexes guard shared state:

- **`dialStateMu` (RWMutex)** — protects `curVideoId`, `curVideo`, `curList*`. HTTP handler goroutines (DIAL) read; Lounge bind goroutine writes. Always use `RLock` for reads, `Lock` for writes.
- **`playbackMu` (Mutex)** — protects `curTime`, `startTime`, `playState`, `loungeCPN`. Held briefly to snapshot playback state.
- **`postBindMu` (Mutex)** — serialises outbound Lounge `postBind` HTTP calls.
- **`printLock` (Mutex)** — serialises stdout writes.

In `cast.py`: `_mpv_lock` protects `_mpv_proc`, `_play_generation`, `_subtitle_generation`, `_active_video_id`. Always acquire before reading or writing any of these.

## DIAL / SSDP protocol

`dial.go` implements the subset of DIAL needed for YouTube auto-discovery:

1. **SSDP listener** (UDP 239.255.255.250:1900) — replies to `M-SEARCH` probes from YouTube apps on the LAN.
2. **HTTP server** (default port 8008):
   - `GET /dial/dd.xml` — UPnP device descriptor
   - `GET /apps/YouTube` — returns `screenId` + `loungeToken` in XML so the phone can connect via Lounge without a PIN
   - `POST /apps/YouTube` — phone sends a UUID `pairingCode`; we call `get_pairing_code?ctx=pair` on YouTube to register it so the phone's follow-up `get_screen` lookup succeeds
   - `DELETE /apps/YouTube` or `/apps/YouTube/run` — stop playback

## Useful flags

```
-n <name>           Display name shown in YouTube app  (default: "Golang Test TV")
-i <id>             App identifier                     (default: "golang-test-838")
-s <screen-id>      Reuse a persistent screen ID       (default: auto-generate)
-p <port>           DIAL HTTP server port              (default: 8008; 0 to disable)
-d <level>          Debug level: 0=off, 1=commands, 2=timestamps
-debug-log <file>   Write debug output to file instead of stderr
-debug-root-ca <f>  Extra PEM root CA for TLS inspection (e.g. mitmproxy)
```

## cast.py environment variables

| Variable | Default | Notes |
|---|---|---|
| `SCREEN_NAME` | `GoTubeCast Pi` | Friendly name in YouTube app |
| `SCREEN_APP` | `gotubecast-pi-v1` | App identifier |
| `SCREEN_ID` | _(auto)_ | Set to persist device identity across restarts |
| `DIAL_PORT` | `8008` | Must match gotubecast `-p` |
| `SUB_LANG` | `en` | Subtitle language; blank to disable |
| `SUB_FONT_SIZE` | `30` | Subtitle font size (mpv units; mpv default is 55) |
| `VIDEO_QUALITY` | `1080` | Maximum video height for yt-dlp format selection |
| `FULLSCREEN` | `1` | Start mpv fullscreen; set `0` to disable |
| `MPV_OPTS` | _(empty)_ | Extra mpv CLI flags; appended last, so overrides the above |

## Pi setup

```bash
bash examples/setup.sh          # installs everything, enables service
systemctl --user start gotubecast-cast
journalctl --user -u gotubecast-cast -f
```

Overrides go in `~/.config/gotubecast/env` (shell `KEY=value` format).

The service targets `graphical-session.target` and sets `WAYLAND_DISPLAY=wayland-0` for labwc on Raspberry Pi OS Bookworm.

## Common gotchas

- **`ioutil` is deprecated** — new code should use `io.ReadAll` / `os.ReadFile`. Existing uses in `main.go` remain for Go 1.16 compat but are candidates for cleanup.
- **`screenUid` is hardcoded** in `main.go`. The `screenId` is generated per-run (or supplied via `-s`). These are different: `screenUid` is a stable device UUID; `screenId` is the YouTube Lounge registration ID.
- **Lounge token expiry** — `get_lounge_token_batch` is called once at startup. Tokens expire (~24h). On expiry, the bind loop will start failing; a restart regenerates the token. Long-running daemon support is a known TODO.
- **`graphical-session.target` is not automatic on Raspberry Pi OS** — the service is
  `WantedBy=graphical-session.target`, but the lightdm→labwc session never activates that
  target: `labwc(1)` documents `labwc-session.target`, yet the Debian package does not ship
  the unit, and the session uses lxsession XDG autostart rather than systemd integration.
  Symptom: `systemctl --user is-enabled` says `enabled` while `ActiveEnterTimestamp` is
  empty — the unit has never run. `setup.sh` now installs `examples/labwc-session.target`
  and hooks `~/.config/labwc/{autostart,shutdown}`. Note labwc reads only the *first*
  autostart found unless run with `-m`/`--merge-config`; Pi OS does use `-m` (see
  `/usr/bin/labwc-pi`), so a user file augments the system one instead of replacing it.
- **yt-dlp format string** — in `cast.py`, the format string prefers `mp4+m4a` for hardware-accelerated playback on Pi. If yt-dlp returns two URLs, mpv receives both via `--audio-file=` (DASH demux).
- **Pi 5 has no H.264/AV1 hardware decode** (HEVC only), so video is software-decoded; AV1 via dav1d is actually the cheapest option. The real 1080p60 bottleneck is mpv's GPU render passes: the default lanczos scaling costs ~39 ms/frame (budget 16.7 ms). `setup.sh` prepends `profile=fast` to `mpv.conf` (~6 ms/frame). Diagnose with mpv IPC properties `frame-drop-count` and `vo-passes`.
- **yt-dlp is installed as the standalone release binary** (bundles Python + curl_cffi) plus `deno` as its JS runtime. Don't go back to `pip install`: a distro Python upgrade orphans the module and every cast fails with "No stream URL".
