#!/usr/bin/env bash
# Pull the latest version from git and redeploy the running daemon.

set -e

INSTALL_DIR="/opt/rpi-controller-mqtt2ha"
SERVICE_NAME="rpi-controller-mqtt2ha"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== RPi Controller MQTT2HA Daemon — Updater ==="
echo ""

# ── Pull latest ───────────────────────────────────────────────────────────────

echo "Pulling latest changes..."
git -C "$REPO_DIR" pull

# ── Copy daemon ───────────────────────────────────────────────────────────────

echo "Deploying daemon.py..."
sudo cp "${REPO_DIR}/daemon.py" "${INSTALL_DIR}/daemon.py"

# ── Update Python packages if requirements changed ────────────────────────────

if ! diff -q "${REPO_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt" > /dev/null 2>&1; then
    echo "requirements.txt changed — updating packages..."
    sudo cp "${REPO_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"
    sudo "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" --quiet
else
    echo "requirements.txt unchanged — skipping pip install."
fi

# ── Restart service ───────────────────────────────────────────────────────────

echo "Restarting service..."
sudo systemctl restart "$SERVICE_NAME"

echo ""
echo "Done. Logs: journalctl -u ${SERVICE_NAME} -f"
