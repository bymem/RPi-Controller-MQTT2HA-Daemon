# RPi Controller MQTT2HA Daemon

A lightweight Python daemon that bridges MQTT and local system commands on a Raspberry Pi kiosk. It exposes controls and sensors to Home Assistant via MQTT discovery — no manual HA configuration needed.

Inspired by [ironsheep/RPi-Reporter-MQTT2HA-Daemon](https://github.com/ironsheep/RPi-Reporter-MQTT2HA-Daemon) for the sensor side, with added kiosk-specific controls (display power, brightness, browser refresh).

Compatible with Raspberry Pi 3B+, 4, and 5.

---

## Features

- **Sensors**: temperature, CPU, memory, disk, uptime, network, OS info, pending updates
- **Controls**: screen on/off, brightness, browser refresh, reboot, shutdown, restart service
- **Auto-discovery**: appears as a device in Home Assistant automatically
- **Lightweight**: designed to run alongside Chromium on a Pi 3B+

---

## Requirements

- Raspberry Pi OS (Bullseye or later recommended)
- Python 3
- `paho-mqtt`, `psutil` (installed by `install.sh`)
- `python3-apt` (for pending update count — available by default on Pi OS)
- MQTT broker (e.g. Mosquitto on your Home Assistant host)

---

## Install

```bash
git clone https://github.com/bymem/RPi-Controller-MQTT2HA-Daemon ~/rpi-controller-mqtt2ha
cd ~/rpi-controller-mqtt2ha
chmod +x install.sh
./install.sh
```

The installer will:
1. Install system and Python dependencies
2. Optionally install `ddcutil` for HDMI brightness control
3. Copy files to `/opt/rpi-controller-mqtt2ha/`
4. Create `config.ini` from the example (if it doesn't exist)
5. Install and start the systemd service
6. Add passwordless sudo rules for reboot/shutdown/restart

---

## Configuration

Edit `/opt/rpi-controller-mqtt2ha/config.ini`:

```ini
[mqtt]
host = 192.168.1.100
port = 1883
user = kiosk
password = yourpassword
discovery_prefix = homeassistant
base_topic = rpi2ha

[device]
name = Touch Panel
location = Living Room

[display]
on_command = xset -display :0 dpms force on
off_command = xset -display :0 dpms force off
brightness_method = auto   # auto | dsi | ddcutil | none
brightness_path =           # leave empty for auto-detect
brightness_max = 255

[system]
report_interval = 60        # seconds, minimum 60
```

After editing, restart the service:

```bash
sudo systemctl restart rpi-controller-mqtt2ha
```

---

## Brightness

Three methods, tried in order when `brightness_method = auto`:

| Method | Hardware | Notes |
|--------|----------|-------|
| `dsi` | Official Raspberry Pi touchscreen | Reads/writes `/sys/class/backlight/*/brightness` |
| `ddcutil` | HDMI monitors with DDC/CI | Requires `ddcutil` installed and I2C enabled |
| `none` | — | Brightness entity not advertised to HA |

For **DSI on Pi 5**, the path is `/sys/class/backlight/rpi_backlight0/brightness` (note the `0`). Auto-detect handles this via glob.

For **ddcutil**, I2C must be enabled:
```bash
sudo raspi-config nonint do_i2c 0
sudo usermod -aG i2c kiosk
sudo reboot
```

---

## Browser refresh

The **Refresh Browser** button runs a configurable shell command. Set it to whatever restarts your kiosk:

```ini
[browser]
refresh_command = systemctl --user restart kiosk.service
```

Any shell command works — `systemctl`, a custom script, `pkill`, whatever your setup needs. Leave `refresh_command` empty (or remove the option) to disable the button entirely so it won't appear in HA.

---

## Display commands (X11 vs Wayland)

The `on_command` and `off_command` are just shell strings — swap them for your compositor:

| Setup | on_command | off_command |
|-------|-----------|------------|
| X11 (default) | `xset -display :0 dpms force on` | `xset -display :0 dpms force off` |
| Wayland / labwc | `wlopm --on \*` | `wlopm --off \*` |

---

## MQTT topics

All topics are prefixed with `{base_topic}/{hostname}/` (e.g. `rpi2ha/touchpanel/`).

### Published by the Pi

| Topic | Value |
|-------|-------|
| `…/monitor` | JSON blob with all sensor values |
| `…/status` | `online` / `offline` (LWT) |
| `…/display/state` | `ON` / `OFF` |
| `…/brightness/state` | `0`–`100` |

### Subscribed by the Pi

| Topic | Payload |
|-------|---------|
| `…/display/set` | `ON` / `OFF` |
| `…/brightness/set` | `0`–`100` |
| `…/command/reboot` | any |
| `…/command/shutdown` | any |
| `…/command/refresh_browser` | any |
| `…/command/restart_service` | any |

---

## Sudoers

The service user (`kiosk`) needs passwordless sudo for a few commands. The installer writes this automatically, but if you need to do it manually:

```bash
sudo visudo -f /etc/sudoers.d/rpi-controller-mqtt2ha
```

```
kiosk ALL=(ALL) NOPASSWD: /sbin/reboot
kiosk ALL=(ALL) NOPASSWD: /sbin/shutdown
kiosk ALL=(ALL) NOPASSWD: /bin/systemctl restart rpi-controller-mqtt2ha.service
```

---

## Logs

```bash
journalctl -u rpi-controller-mqtt2ha -f
```

---

## Updating

Pull the latest version and redeploy in one command:

```bash
cd ~/rpi-controller-mqtt2ha
./update.sh
```

This pulls from git, copies the updated daemon to `/opt/`, reinstalls Python packages if `requirements.txt` changed, and restarts the service.

---

## Service management

```bash
sudo systemctl start rpi-controller-mqtt2ha
sudo systemctl stop rpi-controller-mqtt2ha
sudo systemctl restart rpi-controller-mqtt2ha
sudo systemctl status rpi-controller-mqtt2ha
```
