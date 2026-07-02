#!/usr/bin/env bash
# Install script for RPi Controller MQTT2HA Daemon

set -e

INSTALL_DIR="/opt/rpi-controller-mqtt2ha"
SERVICE_NAME="rpi-controller-mqtt2ha"
SERVICE_FILE="rpi-controller-mqtt2ha.service"

echo "=== RPi Controller MQTT2HA Daemon — Installer ==="
echo ""

# ── System dependencies ───────────────────────────────────────────────────────

echo "Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-psutil python3-apt

# ── Optional: ddcutil for HDMI brightness control ─────────────────────────────

echo ""
read -rp "Install ddcutil for HDMI display brightness control? [y/N] " INSTALL_DDCUTIL
if [[ "$INSTALL_DDCUTIL" =~ ^[Yy]$ ]]; then
    echo "Installing ddcutil..."
    sudo apt-get install -y ddcutil
    echo "Enabling I2C interface..."
    sudo raspi-config nonint do_i2c 0
    sudo usermod -aG i2c "$(whoami)"
    echo ""
    echo "NOTE: I2C was just enabled. A reboot is required before ddcutil will work."
    NEEDS_REBOOT=1
fi

# ── Python packages ───────────────────────────────────────────────────────────

echo ""
echo "Installing Python packages..."
pip3 install --user -r "$(dirname "$0")/requirements.txt"

# ── Install files ─────────────────────────────────────────────────────────────

echo ""
echo "Installing to ${INSTALL_DIR}..."
sudo mkdir -p "$INSTALL_DIR"
sudo cp daemon.py "$INSTALL_DIR/"
sudo cp requirements.txt "$INSTALL_DIR/"

if [[ ! -f "${INSTALL_DIR}/config.ini" ]]; then
    sudo cp config.example.ini "${INSTALL_DIR}/config.ini"
    echo "Created config.ini from example — edit it before starting the service."
else
    echo "Existing config.ini left untouched."
fi

sudo chmod +x "${INSTALL_DIR}/daemon.py"

# ── systemd service ───────────────────────────────────────────────────────────

echo ""
echo "Installing systemd service..."
sudo cp "$SERVICE_FILE" "/etc/systemd/system/${SERVICE_FILE}"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl start "$SERVICE_NAME"

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit config:  sudo nano ${INSTALL_DIR}/config.ini"
echo "  2. Restart:      sudo systemctl restart ${SERVICE_NAME}"
echo "  3. View logs:    journalctl -u ${SERVICE_NAME} -f"
echo ""

if [[ -n "$NEEDS_REBOOT" ]]; then
    echo "IMPORTANT: Reboot required for I2C (ddcutil) to work."
    echo "  sudo reboot"
    echo ""
fi

# ── Sudoers ───────────────────────────────────────────────────────────────────

SUDOERS_FILE="/etc/sudoers.d/rpi-controller-mqtt2ha"
if [[ ! -f "$SUDOERS_FILE" ]]; then
    echo "Adding passwordless sudo rules for service commands..."
    cat <<EOF | sudo tee "$SUDOERS_FILE" > /dev/null
# Allow the kiosk user to run these commands without a password
kiosk ALL=(ALL) NOPASSWD: /sbin/reboot
kiosk ALL=(ALL) NOPASSWD: /sbin/shutdown
kiosk ALL=(ALL) NOPASSWD: /bin/systemctl restart rpi-controller-mqtt2ha.service
EOF
    sudo chmod 0440 "$SUDOERS_FILE"
    echo "Sudoers rules written to ${SUDOERS_FILE}"
fi
