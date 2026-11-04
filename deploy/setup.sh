#!/usr/bin/env bash
# Install groceries-assistant as a systemd service that supervises
# `docker compose up`. All app dependencies (tesseract, xvfb, chromium) are
# baked into the image; the only host requirement is docker + compose v2.
#
#   sudo ./deploy/setup.sh
#
# Re-running is safe: it re-builds the image, re-renders the unit file and
# re-enables the service. Afterwards manage the app with systemctl and read
# the logs with: journalctl -u groceries-assistant.service -f
set -euo pipefail

fail() { echo "error: $*" >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO_DIR/deploy/groceries-assistant.service"
UNIT_DST="/etc/systemd/system/groceries-assistant.service"

# Privileged steps need root; the service itself runs as the invoking user.
if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
else
    SUDO="sudo"
fi
RUN_USER="${SUDO_USER:-$(id -un)}"
[ "$RUN_USER" != "root" ] || fail "run as your own user (sudo ./deploy/setup.sh), not as root: the service must run under your account"

as_run_user() { # run a command with the service user's privileges
    if [ "$(id -u)" -eq 0 ]; then sudo -u "$RUN_USER" "$@"; else "$@"; fi
}

[ -f "$UNIT_SRC" ] || fail "$UNIT_SRC not found (run from the repo checkout)"

# --- host checks: docker + compose v2, nothing else -------------------------
command -v docker >/dev/null 2>&1 || fail "docker not found on the host. Install Docker first (https://docs.docker.com/engine/install/) and re-run."

docker compose version >/dev/null 2>&1 || fail "docker compose v2 not found. Install the compose plugin (e.g. 'sudo apt-get install docker-compose-v2') and re-run."

"$SUDO" docker info >/dev/null 2>&1 || {
    echo "docker daemon not reachable; starting docker.service..."
    "$SUDO" systemctl start docker || fail "could not start the docker daemon"
}

# The unit runs docker as $RUN_USER, which needs the 'docker' group. systemd
# re-reads group membership when it starts the service, so no re-login is
# needed for the service itself (only for your own shell).
if ! as_run_user docker info >/dev/null 2>&1; then
    if id -nG "$RUN_USER" | grep -qw docker; then
        fail "docker daemon unreachable for user $RUN_USER even though they are in the docker group"
    fi
    echo "adding $RUN_USER to the docker group (the service runs docker without root)"
    "$SUDO" usermod -aG docker "$RUN_USER"
    as_run_user docker info >/dev/null 2>&1 || fail "docker still unreachable for $RUN_USER after adding them to the docker group"
    echo "note: your own shell needs a re-login before 'docker' works directly; the systemd service does not."
fi

# --- build image, install unit, enable --------------------------------------
echo "building the image (docker compose build)..."
cd "$REPO_DIR"
as_run_user docker compose build

echo "rendering $UNIT_SRC -> $UNIT_DST (user: $RUN_USER, dir: $REPO_DIR)"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
sed -e "s|@REPO_DIR@|$REPO_DIR|g" -e "s|@USER@|$RUN_USER|g" "$UNIT_SRC" >"$tmp"
"$SUDO" install -m 644 "$tmp" "$UNIT_DST"

"$SUDO" systemctl daemon-reload
"$SUDO" systemctl enable --now groceries-assistant.service

echo
echo "groceries-assistant is installed and running:"
echo "  web UI : http://localhost:8901"
echo "  status : systemctl status groceries-assistant.service"
echo "  logs   : journalctl -u groceries-assistant.service -f"
echo "  stop   : sudo systemctl stop groceries-assistant.service"
echo "Use systemctl from now on; running 'docker compose up -d' by hand would"
echo "fight the service over the same containers."
