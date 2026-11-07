#!/usr/bin/env bash
# Update a running deployment: pull the latest master, rebuild the image and
# restart the service. Run on the server, in the checkout:
#
#   sudo ./deploy/update.sh
#
# (setup.sh is only needed once; this is the everyday deploy.)
set -euo pipefail

fail() { echo "error: $*" >&2; exit 1; }

run_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

echo "pulling the latest master..."
git pull --ff-only || fail "git pull failed (lokaal gewijzigde bestanden op de server?)"

echo "rebuilding the image (met de huidige commit-hash als versie-indicator)..."
GIT_HASH="$(git rev-parse --short HEAD)"
export GIT_HASH
run_root docker compose build

echo "restarting the service..."
run_root systemctl restart groceries-assistant.service

echo
echo "bijgewerkt: $(git log --oneline -1)"
echo "  web  : http://localhost:8901"
echo "  logs : sudo journalctl -u groceries-assistant.service -f"
