#!/usr/bin/env python3
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
    GTC_TRACE_PROTOCOL Enable protocol trace logging (0/1)   (default: 0)
    SUB_LANG        Subtitle language code, e.g. "en"        (default: en, blank to disable)
    VIDEO_QUALITY   Maximum video height in pixels           (default: 1080)
    MPV_OPTS        Extra mpv CLI options (space-separated)
                    e.g. MPV_OPTS=--fullscreen for a dedicated display

Usage:
    python3 cast.py
"""

import json
import os
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

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
VIDEO_QUALITY = os.environ.get("VIDEO_QUALITY", "1080")
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

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_mpv_proc = None
_mpv_lock = threading.Lock()
_play_generation     = 0   # incremented on each play_video(); guards stale fetches
_subtitle_generation = 0   # incremented on each subtitle change; guards stale loads

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


def fetch_subtitles(video_id: str, lang: str) -> Path | None:
    """Download the subtitle file for video_id / lang into TEMP_DIR.

    Prefers manual captions; falls back to auto-generated ones.
    Returns the Path of the downloaded .srt file, or None.
    """
    if not lang:
        return None
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(TEMP_DIR / video_id)

    # Remove stale files for this language before downloading so we can't
    # accidentally return a leftover track from a previous request.
    for stale in [
        *TEMP_DIR.glob(f"{video_id}.{lang}*.srt"),
        *TEMP_DIR.glob(f"{video_id}.{lang}*.vtt"),
    ]:
        try:
            stale.unlink()
        except OSError:
            pass

    try:
        result = subprocess.run(
            ["yt-dlp",
             "--write-subs", "--write-auto-subs",
             "--sub-lang", lang,
             "--sub-format", "vtt/best",
             "--convert-subs", "srt",
             "--skip-download",
             "--no-playlist",
             "-o", out_tmpl,
             f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    candidates = (
        sorted(TEMP_DIR.glob(f"{video_id}.{lang}*.srt")) +
        sorted(TEMP_DIR.glob(f"{video_id}.{lang}*.vtt"))
    )
    return candidates[0] if candidates else None

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


def play_video(video_id: str) -> None:
    global _mpv_proc, _play_generation
    with _mpv_lock:
        _play_generation += 1
        generation = _play_generation
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
        *MPV_EXTRA,
    ]

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
        _mpv_proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)


def load_subtitle_track(video_id: str, lang: str, generation: int) -> None:
    """Download a subtitle track and sideload it into the running mpv instance."""
    if not lang:
        mpv_ipc({"command": ["set_property", "sid", "no"]})
        return
    sub = fetch_subtitles(video_id, lang)
    if sub:
        with _mpv_lock:
            # Abort if a newer subtitle selection has superseded this one.
            if generation != _subtitle_generation:
                return
        mpv_ipc({"command": ["sub-add", str(sub), "select"]})

# ---------------------------------------------------------------------------
# Command dispatcher
# ---------------------------------------------------------------------------

def dispatch(line: str) -> None:
    global _subtitle_generation

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
        vid = parts[1] if len(parts) > 1 else ""
        if vid:
            print(f"Playing: {vid}", flush=True)
            threading.Thread(target=play_video, args=(vid,), daemon=True).start()

    elif cmd == "play":
        mpv_ipc({"command": ["set_property", "pause", False]})

    elif cmd == "pause":
        mpv_ipc({"command": ["set_property", "pause", True]})

    elif cmd == "stop":
        with _mpv_lock:
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
        # "set_subtitles off"            → disable
        # "set_subtitles <id> <lang>"    → load track
        if len(parts) >= 3 and parts[1] != "off":
            with _mpv_lock:
                _subtitle_generation += 1
                gen = _subtitle_generation
            threading.Thread(
                target=load_subtitle_track, args=(parts[1], parts[2], gen), daemon=True
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
    if SCREEN_ID:
        gtc_cmd.extend(["-s", SCREEN_ID])

    print(f"Starting: {' '.join(gtc_cmd)}", flush=True)

    proc = subprocess.Popen(
        gtc_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    try:
        for line in proc.stdout:
            dispatch(line.rstrip())
        for line in proc.stderr:
            print(line)
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
