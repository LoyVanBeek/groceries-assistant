#!/usr/bin/env bash
# Install groceries-assistant as a systemd service that supervises
# `docker compose up`. All app dependencies (tesseract, xvfb, chromium) are
# baked into the image; the only host requirement is docker + compose v2.
#
#   sudo ./deploy/setup.sh
#
# Re-running is safe: it re-builds the image, re-renders the unit file and
# re-enables the service. Afterwards manage the app with systemctl and read
# the logs with: sudo journalctl -u groceries-assistant.service -f
set -euo pipefail

fail() { echo "error: $*" >&2; exit 1; }

# Root-privileged steps; a no-op wrapper when the script already runs as root
# (sudo ./deploy/setup.sh).
run_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO_DIR/deploy/groceries-assistant.service"
UNIT_DST="/etc/systemd/system/groceries-assistant.service"

[ -f "$UNIT_SRC" ] || fail "$UNIT_SRC not found (run from the repo checkout)"

# --- host checks: docker + compose v2, nothing else -------------------------
command -v docker >/dev/null 2>&1 || fail "docker not found on the host. Install Docker first (https://docs.docker.com/engine/install/) and re-run."

docker compose version >/dev/null 2>&1 || fail "docker compose v2 not found. Install the compose plugin (e.g. 'sudo apt-get install docker-compose-v2') and re-run."

if ! run_root docker info >/dev/null 2>&1; then
    echo "docker daemon not reachable; starting docker.service..."
    run_root systemctl start docker || fail "could not start the docker daemon"
fi

# --- build image, install unit, enable --------------------------------------
# The unit itself runs as root: on hosts where docker needs sudo (rootful
# daemon, no docker-group membership) a non-root unit cannot reach the docker
# socket, and the container runs as root regardless, so the app gains nothing.
echo "building the image (docker compose build)..."
cd "$REPO_DIR"
run_root docker compose build

echo "rendering $UNIT_SRC -> $UNIT_DST (dir: $REPO_DIR)"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
sed -e "s|@REPO_DIR@|$REPO_DIR|g" "$UNIT_SRC" >"$tmp"
run_root install -m 644 "$tmp" "$UNIT_DST"

run_root systemctl daemon-reload
run_root systemctl enable --now groceries-assistant.service

echo
echo "groceries-assistant is installed and running:"
echo "  web UI : http://localhost:8901"
echo "  status : sudo systemctl status groceries-assistant.service"
echo "  logs   : sudo journalctl -u groceries-assistant.service -f"
echo "  stop   : sudo systemctl stop groceries-assistant.service"
echo "Use systemctl from now on; running 'docker compose up -d' by hand would"
echo "fight the service over the same containers."
