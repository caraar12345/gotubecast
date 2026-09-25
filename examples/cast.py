#!/usr/bin/env python3
# ruff: noqa: UP007  # keep Optional[] for Python 3.9 compat (Raspberry Pi OS Bullseye)
"""
cast.py — YouTube cast device integration for Raspberry Pi 5

Spawns gotubecast (with DIAL enabled), then drives mpv via yt-dlp.
The YouTube app on your phone discovers this device automatically via DIAL/SSDP
and connects through the YouTube Lounge API (see https://github.com/FabioGNR/pyytlounge).

Requirements:
    bash examples/setup.sh   # installs mpv, yt-dlp, uosc, and touch-gestures
    go build .               # build gotubecast binary and put it on PATH

Touch controls (installed by setup.sh — uosc + mpv-touch-gestures):
    Tap                        → pause/unpause
    Swipe left/right           → seek
    Swipe up/down (right half) → volume
    Long-press                 → uosc context menu (subtitle/audio track, quality…)
    ✕ button in controls bar   → quit mpv

Configuration (environment variables):
    SCREEN_NAME     Friendly name shown in the YouTube app   (default: GoTubeCast Pi)
    SCREEN_APP      App identifier                           (default: gotubecast-pi-v1)
    SCREEN_ID       Persistent screen ID (leave blank to auto-generate)
    DIAL_PORT       DIAL HTTP server port                    (default: 8008)
    GTC_DEBUG_LEVEL gotubecast debug level                   (default: 2)
    GTC_DEBUG_LOG   gotubecast debug log file path           (default: $XDG_RUNTIME_DIR/gotubecast/gotubecast-debug.log)
    GTC_DEBUG_ROOT_CA PEM path for extra TLS roots (MITM proxy) → -debug-root-ca
    GTC_TRACE_PROTOCOL Enable protocol trace logging (0/1)   (default: 0)
    SUB_LANG        Subtitle language code, e.g. "en"        (default: en, blank to disable)
    SUB_FONT_SIZE   Subtitle font size (mpv units)           (default: 30; mpv default is 55)
    VIDEO_QUALITY   Maximum video height in pixels           (default: 1080)
    FULLSCREEN      Start mpv fullscreen (0/1)               (default: 1)
    MPV_OPTS        Extra mpv CLI options (space-separated); overrides the above

Usage:
    python3 cast.py
"""

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCREEN_NAME   = os.environ.get("SCREEN_NAME",   "GoTubeCast Pi")
SCREEN_APP    = os.environ.get("SCREEN_APP",    "gotubecast-pi-v1")
SCREEN_ID     = os.environ.get("SCREEN_ID",     "")
DIAL_PORT     = int(os.environ.get("DIAL_PORT", "8008"))
GTC_DEBUG_LEVEL = os.environ.get("GTC_DEBUG_LEVEL", "2")
GTC_TRACE_PROTOCOL = os.environ.get("GTC_TRACE_PROTOCOL", "0")
SUB_LANG      = os.environ.get("SUB_LANG",      "en")
SUB_FONT_SIZE = os.environ.get("SUB_FONT_SIZE", "30")   # mpv default is 55
VIDEO_QUALITY = os.environ.get("VIDEO_QUALITY", "1080")
FULLSCREEN    = os.environ.get("FULLSCREEN",    "1") not in ("0", "", "false", "no")
MPV_EXTRA     = os.environ.get("MPV_OPTS",      "").split()

# Use XDG_RUNTIME_DIR when available (OS-managed, 0700); otherwise create a
# private temp directory so the socket and subtitle files are not world-readable.
_xdg = os.environ.get("XDG_RUNTIME_DIR")
RUNTIME_DIR = Path(_xdg) / "gotubecast" if _xdg else Path(
    tempfile.mkdtemp(prefix="gotubecast-")
)
RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)

MPV_SOCKET = str(RUNTIME_DIR / "mpv.sock")
TEMP_DIR   = RUNTIME_DIR / "downloads"
GTC_DEBUG_LOG = os.environ.get(
    "GTC_DEBUG_LOG", str(RUNTIME_DIR / "gotubecast-debug.log")
)
GTC_DEBUG_ROOT_CA = os.environ.get("GTC_DEBUG_ROOT_CA", "").strip()

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_mpv_proc = None
_mpv_lock = threading.Lock()
_play_generation     = 0   # incremented on each play_video(); guards stale fetches
_subtitle_generation = 0   # incremented on each subtitle change; guards stale loads
_active_video_id: Optional[str] = None  # video currently tied to mpv; cleared between plays / on stop

# ---------------------------------------------------------------------------
# mpv IPC
# ---------------------------------------------------------------------------

def mpv_ipc(cmd: dict) -> None:
    """Send a JSON IPC command to the running mpv instance."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(MPV_SOCKET)
        s.sendall(json.dumps(cmd).encode() + b"\n")
        s.close()
    except OSError:
        pass


def mpv_get_property(name: str):
    """Read one mpv property via the IPC socket (request/response)."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(MPV_SOCKET)
        s.sendall((json.dumps({"command": ["get_property", name]}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk
        s.close()
        if b"\n" not in buf:
            return None
        line = buf.split(b"\n", 1)[0]
        resp = json.loads(line.decode())
        if resp.get("error") != "success":
            return None
        return resp.get("data")
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _playback_observer(gtc: subprocess.Popen) -> None:
    """Push mpv pause/time-pos to gotubecast stdin so the Lounge client UI stays in sync."""
    last_pause: Optional[bool] = None
    last_play_generation = 0
    last_alive: bool = False
    last_position_sent: float = 0.0  # time.monotonic() of last position notification
    POSITION_INTERVAL = 5.0          # seconds between periodic position corrections
    while gtc.poll() is None:
        time.sleep(0.25)
        with _mpv_lock:
            gen = _play_generation
            alive = _mpv_proc is not None and _mpv_proc.poll() is None
        if gen != last_play_generation:
            last_play_generation = gen
            last_pause = None
            last_alive = False
            last_position_sent = 0.0
        # Notify gotubecast when mpv exits so the Lounge session is cleared.
        if last_alive and not alive:
            last_alive = False
            try:
                if gtc.stdin and not gtc.stdin.closed:
                    gtc.stdin.write("playback_ended\n")
                    gtc.stdin.flush()
            except (BrokenPipeError, OSError, TypeError, ValueError):
                return
        last_alive = alive
        if not alive:
            last_pause = None
            continue
        if not Path(MPV_SOCKET).exists():
            continue
        pause_raw = mpv_get_property("pause")
        if pause_raw is None:
            continue
        paused = bool(pause_raw)
        now = time.monotonic()
        state_changed = last_pause is None or paused != last_pause
        # Send periodic position corrections during playback so seek drift is resolved.
        periodic_update = not paused and (now - last_position_sent >= POSITION_INTERVAL)
        if not state_changed and not periodic_update:
            continue
        last_pause = paused
        t_raw = mpv_get_property("time-pos")
        try:
            pos = float(t_raw) if t_raw is not None else 0.0
        except (TypeError, ValueError):
            pos = 0.0
        st = "2" if paused else "1"
        line = f"playback_notify {st} {pos:.3f}\n"
        try:
            if gtc.stdin and not gtc.stdin.closed:
                gtc.stdin.write(line)
                gtc.stdin.flush()
                last_position_sent = now
        except (BrokenPipeError, OSError, TypeError, ValueError):
            return

# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------

def get_stream_urls(video_id: str) -> list:
    """Return one or two stream URLs for the given video ID.

    Two URLs are returned when yt-dlp selects separate video + audio streams;
    mpv merges them using --audio-file.
    """
    fmt = (
        f"bestvideo[height<={VIDEO_QUALITY}][ext=mp4]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={VIDEO_QUALITY}]+bestaudio"
        f"/best[height<={VIDEO_QUALITY}]"
        "/best"
    )
    try:
        r = subprocess.run(
            ["yt-dlp", "-g", "-f", fmt, "--no-playlist",
             f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"error yt-dlp: {exc}", flush=True)
        return []
    if r.returncode != 0:
        print(f"error yt-dlp ({video_id}): {r.stderr.strip()}", flush=True)
        return []
    return [u for u in r.stdout.strip().splitlines() if u]


def _vss_to_lang(vss_id: Optional[str]) -> Optional[str]:
    """Map a Lounge vss_id to a yt-dlp subtitle language code.

    The phone identifies the *exact* track it selected with a vss_id like
    ".en.ehkg1hFWq8A" (manual) or "a.en" (auto/ASR); yt-dlp names the same
    track "en-ehkg1hFWq8A" / "en". Many videos expose a manual track under a
    non-standard code (e.g. "en-ehkg1hFWq8A") alongside an auto track under the
    plain "en", so selecting by languageCode alone always grabs the auto one.
    Honouring the vss_id pins the track the user actually picked.
    """
    if not vss_id:
        return None
    body = vss_id[2:] if vss_id.startswith("a.") else vss_id.lstrip(".")
    body = body.replace(".", "-")
    return body or None


def fetch_subtitles(video_id: str, lang: str,
                    vss_id: Optional[str] = None) -> Optional[Path]:
    """Download the subtitle track for video_id into TEMP_DIR.

    Requests the vss_id-derived track first (the exact one the phone selected)
    and falls back to the bare languageCode. Returns the Path of the best
    matching .vtt file, or None.

    YouTube serves WebVTT and mpv reads it natively, so we deliberately do
    *not* convert to .srt: --convert-subs requires ffmpeg, and when ffmpeg is
    absent yt-dlp downloads the .vtt but then exits non-zero on the failed
    conversion, which used to make us discard a perfectly usable subtitle.
    """
    # Preference order: the exact selected track, then the bare language code.
    vss_lang = _vss_to_lang(vss_id)
    wanted: list = []
    for code in (vss_lang, lang):
        if code and code not in wanted:
            wanted.append(code)
    if not wanted:
        return None

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(TEMP_DIR / video_id)

    # Remove stale subtitle files for this video before downloading so we can't
    # accidentally return a leftover track from a previous request.
    for stale in [
        *TEMP_DIR.glob(f"{video_id}.*.srt"),
        *TEMP_DIR.glob(f"{video_id}.*.vtt"),
    ]:
        try:
            stale.unlink()
        except OSError:
            pass

    try:
        subprocess.run(
            ["yt-dlp",
             "--write-subs", "--write-auto-subs",
             "--sub-langs", ",".join(wanted),
             "--sub-format", "vtt/best",
             "--skip-download",
             "--no-playlist",
             "-o", out_tmpl,
             f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    # Select by file presence, not return code: when several langs are
    # requested, a missing one makes yt-dlp exit non-zero even though the
    # track we care about downloaded fine. Honour preference order.
    for code in wanted:
        for ext in ("srt", "vtt"):
            p = TEMP_DIR / f"{video_id}.{code}.{ext}"
            if p.exists():
                return p
    leftovers = (sorted(TEMP_DIR.glob(f"{video_id}.*.srt")) +
                 sorted(TEMP_DIR.glob(f"{video_id}.*.vtt")))
    return leftovers[0] if leftovers else None

# ---------------------------------------------------------------------------
# Playback control
# ---------------------------------------------------------------------------

def _reap_mpv() -> None:
    """Terminate and wait for the current mpv process. Caller must hold _mpv_lock."""
    global _mpv_proc
    if _mpv_proc and _mpv_proc.poll() is None:
        _mpv_proc.terminate()
        try:
            _mpv_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _mpv_proc.kill()
            _mpv_proc.wait()
        _mpv_proc = None


def play_video(video_id: str, start: float = 0.0, paused: bool = False) -> None:
    global _mpv_proc, _play_generation, _active_video_id
    with _mpv_lock:
        _play_generation += 1
        generation = _play_generation
        _active_video_id = None
        _reap_mpv()

        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        for f in TEMP_DIR.glob(f"{video_id}.*"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            Path(MPV_SOCKET).unlink()
        except OSError:
            pass

    # Fetch stream URL and subtitles concurrently (lock released during I/O).
    url_result: list = []
    sub_result: list = []

    def _urls():
        url_result.extend(get_stream_urls(video_id))

    def _subs():
        sub = fetch_subtitles(video_id, SUB_LANG)
        if sub:
            sub_result.append(sub)

    t_url = threading.Thread(target=_urls, daemon=True)
    t_sub = threading.Thread(target=_subs, daemon=True)
    t_url.start()
    t_sub.start()
    t_url.join()
    t_sub.join()

    if not url_result:
        print(f"error No stream URL for {video_id}", flush=True)
        return

    cmd = [
        "mpv",
        f"--input-ipc-server={MPV_SOCKET}",
        "--no-terminal",
        "--force-window=yes",
        "--keep-open=no",
        "--really-quiet",
        "--no-osc",                        # replaced by uosc (installed by setup.sh)
        f"--sub-font-size={SUB_FONT_SIZE}",
        # Listed before MPV_EXTRA so MPV_OPTS can still override either default.
        *(["--fullscreen"] if FULLSCREEN else []),
        *MPV_EXTRA,
    ]

    # Resume at the sender's position / play state. --start seeks before the
    # first frame, so there's no race against mpv's IPC socket coming up (which
    # a post-spawn "seek" command would have).
    if start > 0:
        cmd.append(f"--start={start:.3f}")
    if paused:
        cmd.append("--pause")

    for sub in sub_result:
        cmd.append(f"--sub-file={sub}")

    cmd.append(url_result[0])
    if len(url_result) >= 2:
        # Separate video and audio streams — mpv merges them.
        cmd.append(f"--audio-file={url_result[1]}")

    with _mpv_lock:
        # Abort if a newer play_video() call has already taken over.
        if generation != _play_generation:
            return
        _active_video_id = video_id
        _mpv_proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)


def load_subtitle_track(video_id: str, lang: str, generation: int,
                        vss_id: Optional[str] = None) -> None:
    """Download a subtitle track and sideload it into the running mpv instance."""
    if not lang:
        mpv_ipc({"command": ["set_property", "sid", "no"]})
        return
    with _mpv_lock:
        if generation != _subtitle_generation or video_id != _active_video_id:
            return
    sub = fetch_subtitles(video_id, lang, vss_id)
    if sub:
        with _mpv_lock:
            if generation != _subtitle_generation or video_id != _active_video_id:
                return
        mpv_ipc({"command": ["sub-add", str(sub), "select"]})

# ---------------------------------------------------------------------------
# Command dispatcher
# ---------------------------------------------------------------------------

def dispatch(line: str) -> None:
    global _subtitle_generation, _play_generation, _active_video_id

    parts = line.split()
    if not parts:
        return
    cmd = parts[0]

    if cmd == "pairing_code":
        code = parts[1] if len(parts) > 1 else "?"
        print(f"Pairing code: {code}", flush=True)

    elif cmd == "dial_url":
        url = parts[1] if len(parts) > 1 else ""
        print(f"DIAL: {url}", flush=True)

    elif cmd == "video_id":
        # "video_id <id>"                        → play from start
        # "video_id <id> <start_secs> <paused>"  → resume at offset / state
        vid = parts[1] if len(parts) > 1 else ""
        if vid:
            start = 0.0
            if len(parts) > 2:
                try:
                    start = max(0.0, float(parts[2]))
                except ValueError:
                    start = 0.0
            paused = len(parts) > 3 and parts[3] == "1"
            print(f"Playing: {vid}", flush=True)
            threading.Thread(
                target=play_video, args=(vid, start, paused), daemon=True
            ).start()

    elif cmd == "play":
        mpv_ipc({"command": ["set_property", "pause", False]})

    elif cmd == "pause":
        mpv_ipc({"command": ["set_property", "pause", True]})

    elif cmd == "stop":
        with _mpv_lock:
            _play_generation += 1
            _active_video_id = None
            _reap_mpv()

    elif cmd == "seek_to":
        try:
            mpv_ipc({"command": ["seek", float(parts[1]), "absolute"]})
        except (IndexError, ValueError):
            pass

    elif cmd == "set_volume":
        try:
            # Lounge API volume is 0–100; mpv volume is also 0–100.
            mpv_ipc({"command": ["set_property", "volume", int(parts[1])]})
        except (IndexError, ValueError):
            pass

    elif cmd == "mute":
        mpv_ipc({"command": ["set_property", "mute", True]})

    elif cmd == "unmute":
        mpv_ipc({"command": ["set_property", "mute", False]})

    elif cmd == "set_subtitles":
        # "set_subtitles off"                   → disable
        # "set_subtitles <id> <lang> [vss_id]"  → load track
        if len(parts) >= 3 and parts[1] != "off":
            vss = parts[3] if len(parts) >= 4 else None
            with _mpv_lock:
                _subtitle_generation += 1
                gen = _subtitle_generation
            threading.Thread(
                target=load_subtitle_track,
                args=(parts[1], parts[2], gen, vss), daemon=True
            ).start()
        else:
            with _mpv_lock:
                _subtitle_generation += 1
            mpv_ipc({"command": ["set_property", "sid", "no"]})

    elif cmd == "remote_join":
        print(f"Remote connected: {' '.join(parts[2:])}", flush=True)

    elif cmd == "remote_leave":
        print(f"Remote disconnected: {parts[1] if len(parts) > 1 else ''}", flush=True)

    elif cmd == "next":
        mpv_ipc({"command": ["playlist-next", "force"]})

    elif cmd == "previous":
        mpv_ipc({"command": ["playlist-prev", "force"]})

    elif cmd == "set_playback_rate":
        try:
            mpv_ipc({"command": ["set_property", "speed", float(parts[1])]})
        except (IndexError, ValueError):
            pass

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    gtc_cmd = [
        "gotubecast",
        "-n", SCREEN_NAME,
        "-i", SCREEN_APP,
        "-p", str(DIAL_PORT),
        "-d", GTC_DEBUG_LEVEL,
        "-debug-log-file", GTC_DEBUG_LOG,
    ]
    if GTC_TRACE_PROTOCOL not in ("", "0", "false", "False", "FALSE", "no", "No", "NO"):
        gtc_cmd.append("-trace-protocol")
    if GTC_DEBUG_ROOT_CA:
        gtc_cmd.extend(["-debug-root-ca", GTC_DEBUG_ROOT_CA])
    if SCREEN_ID:
        gtc_cmd.extend(["-s", SCREEN_ID])

    print(f"Starting: {' '.join(gtc_cmd)}", flush=True)

    proc = subprocess.Popen(
        gtc_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    threading.Thread(target=_playback_observer, args=(proc,), daemon=True).start()

    try:
        for line in proc.stdout:
            dispatch(line.rstrip())
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        with _mpv_lock:
            _reap_mpv()


if __name__ == "__main__":
    main()
