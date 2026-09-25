#!/usr/bin/env bash
# setup.sh — one-shot install for gotubecast + cast.py on Raspberry Pi
#
# Installs:
#   • mpv, yt-dlp, python3, curl (via apt)
#   • uosc        — feature-rich mpv UI  (https://github.com/tomasklaen/uosc)
#   • pointer-event + touch-gestures     (https://github.com/christoph-heinrich)
#   • gotubecast binary → ~/.local/bin/gotubecast  (built from source via go)
#   • cast.py          → ~/.local/lib/gotubecast/cast.py
#   • systemd user service gotubecast-cast (enabled, not started)
#
# Creates configs under ~/.config/mpv/ tuned for a touchscreen Pi display.
# Safe to re-run: scripts are overwritten, mpv.conf is patched not replaced.
#
# Update in place (skip apt / mpv / uosc — only rebuild + copy artifacts):
#   bash examples/setup.sh --update
#   bash examples/setup.sh -u
#   bash examples/setup.sh update

set -euo pipefail

UPDATE_ONLY=0
for arg in "$@"; do
    case "${arg}" in
        --update|-u|update) UPDATE_ONLY=1 ;;
        -h|--help)
            echo "Usage: $(basename "$0") [--update|-u]"
            echo "  (no args)  Full install: packages, mpv UI, gotubecast, cast.py, systemd unit."
            echo "  --update   Rebuild gotubecast, reinstall cast.py + service unit, daemon-reload only."
            exit 0
            ;;
    esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MPV_CFG="${HOME}/.config/mpv"
SCRIPTS="${MPV_CFG}/scripts"
OPTS="${MPV_CFG}/script-opts"

if [[ "${UPDATE_ONLY}" -eq 0 ]]; then
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

else
    echo ">>> Update mode: skipping packages and mpv/uosc setup; rebuilding gotubecast + refreshing install paths only."
    git -C "${REPO_DIR}" pull
fi

# ── gotubecast binary ─────────────────────────────────────────────────────────

echo ">>> Building gotubecast..."
mkdir -p "${HOME}/.local/bin"
if command -v go &>/dev/null; then
    (cd "${REPO_DIR}" && go build -o "${HOME}/.local/bin/gotubecast" .)
    echo "    Installed to ~/.local/bin/gotubecast"
else
    echo "    WARNING: go not found — skipping build. Install Go then run:"
    echo "    cd ${REPO_DIR} && go build -o ~/.local/bin/gotubecast ."
fi

# ── cast.py ───────────────────────────────────────────────────────────────────

echo ">>> Installing cast.py..."
CAST_INSTALL="${HOME}/.local/lib/gotubecast"
mkdir -p "${CAST_INSTALL}"
cp "${REPO_DIR}/examples/cast.py" "${CAST_INSTALL}/cast.py"

# ── systemd user service ──────────────────────────────────────────────────────

echo ">>> Installing systemd user service..."
SYSTEMD_USER="${HOME}/.config/systemd/user"
mkdir -p "${SYSTEMD_USER}"
cp "${REPO_DIR}/examples/gotubecast-cast.service" "${SYSTEMD_USER}/gotubecast-cast.service"
if ! systemctl --user daemon-reload 2>/dev/null; then
    echo "    WARNING: systemctl --user daemon-reload failed (no user systemd session?). When logged in, run:"
    echo "            systemctl --user daemon-reload"
fi
if [[ "${UPDATE_ONLY}" -eq 0 ]]; then
    if ! systemctl --user enable gotubecast-cast 2>/dev/null; then
        echo "    WARNING: systemctl --user enable gotubecast-cast failed. When logged in, run:"
        echo "            systemctl --user enable --now gotubecast-cast"
    else
        echo "    Enabled gotubecast-cast (not started — see note below)."
    fi
else
    echo "    Refreshed gotubecast-cast.service (run: systemctl --user daemon-reload already done)."
fi

# ── labwc → systemd session bridge ───────────────────────────────────────────
#
# gotubecast-cast.service is WantedBy=graphical-session.target, but labwc on
# Raspberry Pi OS never activates that target: labwc(1) documents
# labwc-session.target yet the Debian package does not ship the unit, and the
# lightdm→labwc session has no systemd integration (it uses lxsession XDG
# autostart instead). Without this, the service is "enabled" but never starts.

echo ">>> Wiring labwc into the systemd user session..."
cp "${REPO_DIR}/examples/labwc-session.target" "${SYSTEMD_USER}/labwc-session.target"
systemctl --user daemon-reload 2>/dev/null || true

LABWC_CONF="${HOME}/.config/labwc"
mkdir -p "${LABWC_CONF}"

# labwc reads only the FIRST autostart found unless started with -m/--merge-config.
# Raspberry Pi OS uses `labwc -m` (see /usr/bin/labwc-pi), so a user file augments
# the system one. Without -m it would REPLACE it, taking the panel and desktop
# with it — so warn rather than silently break the session.
if ! grep -qs -- '-m\|--merge-config' /usr/bin/labwc-pi 2>/dev/null \
   && [[ -f /etc/xdg/labwc/autostart ]] && [[ ! -f "${LABWC_CONF}/autostart" ]]; then
    echo "    WARNING: labwc does not appear to use --merge-config, and a system"
    echo "             autostart exists at /etc/xdg/labwc/autostart."
    echo "             Copy its contents into ${LABWC_CONF}/autostart as well,"
    echo "             or your panel/desktop will not start."
fi

if ! grep -qs 'labwc-session.target' "${LABWC_CONF}/autostart" 2>/dev/null; then
    cat >> "${LABWC_CONF}/autostart" <<'EOF'

# Activate the systemd user session target so units declaring
# WantedBy=graphical-session.target start in sync with the labwc session.
systemctl --user --no-block start labwc-session.target
EOF
    echo "    Added labwc-session.target activation to ${LABWC_CONF}/autostart"
else
    echo "    ${LABWC_CONF}/autostart already activates labwc-session.target"
fi

if ! grep -qs 'graphical-session.target' "${LABWC_CONF}/shutdown" 2>/dev/null; then
    cat >> "${LABWC_CONF}/shutdown" <<'EOF'

# Tear down graphical-session.target before the Wayland socket goes away,
# so services stop cleanly instead of failing with "Broken pipe".
systemctl --user stop graphical-session.target
EOF
    echo "    Added graphical-session.target teardown to ${LABWC_CONF}/shutdown"
else
    echo "    ${LABWC_CONF}/shutdown already tears down graphical-session.target"
fi

# ── done ─────────────────────────────────────────────────────────────────────

echo ""
if [[ "${UPDATE_ONLY}" -eq 0 ]]; then
    echo "Done!  Gesture summary:"
    echo "  Tap              → pause / unpause"
    echo "  Swipe left/right → seek (or previous/next video)"
    echo "  Swipe up/down (right half) → volume"
    echo "  Long-press       → uosc context menu"
    echo "  ✕ in controls bar → quit mpv"
    echo ""
    echo "Service management:"
    echo "  Start now:   systemctl --user start gotubecast-cast"
    echo "  View logs:   journalctl --user -u gotubecast-cast -f"
    echo "  Customise:   edit ~/.config/gotubecast/env  (SCREEN_NAME, MPV_OPTS, …)"
    echo ""
    echo "For a fullscreen dedicated display:"
    echo "  echo 'MPV_OPTS=--fullscreen' >> ~/.config/gotubecast/env"
    echo "  systemctl --user restart gotubecast-cast"
else
    echo "Update done."
    echo "  gotubecast → ~/.local/bin/gotubecast"
    echo "  cast.py    → ~/.local/lib/gotubecast/cast.py"
    echo "  Restart the service to pick up changes:  systemctl --user restart gotubecast-cast"
fi
