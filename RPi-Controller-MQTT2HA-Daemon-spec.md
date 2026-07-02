# RPi Controller MQTT2HA Daemon — Project Spec

## Overview

A lightweight Python daemon that bridges MQTT and local system commands on a Raspberry Pi kiosk device. It exposes controls and sensors to Home Assistant via MQTT discovery, making the kiosk a proper HA device.

Designed to run alongside a plain Chromium kiosk on Raspberry Pi OS Lite + X11. Inspired by [ironsheep/RPi-Reporter-MQTT2HA-Daemon](https://github.com/ironsheep/RPi-Reporter-MQTT2HA-Daemon) for the reporting side, but adds kiosk-specific controls (display, brightness, browser) that the original doesn't have.

---

## Goals

- Receive commands from HA via MQTT and execute them locally
- Report Pi system state back to HA via MQTT (matching ironsheep sensor set)
- Auto-discover as a device in HA (no manual HA config needed)
- **Compatible with Raspberry Pi 3B+, 4, and 5** — use Pi APIs where available, always fall back gracefully
- Be lightweight — runs alongside Chromium on even a Pi 3B+, every MB and CPU cycle counts
- Run as a systemd service, start on boot, restart on failure

---

## Tech Stack

- **Language**: Python 3
- **MQTT**: `paho-mqtt`
- **System stats**: `psutil`
- **Config**: Single `config.ini` file (no hardcoded values)
- **Service**: systemd unit file included

---

## Configuration

All configuration via `config.ini`, nothing hardcoded:

```ini
[mqtt]
host = 192.168.10.20
port = 1883
user = kiosk
password = yourpassword
discovery_prefix = homeassistant
base_topic = rpi2ha

[device]
name = Touch Panel
location = Living Room
# id is auto-derived from hostname if not set

[display]
# X11 commands (default). Swap for Wayland: wlopm --on \* / wlopm --off \*
on_command = xset -display :0 dpms force on
off_command = xset -display :0 dpms force off
# Brightness method: auto | dsi | ddcutil | none
# auto = try DSI backlight first, fall back to ddcutil, then disable
brightness_method = auto
# Only needed if auto-detect picks the wrong DSI path
brightness_path =
brightness_max = 255

[system]
# How often to report system stats in seconds (min 60)
report_interval = 60
```

---

## Sensors (Reported to HA)

Matches the ironsheep RPi-Reporter-MQTT2HA-Daemon sensor set:

| Sensor | Value | Unit | Notes |
|--------|-------|------|-------|
| `temperature_c` | float | °C | GPU temp via `vcgencmd measure_temp` (Pi 3/4), `/sys/class/thermal/thermal_zone0/temp` fallback (Pi 5 + all models) |
| `cpu_load_1min_prcnt` | float | % | 1-minute load average |
| `cpu_load_5min_prcnt` | float | % | 5-minute load average |
| `mem_used_prcnt` | float | % | RAM used % |
| `fs_used_prcnt` | float | % | Root filesystem used % |
| `fs_disk_used` | int | MB | Root filesystem used MB |
| `uptime_sec` | int | s | Uptime in seconds |
| `uptime` | string | — | Human readable uptime (e.g. "2d 4h 32m") |
| `last_update` | timestamp | — | ISO8601 timestamp of last report |
| `hostname` | string | — | Pi hostname |
| `fqdn` | string | — | Fully qualified domain name |
| `ip_addr` | string | — | Primary IP address |
| `mac_addr` | string | — | Primary MAC address |
| `os_version` | string | — | e.g. "Debian GNU/Linux 12 (bookworm)" |
| `os_kernel` | string | — | Kernel version string |
| `rpi_model` | string | — | e.g. "Raspberry Pi 4 Model B Rev 1.4" |
| `cpu_model` | string | — | CPU model string |
| `tx_data` | float | MB/s | Network transmit per interface |
| `rx_data` | float | MB/s | Network receive per interface |
| `ux_updates` | int | — | Pending apt package updates (-1 if unavailable) |

### Kiosk-specific sensors

| Sensor | Value | Unit | Notes |
|--------|-------|------|-------|
| `display_state` | `ON`/`OFF` | — | Current screen power state |
| `display_brightness` | int | % | Current brightness 0-100 (if supported) |

---

## Controls (Commands from HA)

### Display

| Entity | Type | Payload | Action |
|--------|------|---------|--------|
| Screen Power | Switch | `ON` / `OFF` | Runs `on_command` / `off_command` from config |
| Brightness | Light (brightness) | `0-255` | Writes to `brightness_path` in config |

### Browser

| Entity | Type | Payload | Action |
|--------|------|---------|--------|
| Refresh Browser | Button | — | Kills and relaunches Chromium |

### System

| Entity | Type | Payload | Action |
|--------|------|---------|--------|
| Reboot | Button | — | `sudo reboot` |
| Shutdown | Button | — | `sudo shutdown -h now` |
| Restart Service | Button | — | `sudo systemctl restart rpi-controller-mqtt2ha.service` |

---

## MQTT Topics

All topics follow the pattern: `{base_topic}/{hostname}/`

### State (published — Pi → HA)

```
rpi2ha/{hostname}/monitor          ← JSON blob with all sensor values
rpi2ha/{hostname}/status           ← "online" / "offline" (LWT)
rpi2ha/{hostname}/display/state    ← "ON" / "OFF"
rpi2ha/{hostname}/brightness/state ← 0-100
```

### Commands (subscribed — HA → Pi)

```
rpi2ha/{hostname}/display/set      ← "ON" / "OFF"
rpi2ha/{hostname}/brightness/set   ← 0-100
rpi2ha/{hostname}/command/reboot
rpi2ha/{hostname}/command/shutdown
rpi2ha/{hostname}/command/refresh_browser
rpi2ha/{hostname}/command/restart_service
```

---

## HA MQTT Discovery

On startup publish discovery payloads for all entities, grouped under a single HA device:

- Device name from `config.ini`
- Device identifiers: hostname + MAC
- Device model: auto-detected from `/proc/cpuinfo` or `vcgencmd`
- Manufacturer: "Raspberry Pi Ltd"
- SW version: OS version string

All sensors reference `~/monitor` JSON topic with `val_tpl` to extract individual values, matching the ironsheep pattern exactly so the same Lovelace cards work.

---

## Behavior

- **On startup**: publish discovery payloads → set LWT → report initial state → start reporting loop
- **On command received**: execute → publish updated state immediately
- **Every `report_interval` seconds**: collect and publish full sensor JSON to `monitor` topic
- **On shutdown/crash**: LWT publishes `offline` automatically (set on MQTT connect)
- **Display state on boot**: always publishes `ON` on startup (screen is on when Pi boots)
- **Brightness**: gracefully skip if `brightness_path` doesn't exist or isn't writable — log warning, don't crash

---

## Project Structure

```
RPi-Controller-MQTT2HA-Daemon/
├── daemon.py          # Main daemon
├── config.ini             # Local config (gitignored)
├── config.example.ini     # Example config (committed)
├── rpi-controller-mqtt2ha.service     # systemd unit file
├── install.sh             # Install script
├── requirements.txt       # paho-mqtt, psutil
└── README.md
```

---

## Install Script

`install.sh` should:
1. Install system deps: `python3-pip python3-psutil`
2. `pip3 install -r requirements.txt`
3. Optionally install `ddcutil` if HDMI brightness is wanted (prompt user)
4. If ddcutil selected: enable i2c (`sudo raspi-config nonint do_i2c 0`) and add user to `i2c` group
5. Copy `config.example.ini` → `config.ini` if not present
6. Copy service file to `/etc/systemd/system/`
7. `systemctl daemon-reload && systemctl enable rpi-controller-mqtt2ha && systemctl start rpi-controller-mqtt2ha`
8. Print reminder to edit `config.ini` and reboot if i2c was just enabled

---

## Notes for Implementation

### Pi Model Compatibility (3B+, 4, 5)

Every Pi-specific API call must have a fallback. Never crash on missing features — log a warning and continue.

**Temperature**
- Primary: `vcgencmd measure_temp` — available on Pi 3/4, may not be on Pi 5 without firmware update
- Fallback: `/sys/class/thermal/thermal_zone0/temp` — works on all models including Pi 5
- Parse both formats gracefully (`temp=47.2'C` vs raw integer in millidegrees)

**Pi Model detection**
- Primary: `/proc/device-tree/model` — works on all Pi models (e.g. `Raspberry Pi 4 Model B Rev 1.4`)
- Fallback: parse `Model` field from `/proc/cpuinfo`
- Fallback: return `"Unknown"` — never crash

**vcgencmd availability**
- Check once at startup with `shutil.which('vcgencmd')`
- If not found, skip all vcgencmd calls for the entire session and use fallbacks
- Pi 5 ships vcgencmd but some paths differ — don't assume it works even if present, wrap every call in try/except

**Backlight / Brightness**

Three methods in priority order — auto-detected at startup, first working method wins:

1. **DSI backlight** (Pi 3/4/5 with official touchscreen)
   - Pi 3/4: `/sys/class/backlight/rpi_backlight/brightness` (max 255)
   - Pi 5: `/sys/class/backlight/rpi_backlight0/brightness` (note the `0`)
   - Glob `/sys/class/backlight/*/brightness` to auto-detect

2. **DDC/CI via ddcutil** (HDMI displays)
   - Requires `ddcutil` installed (`sudo apt install ddcutil`)
   - Requires i2c enabled (`dtparam=i2c_arm=on` in `/boot/firmware/config.txt`)
   - Requires service user in `i2c` group (`sudo usermod -aG i2c <user>`)
   - Set brightness: `ddcutil setvcp 10 <0-100>` (VCP code 10 = brightness)
   - Read brightness: `ddcutil getvcp 10 --brief` → parse `VCP 10 C 75 100` → current=75
   - Check availability at startup: `ddcutil detect --brief` — if no displays found or command fails, skip
   - Monitor must support DDC/CI — not guaranteed, but most modern monitors do
   - Slow (~100-300ms per command) — run in a thread, never block the main loop

3. **Not available** — brightness entity not advertised to HA at all, no broken switches in the UI

Config should allow forcing a method or path in case auto-detect fails:
```ini
[display]
# brightness_method = auto | dsi | ddcutil | none
brightness_method = auto
# Only needed if auto-detect picks wrong DSI path
brightness_path =
brightness_max = 255
```

**Display on/off commands**
- X11 (all models): `xset -display :0 dpms force on/off` — requires `DISPLAY=:0`
- Wayland/labwc (all models): `wlopm --on \*` / `wlopm --off \*`
- Direct backlight (all models): `echo 0 > /sys/class/backlight/*/bl_power`
- All three are configurable strings in `config.ini` — the daemon just executes whatever is configured

**Network interfaces**
- Pi 3: `wlan0` / `eth0`
- Pi 4/5: same, but sometimes `end0` for ethernet on Pi 5
- Use `psutil.net_io_counters(pernic=True)` and skip loopback (`lo`) and virtual interfaces (`docker`, `veth`, `br-`)
- Report all remaining interfaces, not just the first one

**Memory**
- `psutil.virtual_memory()` works on all models
- `vcgencmd get_mem arm` / `vcgencmd get_mem gpu` for GPU split — only if vcgencmd available, otherwise skip

**Pending apt updates**
- Use `python3-apt` library (`import apt`) — available on all Pi OS versions
- Wrap in try/except ImportError — return `-1` if not available
- This is slow — run in a thread or only update every N intervals, not every report cycle

### General

- Read Pi model from `/proc/device-tree/model` or `/proc/cpuinfo`
- Network stats: report per-interface tx/rx using psutil, skip loopback and virtual interfaces
- All sudo commands require passwordless sudo for the service user — document clearly in README
- Service runs as the kiosk user (same user running Chromium) so `DISPLAY=:0` is accessible
- On Pi 3 with limited RAM: keep memory footprint minimal, avoid loading large libraries at startup

---

## Out of Scope (for now)

- Screenshot capture
- Volume/audio control
- Camera streams
- GPIO/hardware sensor integration
- Multi-display support
