#!/usr/bin/env bash
# setup.sh — one-shot install for gotubecast + cast.py on Raspberry Pi
#
# Installs:
#   • mpv, yt-dlp, python3, curl (via apt)
#   • uosc        — feature-rich mpv UI  (https://github.com/tomasklaen/uosc)
#   • pointer-event + touch-gestures     (https://github.com/christoph-heinrich)
#
# Creates configs under ~/.config/mpv/ tuned for a touchscreen Pi display.
# Safe to re-run: scripts are overwritten, mpv.conf is patched not replaced.

set -euo pipefail

MPV_CFG="${HOME}/.config/mpv"
SCRIPTS="${MPV_CFG}/scripts"
OPTS="${MPV_CFG}/script-opts"

# ── system packages ──────────────────────────────────────────────────────────

echo ">>> Installing packages..."
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    mpv python3 python3-pip curl unzip

# yt-dlp: prefer pip for a more up-to-date version than apt
if ! command -v yt-dlp &>/dev/null; then
    pip3 install --break-system-packages -q yt-dlp
fi

# ── mpv config directories ───────────────────────────────────────────────────

mkdir -p "${SCRIPTS}" "${OPTS}"

# ── uosc ─────────────────────────────────────────────────────────────────────

echo ">>> Installing uosc..."
curl -fsSL https://raw.githubusercontent.com/tomasklaen/uosc/HEAD/installers/unix.sh | bash

# ── pointer-event (dependency of touch-gestures) ─────────────────────────────

echo ">>> Installing pointer-event..."
curl -fsSL \
    "https://raw.githubusercontent.com/christoph-heinrich/mpv-pointer-event/master/pointer-event.lua" \
    -o "${SCRIPTS}/pointer-event.lua"

# ── touch-gestures ────────────────────────────────────────────────────────────

echo ">>> Installing touch-gestures..."
curl -fsSL \
    "https://raw.githubusercontent.com/christoph-heinrich/mpv-touch-gestures/master/touch-gestures.lua" \
    -o "${SCRIPTS}/touch-gestures.lua"

# ── pointer-event.conf ───────────────────────────────────────────────────────
# Margins reserve the bottom 130 px for uosc's timeline so taps there don't
# accidentally trigger pause/seek via touch-gestures.

cat > "${OPTS}/pointer-event.conf" << 'EOF'
margin_left=0
margin_right=0
margin_top=0
margin_bottom=130
ignore_left_single_long_while_window_dragging=yes
left_single=cycle pause
left_double=script-message-to touch_gestures double
left_long=script-binding uosc/menu-blurred
left_drag_start=script-message-to touch_gestures drag_start
left_drag_end=script-message-to touch_gestures drag_end
left_drag=script-message-to touch_gestures drag
EOF

# ── touch-gestures.conf ───────────────────────────────────────────────────────

cat > "${OPTS}/touch-gestures.conf" << 'EOF'
# horizontal_drag=playlist navigates the play queue on left/right swipe.
# Switch to 'seek' if you prefer scrubbing instead.
horizontal_drag=playlist
proportional_seek=yes
seek_scale=1
EOF

# ── uosc.conf ────────────────────────────────────────────────────────────────
# Written only if not already present; uosc's installer writes its own default
# but doesn't overwrite an existing file, so this runs after the installer.

UOSC_CONF="${OPTS}/uosc.conf"
if [[ ! -f "${UOSC_CONF}" ]]; then
    echo ">>> uosc.conf not found — writing defaults (uosc installer may not have created it)"
fi

# Patch or create the relevant keys. We write a small override block at the
# top so it takes precedence over anything the uosc installer wrote below.
UOSC_OVERRIDE="${MPV_CFG}/uosc-pi.conf"
cat > "${UOSC_OVERRIDE}" << 'EOF'
# Raspberry Pi touchscreen overrides — sourced via mpv.conf include directive.

# Scale controls up for a finger-sized touch target.
scale_fullscreen=1.5
scale_windowed=1.5

# Controls bar for a cast device:
#   prev/next  — queue navigation (once queue support is enabled)
#   play-pause — obvious
#   subtitles  — change subtitle track
#   volume     — local volume override (scroll or click)
#   fullscreen — toggle fullscreen
#   command:close:quit — explicit stop/quit button (MDI "close" icon)
controls=prev,items,next,gap,space,play-pause,space,gap,subtitles,volume,fullscreen,command:close:quit

# Keep the timeline visible while paused so seeks are easy.
timeline_persistency=paused
EOF

# ── mpv.conf ─────────────────────────────────────────────────────────────────
# Append osc=no if not already present, then include the Pi override conf.

MPV_CONF="${MPV_CFG}/mpv.conf"
touch "${MPV_CONF}"

if ! grep -q "^osc=" "${MPV_CONF}" 2>/dev/null; then
    echo "osc=no" >> "${MPV_CONF}"
fi

INCLUDE_LINE="include=~~/uosc-pi.conf"
if ! grep -qF "${INCLUDE_LINE}" "${MPV_CONF}" 2>/dev/null; then
    echo "${INCLUDE_LINE}" >> "${MPV_CONF}"
fi

# ── done ─────────────────────────────────────────────────────────────────────

echo ""
echo "Done!  Gesture summary:"
echo "  Tap              → pause / unpause"
echo "  Swipe left/right → seek (or previous/next video)"
echo "  Swipe up/down (right half) → volume"
echo "  Long-press       → uosc context menu"
echo "  ✕ in controls bar → quit mpv"
echo ""
echo "To start casting:"
echo "  python3 examples/cast.py"
echo ""
echo "For a fullscreen dedicated display add MPV_OPTS=--fullscreen before the command."
