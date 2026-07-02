#!/usr/bin/env python3
"""RPi Controller MQTT2HA Daemon — bridges MQTT commands and system state to Home Assistant."""

import configparser
import glob
import json
import logging
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime

import psutil
import paho.mqtt.client as mqtt

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")


def load_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return cfg


# ── System helpers ────────────────────────────────────────────────────────────

# Check vcgencmd availability once at startup.
VCGENCMD = shutil.which("vcgencmd")


def read_temperature():
    """Return CPU temperature in °C, or None if unavailable."""
    if VCGENCMD:
        try:
            out = subprocess.check_output([VCGENCMD, "measure_temp"], timeout=3, text=True)
            # Format: temp=47.2'C
            return float(out.strip().replace("temp=", "").replace("'C", ""))
        except Exception:
            pass
    # Fallback: thermal_zone0 reports millidegrees on all Pi models including Pi 5.
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None


def read_pi_model():
    """Return Pi model string from device-tree or cpuinfo."""
    try:
        with open("/proc/device-tree/model") as f:
            return f.read().rstrip("\x00").strip()
    except Exception:
        pass
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Model"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return "Unknown"


def read_cpu_model():
    """Return CPU model string."""
    try:
        with open("/proc/cpuinfo") as f:
            content = f.read()
        for line in content.splitlines():
            if line.startswith("model name") or line.startswith("Model name"):
                return line.split(":", 1)[1].strip()
        # ARM chips often lack "model name" — use Hardware field instead.
        for line in content.splitlines():
            if line.startswith("Hardware"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "Unknown"


def read_os_version():
    """Return pretty OS version string."""
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return platform.version()


def read_uptime():
    """Return (uptime_seconds: int, human_readable: str)."""
    with open("/proc/uptime") as f:
        seconds = int(float(f.read().split()[0]))
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return seconds, " ".join(parts)


def read_network_counters():
    """Return psutil net_io_counters dict, skipping loopback and virtual interfaces."""
    skip_prefixes = ("lo", "docker", "veth", "br-", "virbr")
    return {
        iface: stats
        for iface, stats in psutil.net_io_counters(pernic=True).items()
        if not any(iface.startswith(p) for p in skip_prefixes)
    }


def read_pending_updates():
    """Return count of pending apt updates. Slow — always call in a thread."""
    try:
        import apt  # noqa: PLC0415
        cache = apt.Cache()
        cache.upgrade()
        return sum(1 for _ in cache.get_changes())
    except ImportError:
        pass
    except Exception as e:
        log.debug(f"apt update check failed: {e}")
    return -1


def get_hostname():
    return socket.gethostname()


def get_ip():
    """Return primary outbound IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "unknown"


def get_mac():
    """Return MAC address of the first non-loopback interface."""
    try:
        for iface, addrs in psutil.net_if_addrs().items():
            if iface == "lo":
                continue
            for addr in addrs:
                if addr.family == psutil.AF_LINK:
                    mac = addr.address
                    if mac and mac != "00:00:00:00:00:00":
                        return mac
    except Exception:
        pass
    return "unknown"


# ── Brightness controller ─────────────────────────────────────────────────────

class BrightnessController:
    """
    Auto-detects and manages screen brightness.
    Priority: DSI backlight → ddcutil → disabled.
    ddcutil operations run in threads since they take 100-300 ms.
    """

    METHOD_DSI = "dsi"
    METHOD_DDCUTIL = "ddcutil"
    METHOD_NONE = "none"

    def __init__(self, cfg):
        method_cfg = cfg.get("display", "brightness_method", fallback="auto").strip()
        path_cfg = cfg.get("display", "brightness_path", fallback="").strip()
        self._max_value = cfg.getint("display", "brightness_max", fallback=255)
        self._dsi_path = None
        self._method = self.METHOD_NONE
        self._lock = threading.Lock()

        if method_cfg == "none":
            log.info("Brightness: disabled by config")
            return

        if method_cfg in ("auto", "dsi"):
            self._try_dsi(path_cfg)

        if self._method == self.METHOD_NONE and method_cfg in ("auto", "ddcutil"):
            self._try_ddcutil(method_cfg)

        if self._method == self.METHOD_NONE:
            log.info("Brightness: not available on this hardware")

    def _try_dsi(self, path_cfg):
        if path_cfg:
            if os.path.exists(path_cfg):
                self._dsi_path = path_cfg
                self._method = self.METHOD_DSI
                log.info(f"Brightness: using configured DSI path {path_cfg}")
            else:
                log.warning(f"Brightness: configured path {path_cfg} not found")
            return
        # Auto-detect: glob covers both rpi_backlight (Pi 3/4) and rpi_backlight0 (Pi 5).
        paths = glob.glob("/sys/class/backlight/*/brightness")
        if paths:
            self._dsi_path = paths[0]
            self._method = self.METHOD_DSI
            log.info(f"Brightness: detected DSI backlight at {self._dsi_path}")

    def _try_ddcutil(self, method_cfg):
        if not shutil.which("ddcutil"):
            if method_cfg == "ddcutil":
                log.warning("Brightness: ddcutil not installed")
            return
        try:
            result = subprocess.run(
                ["ddcutil", "detect", "--brief"],
                capture_output=True, timeout=5
            )
            if result.returncode == 0 and b"Display" in result.stdout:
                self._method = self.METHOD_DDCUTIL
                log.info("Brightness: using ddcutil")
            else:
                log.warning("Brightness: ddcutil found but no compatible displays detected")
        except Exception as e:
            log.warning(f"Brightness: ddcutil detect failed: {e}")

    @property
    def available(self):
        return self._method != self.METHOD_NONE

    def get(self):
        """Return brightness as 0-100%, or None on failure."""
        try:
            if self._method == self.METHOD_DSI:
                with open(self._dsi_path) as f:
                    raw = int(f.read().strip())
                return round(raw / self._max_value * 100)
            elif self._method == self.METHOD_DDCUTIL:
                return self._ddcutil_get()
        except Exception as e:
            log.warning(f"Brightness read failed: {e}")
        return None

    def set(self, percent):
        """Set brightness from 0-100. Non-blocking — ddcutil runs in a thread."""
        percent = max(0, min(100, int(percent)))
        if self._method == self.METHOD_DSI:
            raw = round(percent / 100 * self._max_value)
            threading.Thread(target=self._dsi_set, args=(raw,), daemon=True).start()
        elif self._method == self.METHOD_DDCUTIL:
            threading.Thread(target=self._ddcutil_set, args=(percent,), daemon=True).start()

    def _dsi_set(self, raw):
        try:
            with self._lock:
                with open(self._dsi_path, "w") as f:
                    f.write(str(raw))
        except Exception as e:
            log.warning(f"DSI brightness write failed: {e}")

    def _ddcutil_get(self):
        try:
            out = subprocess.check_output(
                ["ddcutil", "getvcp", "10", "--brief"], timeout=5, text=True
            )
            # Format: VCP 10 C <current> <max>
            parts = out.strip().split()
            if len(parts) >= 4:
                return int(parts[3])
        except Exception as e:
            log.warning(f"ddcutil getvcp failed: {e}")
        return None

    def _ddcutil_set(self, percent):
        try:
            with self._lock:
                subprocess.run(
                    ["ddcutil", "setvcp", "10", str(percent)],
                    timeout=10, check=True
                )
        except Exception as e:
            log.warning(f"ddcutil setvcp failed: {e}")


# ── Display on/off ────────────────────────────────────────────────────────────

class DisplayController:
    """Executes configurable shell commands to power the display on or off."""

    def __init__(self, cfg):
        self._on_cmd = cfg.get("display", "on_command",
                                fallback="xset -display :0 dpms force on")
        self._off_cmd = cfg.get("display", "off_command",
                                 fallback="xset -display :0 dpms force off")
        self._env = {**os.environ, "DISPLAY": ":0"}

    def turn_on(self):
        self._run(self._on_cmd)

    def turn_off(self):
        self._run(self._off_cmd)

    def _run(self, cmd):
        try:
            subprocess.run(cmd, shell=True, env=self._env, timeout=5, check=True)
        except Exception as e:
            log.warning(f"Display command failed ({cmd!r}): {e}")


# ── MQTT discovery ────────────────────────────────────────────────────────────

def _device_payload(cfg, hostname, mac, model, os_ver):
    name = cfg.get("device", "name", fallback=hostname)
    location = cfg.get("device", "location", fallback="").strip()
    dev = {
        "identifiers": [f"rpi2ha_{hostname}", mac.replace(":", "")],
        "name": name,
        "model": model,
        "manufacturer": "Raspberry Pi Ltd",
        "sw_version": os_ver,
    }
    if location:
        dev["suggested_area"] = location
    return dev


def publish_discovery(client, cfg, hostname, mac, model, os_ver, brightness_available, net_ifaces):
    """Publish all HA MQTT discovery payloads for sensors, switches, and buttons."""
    prefix = cfg.get("mqtt", "discovery_prefix", fallback="homeassistant")
    base = cfg.get("mqtt", "base_topic", fallback="rpi2ha")
    name = cfg.get("device", "name", fallback=hostname)
    dev = _device_payload(cfg, hostname, mac, model, os_ver)
    monitor_topic = f"{base}/{hostname}/monitor"
    avail_topic = f"{base}/{hostname}/status"

    availability = [{
        "topic": avail_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
    }]

    def pub(component, object_id, payload):
        topic = f"{prefix}/{component}/rpi2ha_{hostname}/{object_id}/config"
        client.publish(topic, json.dumps(payload), retain=True)

    def sensor(key, label, dev_class=None, unit=None, icon=None):
        p = {
            "name": f"{name} {label}",
            "unique_id": f"rpi2ha_{hostname}_{key}",
            "state_topic": monitor_topic,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "availability": availability,
            "device": dev,
        }
        if dev_class:
            p["device_class"] = dev_class
        if unit:
            p["unit_of_measurement"] = unit
        if icon:
            p["icon"] = icon
        pub("sensor", key, p)

    # ── Sensors ───────────────────────────────────────────────────────────────

    sensor("temperature_c",       "Temperature",     "temperature", "°C",  "mdi:thermometer")
    sensor("cpu_load_1min_prcnt", "CPU Load 1min",   None,          "%",   "mdi:cpu-64-bit")
    sensor("cpu_load_5min_prcnt", "CPU Load 5min",   None,          "%",   "mdi:cpu-64-bit")
    sensor("mem_used_prcnt",      "Memory Used",     None,          "%",   "mdi:memory")
    sensor("fs_used_prcnt",       "Disk Used",       None,          "%",   "mdi:harddisk")
    sensor("fs_disk_used",        "Disk Used MB",    None,          "MB",  "mdi:harddisk")
    sensor("uptime_sec",          "Uptime",          "duration",    "s",   "mdi:clock-outline")
    sensor("uptime",              "Uptime Human",    None,          None,  "mdi:clock-outline")
    sensor("last_update",         "Last Update",     "timestamp",   None,  "mdi:update")
    sensor("hostname",            "Hostname",        None,          None,  "mdi:server")
    sensor("fqdn",                "FQDN",            None,          None,  "mdi:server-network")
    sensor("ip_addr",             "IP Address",      None,          None,  "mdi:ip-network")
    sensor("mac_addr",            "MAC Address",     None,          None,  "mdi:ethernet")
    sensor("os_version",          "OS Version",      None,          None,  "mdi:linux")
    sensor("os_kernel",           "Kernel",          None,          None,  "mdi:linux")
    sensor("rpi_model",           "Pi Model",        None,          None,  "mdi:raspberry-pi")
    sensor("cpu_model",           "CPU Model",       None,          None,  "mdi:chip")
    sensor("ux_updates",          "Pending Updates", None,          None,  "mdi:package-up")

    # Per-interface network sensors
    for iface in net_ifaces:
        safe = iface.replace("-", "_").replace(".", "_")
        sensor(f"tx_{safe}", f"TX {iface}", None, "MB/s", "mdi:upload-network")
        sensor(f"rx_{safe}", f"RX {iface}", None, "MB/s", "mdi:download-network")

    # ── Display sensors ───────────────────────────────────────────────────────

    sensor("display_state", "Display State", None, None, "mdi:monitor")

    if brightness_available:
        sensor("display_brightness", "Display Brightness", None, "%", "mdi:brightness-6")

    # ── Display switch (independent power control) ────────────────────────────

    pub("switch", "display", {
        "name": f"{name} Screen Power",
        "unique_id": f"rpi2ha_{hostname}_display_switch",
        "state_topic": f"{base}/{hostname}/display/state",
        "command_topic": f"{base}/{hostname}/display/set",
        "payload_on": "ON",
        "payload_off": "OFF",
        "availability": availability,
        "device": dev,
        "icon": "mdi:monitor",
    })

    # ── Brightness number (independent slider, only if hardware supports it) ──

    if brightness_available:
        pub("number", "brightness", {
            "name": f"{name} Brightness",
            "unique_id": f"rpi2ha_{hostname}_brightness",
            "state_topic": f"{base}/{hostname}/brightness/state",
            "command_topic": f"{base}/{hostname}/brightness/set",
            "min": 0,
            "max": 100,
            "step": 1,
            "unit_of_measurement": "%",
            "availability": availability,
            "device": dev,
            "icon": "mdi:brightness-6",
        })

    # ── Buttons ───────────────────────────────────────────────────────────────

    buttons = [
        ("reboot",           "Reboot",           "restart",     "mdi:restart"),
        ("shutdown",         "Shutdown",          "identify",    "mdi:power"),
        ("refresh_browser",  "Refresh Browser",   None,          "mdi:web-refresh"),
        ("restart_service",  "Restart Service",   "restart",     "mdi:refresh"),
    ]
    for cmd_id, label, dev_class, icon in buttons:
        p = {
            "name": f"{name} {label}",
            "unique_id": f"rpi2ha_{hostname}_{cmd_id}",
            "command_topic": f"{base}/{hostname}/command/{cmd_id}",
            "availability": availability,
            "device": dev,
            "icon": icon,
        }
        if dev_class:
            p["device_class"] = dev_class
        pub("button", cmd_id, p)


# ── Sensor collector ──────────────────────────────────────────────────────────

class SensorCollector:
    """Collects all sensor values. Slow operations (apt) run in separate threads."""

    def __init__(self, model, os_ver, hostname):
        self._model = model
        self._os_ver = os_ver
        self._hostname = hostname
        self._cpu_model = read_cpu_model()
        self._fqdn = socket.getfqdn()
        self._pending_updates = -1
        self._updates_lock = threading.Lock()
        self._prev_net = read_network_counters()
        self._prev_net_time = time.monotonic()
        self._net_lock = threading.Lock()

    def net_ifaces(self):
        """Return current non-virtual interface names."""
        return list(self._prev_net.keys())

    def refresh_pending_updates(self):
        """Fetch apt update count. Call in a thread — can take several seconds."""
        result = read_pending_updates()
        with self._updates_lock:
            self._pending_updates = result
        log.debug(f"Pending updates refreshed: {result}")

    def collect(self, display_state, brightness_pct):
        """Return full sensor dict for the monitor topic."""
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        load1, load5, _ = psutil.getloadavg()
        cpu_count = psutil.cpu_count() or 1
        uptime_sec, uptime_str = read_uptime()

        # Calculate per-interface network rates since last collection.
        now_net = read_network_counters()
        now_time = time.monotonic()
        net_rates = {}
        with self._net_lock:
            elapsed = now_time - self._prev_net_time
            if elapsed > 0:
                for iface in now_net:
                    if iface in self._prev_net:
                        tx = (now_net[iface].bytes_sent - self._prev_net[iface].bytes_sent) / elapsed / 1_000_000
                        rx = (now_net[iface].bytes_recv - self._prev_net[iface].bytes_recv) / elapsed / 1_000_000
                        safe = iface.replace("-", "_").replace(".", "_")
                        net_rates[f"tx_{safe}"] = round(max(0.0, tx), 4)
                        net_rates[f"rx_{safe}"] = round(max(0.0, rx), 4)
            self._prev_net = now_net
            self._prev_net_time = now_time

        with self._updates_lock:
            pending = self._pending_updates

        data = {
            "temperature_c": read_temperature(),
            "cpu_load_1min_prcnt": round(load1 / cpu_count * 100, 1),
            "cpu_load_5min_prcnt": round(load5 / cpu_count * 100, 1),
            "mem_used_prcnt": round(mem.percent, 1),
            "fs_used_prcnt": round(disk.percent, 1),
            "fs_disk_used": disk.used // (1024 * 1024),
            "uptime_sec": uptime_sec,
            "uptime": uptime_str,
            "last_update": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "hostname": self._hostname,
            "fqdn": self._fqdn,
            "ip_addr": get_ip(),
            "mac_addr": get_mac(),
            "os_version": self._os_ver,
            "os_kernel": platform.release(),
            "rpi_model": self._model,
            "cpu_model": self._cpu_model,
            "ux_updates": pending,
            "display_state": display_state,
            "display_brightness": brightness_pct,
        }
        data.update(net_rates)
        return data


# ── Daemon ────────────────────────────────────────────────────────────────────

class Daemon:
    def __init__(self):
        self._cfg = load_config()
        self._hostname = (
            self._cfg.get("device", "id", fallback="").strip() or get_hostname()
        )
        self._base = self._cfg.get("mqtt", "base_topic", fallback="rpi2ha")
        self._report_interval = max(60, self._cfg.getint("system", "report_interval", fallback=60))
        self._display_state = "ON"  # Screen is on when the Pi boots.
        self._state_lock = threading.Lock()
        self._display = DisplayController(self._cfg)
        self._brightness = BrightnessController(self._cfg)
        self._model = read_pi_model()
        self._os_ver = read_os_version()
        self._mac = get_mac()
        self._collector = SensorCollector(self._model, self._os_ver, self._hostname)
        self._running = False
        self._report_count = 0
        self._client = self._build_mqtt_client()

    # ── MQTT setup ────────────────────────────────────────────────────────────

    def _build_mqtt_client(self):
        client = mqtt.Client(client_id=f"rpi2ha_{self._hostname}", clean_session=True)

        user = self._cfg.get("mqtt", "user", fallback="").strip()
        password = self._cfg.get("mqtt", "password", fallback="").strip()
        if user:
            client.username_pw_set(user, password)

        # LWT publishes "offline" automatically if the connection drops.
        client.will_set(f"{self._base}/{self._hostname}/status", "offline", retain=True)

        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        return client

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error(f"MQTT connect failed (rc={rc})")
            return
        log.info("MQTT connected")

        # Subscribe to all incoming command topics.
        cmd_base = f"{self._base}/{self._hostname}"
        topics = [
            (f"{cmd_base}/display/set", 0),
            (f"{cmd_base}/brightness/set", 0),
            (f"{cmd_base}/command/reboot", 0),
            (f"{cmd_base}/command/shutdown", 0),
            (f"{cmd_base}/command/refresh_browser", 0),
            (f"{cmd_base}/command/restart_service", 0),
        ]
        client.subscribe(topics)

        # Republish discovery and availability on reconnect.
        self._client.publish(f"{self._base}/{self._hostname}/status", "online", retain=True)
        publish_discovery(
            self._client, self._cfg, self._hostname, self._mac,
            self._model, self._os_ver, self._brightness.available,
            self._collector.net_ifaces(),
        )
        log.info("Discovery payloads published")

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            log.warning(f"MQTT disconnected unexpectedly (rc={rc}), paho will reconnect")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        log.info(f"Command received: {topic} → {payload!r}")
        cmd_base = f"{self._base}/{self._hostname}"

        if topic == f"{cmd_base}/display/set":
            self._handle_display(payload)
        elif topic == f"{cmd_base}/brightness/set":
            self._handle_brightness(payload)
        elif topic == f"{cmd_base}/command/reboot":
            self._handle_system("reboot")
        elif topic == f"{cmd_base}/command/shutdown":
            self._handle_system("shutdown")
        elif topic == f"{cmd_base}/command/refresh_browser":
            self._handle_refresh_browser()
        elif topic == f"{cmd_base}/command/restart_service":
            self._handle_restart_service()

    # ── Command handlers ───────────────────────────────────────────────────────

    def _handle_display(self, payload):
        if payload == "ON":
            self._display.turn_on()
            with self._state_lock:
                self._display_state = "ON"
        elif payload == "OFF":
            self._display.turn_off()
            with self._state_lock:
                self._display_state = "OFF"
        else:
            log.warning(f"Unknown display payload: {payload!r}")
            return
        self._publish_display_state()

    def _handle_brightness(self, payload):
        try:
            pct = max(0, min(100, int(float(payload))))
        except ValueError:
            log.warning(f"Invalid brightness payload: {payload!r}")
            return
        if not self._brightness.available:
            return
        self._brightness.set(pct)
        # Publish the commanded value immediately (optimistic); hardware update is async.
        self._client.publish(
            f"{self._base}/{self._hostname}/brightness/state", str(pct), retain=True
        )

    def _handle_system(self, action):
        if action == "reboot":
            log.info("Executing system reboot")
            self._publish_offline()
            subprocess.Popen(["sudo", "reboot"])
        elif action == "shutdown":
            log.info("Executing system shutdown")
            self._publish_offline()
            subprocess.Popen(["sudo", "shutdown", "-h", "now"])

    def _handle_refresh_browser(self):
        log.info("Refreshing Chromium browser")
        env = {**os.environ, "DISPLAY": ":0"}
        try:
            subprocess.run(["pkill", "-f", "chromium"], timeout=5)
            time.sleep(2)
            subprocess.Popen(
                ["chromium-browser", "--kiosk", "--noerrdialogs", "--disable-infobars"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log.warning(f"Browser refresh failed: {e}")

    def _handle_restart_service(self):
        log.info("Restarting daemon service")
        self._publish_offline()
        subprocess.Popen(["sudo", "systemctl", "restart", "rpi-controller-mqtt2ha.service"])

    # ── Publishing ─────────────────────────────────────────────────────────────

    def _publish_offline(self):
        self._client.publish(
            f"{self._base}/{self._hostname}/status", "offline", retain=True
        )

    def _publish_display_state(self):
        with self._state_lock:
            state = self._display_state
        self._client.publish(
            f"{self._base}/{self._hostname}/display/state", state, retain=True
        )

    def _publish_brightness_state(self):
        if not self._brightness.available:
            return
        pct = self._brightness.get()
        if pct is not None:
            self._client.publish(
                f"{self._base}/{self._hostname}/brightness/state", str(pct), retain=True
            )

    def _publish_sensors(self):
        with self._state_lock:
            display_state = self._display_state
        brightness_pct = self._brightness.get() if self._brightness.available else None
        data = self._collector.collect(display_state, brightness_pct)
        self._client.publish(
            f"{self._base}/{self._hostname}/monitor", json.dumps(data), retain=True
        )
        log.debug("Sensor report published")

    # ── Report loop ───────────────────────────────────────────────────────────

    def _report_loop(self):
        """Runs in a dedicated thread. Publishes sensor data every report_interval seconds."""
        while self._running:
            self._report_count += 1

            # Refresh apt update count on first report and every 10 reports after.
            if self._report_count == 1 or self._report_count % 10 == 0:
                threading.Thread(
                    target=self._collector.refresh_pending_updates,
                    daemon=True,
                ).start()

            self._publish_sensors()
            self._publish_display_state()
            self._publish_brightness_state()

            # Sleep in 100 ms chunks so SIGTERM exits quickly.
            for _ in range(self._report_interval * 10):
                if not self._running:
                    break
                time.sleep(0.1)

    # ── Startup / shutdown ────────────────────────────────────────────────────

    def start(self):
        log.info(f"RPi Controller MQTT2HA Daemon starting (hostname={self._hostname})")
        log.info(f"Pi model: {self._model}")
        log.info(f"Report interval: {self._report_interval}s")
        log.info(f"Brightness method: {self._brightness._method}")

        host = self._cfg.get("mqtt", "host", fallback="localhost")
        port = self._cfg.getint("mqtt", "port", fallback=1883)

        log.info(f"Connecting to MQTT broker at {host}:{port}")
        self._client.connect(host, port, keepalive=60)
        self._client.loop_start()

        # Wait for on_connect to fire (handles discovery + online publish).
        for _ in range(100):
            if self._client.is_connected():
                break
            time.sleep(0.1)
        else:
            log.error("Could not connect to MQTT broker within 10 seconds — exiting")
            sys.exit(1)

        self._running = True
        report_thread = threading.Thread(target=self._report_loop, daemon=True)
        report_thread.start()

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        log.info("Daemon running")
        report_thread.join()

    def _handle_signal(self, signum, frame):
        log.info(f"Signal {signum} received — shutting down")
        self._running = False
        self._publish_offline()
        self._client.loop_stop()
        self._client.disconnect()
        sys.exit(0)


if __name__ == "__main__":
    Daemon().start()
