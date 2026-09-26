#!/usr/bin/env bash
# install-local.sh — build and deploy gotubecast from the current working tree.
#
# Unlike setup.sh --update, this never touches git, so it deploys your local
# (possibly uncommitted) changes straight to the installed locations:
#   • gotubecast binary → ~/.local/bin/gotubecast
#   • cast.py           → ~/.local/lib/gotubecast/cast.py
#   • service unit      → ~/.config/systemd/user/gotubecast-cast.service
# Then daemon-reloads and restarts the user service.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
LIB_DIR="${HOME}/.local/lib/gotubecast"
SYSTEMD_USER="${HOME}/.config/systemd/user"

echo ">>> Building gotubecast from ${REPO_DIR}..."
mkdir -p "${BIN_DIR}"
(cd "${REPO_DIR}" && go build -o "${BIN_DIR}/gotubecast" .)
echo "    → ${BIN_DIR}/gotubecast"

echo ">>> Installing cast.py..."
mkdir -p "${LIB_DIR}"
cp "${REPO_DIR}/examples/cast.py" "${LIB_DIR}/cast.py"
echo "    → ${LIB_DIR}/cast.py"

echo ">>> Installing service unit..."
mkdir -p "${SYSTEMD_USER}"
cp "${REPO_DIR}/examples/gotubecast-cast.service" "${SYSTEMD_USER}/gotubecast-cast.service"
echo "    → ${SYSTEMD_USER}/gotubecast-cast.service"

echo ">>> Reloading and restarting service..."
systemctl --user daemon-reload
systemctl --user restart gotubecast-cast
echo "    Restarted gotubecast-cast."

echo ""
echo "Done. Follow logs with:"
echo "  journalctl --user -u gotubecast-cast -f"
