import glob
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from datetime import date, datetime
from pathlib import Path

from flask import jsonify, Response, stream_with_context

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("seedbox")

DEFAULT_TIMEOUT = 30
APP_HOST = os.environ.get("SEEDBOX_APP_HOST", "0.0.0.0").strip() or "0.0.0.0"
APP_PORT = int(os.environ.get("SEEDBOX_APP_PORT", "5000").strip() or "5000")

MOUNT_POINT = "/mnt/exstore"
COMPOSE_DIR = os.path.expanduser("~/seedbox")
SAMBA_SERVICE = "smbd"
WEBUI_PORT = 8080
JELLYFIN_PORT = 8096
WETTY_PORT = 7681
FILEBROWSER_PORT = 8097
NEKO_PORT = 8095
AVISTAZ_IP = "143.244.42.67"
SMB_CONF = "/etc/samba/smb.conf"
DOWNLOAD_SCRIPT = os.path.expanduser("~/download.sh")
MOUNT_POINT_BASE = "/mnt/exstore"
VPN_PROXY = "socks5://127.0.0.1:10800"
MAX_OUTPUT_LINES = 600
KEEP_OUTPUT_LINES = 450
SERVER_START_TIME = time.time()
APP_LOG_FILE = os.path.expanduser("~/seedbox/seedbox.log")


def _state_dir():
    preferred = Path(os.environ.get("SEEDBOX_STATE_DIR", str(Path.home() / ".seedbox-state")))
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except Exception:
        fallback = Path.cwd() / ".seedbox-state"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


STATE_DIR = _state_dir()
COMMAND_LOG_FILE = STATE_DIR / "command_log.jsonl"

PRESET_DIRS = [
    {"label": "exstore (root)", "path": "/mnt/exstore"},
    {"label": "TVShows", "path": "/mnt/exstore/TVShows"},
    {"label": "TVShows/Torrent", "path": "/mnt/exstore/TVShows/Torrent"},
    {"label": "Movies", "path": "/mnt/exstore/Movies"},
    {"label": "Music", "path": "/mnt/exstore/Music"},
    {"label": "Downloads", "path": "/mnt/exstore/Downloads"},
    {"label": "Home (~)", "path": os.path.expanduser("~")},
]

PRIV_HELPER = "/usr/local/bin/seedbox-root-helper"
PRIV_HINT = (
    "Privileged helper is not configured for the dashboard. "
    "Re-run install.sh to install the root helper and sudoers rule."
)

ALLOWED_RUN_COMMANDS = {"ls", "df", "du", "systemctl", "docker", "cat", "ps"}


def strip_ansi(text):
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text or "")


def human_size(size):
    try:
        size = int(size)
    except Exception:
        return "--"
    if size > 1024 ** 3:
        return "{:.2f} GB".format(size / 1024 ** 3)
    if size > 1024 ** 2:
        return "{:.1f} MB".format(size / 1024 ** 2)
    if size > 1024:
        return "{:.1f} KB".format(size / 1024)
    return "{} B".format(size)


def safe_path(path):
    path = os.path.normpath((path or "").strip())
    if not os.path.isabs(path):
        raise ValueError("Path must be absolute")
    for char in ["\x00", ";", "&", "|", "`", "$", "(", ")", "<", ">", "\\"]:
        if char in path:
            raise ValueError("Illegal character in path: {!r}".format(char))
    return path


def has_tool(tool_name):
    return shutil.which(tool_name) is not None


def legacy_error_payload(message, **extra):
    payload = {"success": False, "error": message, "ok": False, "msg": message}
    payload.update(extra)
    return payload


def json_error(message, status=400, **extra):
    return jsonify(legacy_error_payload(message, **extra)), status


def run(cmd, timeout=DEFAULT_TIMEOUT, shell=True, env=None, cwd=None):
    try:
        result = subprocess.run(
            cmd,
            shell=shell,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", "timed out", 124
    except Exception as exc:
        return "", str(exc), 1


def _line_matches(line, filter_text=None, search_text=None):
    if filter_text and filter_text not in line:
        return False
    if search_text and search_text.lower() not in line.lower():
        return False
    return True


def run_stream(cmd, timeout=DEFAULT_TIMEOUT, shell=True, env=None, cwd=None, filter_text=None, search_text=None):
    try:
        proc = subprocess.Popen(
            cmd,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=cwd,
            bufsize=1,
        )
    except Exception as exc:
        yield "data: {}\n\n".format(json.dumps("ERROR: {}".format(exc)))
        yield "data: {}\n\n".format(json.dumps("__DONE__"))
        return

    timed_out = {"value": False}

    def _kill_proc():
        timed_out["value"] = True
        try:
            proc.kill()
        except Exception:
            pass

    timer = threading.Timer(timeout, _kill_proc)
    timer.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            clean = strip_ansi(line.rstrip())
            if not clean:
                continue
            if _line_matches(clean, filter_text, search_text):
                yield "data: {}\n\n".format(json.dumps(clean))
        proc.wait()
        if timed_out["value"]:
            yield "data: {}\n\n".format(json.dumps("ERROR: timed out after {}s".format(timeout)))
        yield "data: {}\n\n".format(json.dumps("__DONE__"))
    finally:
        timer.cancel()


def _privileged_cmd(*args):
    helper = PRIV_HELPER if os.path.exists(PRIV_HELPER) else shutil.which("seedbox-root-helper")
    if not helper:
        return None
    cmd = "{} {}".format(
        shlex.quote(helper),
        " ".join(shlex.quote(str(arg)) for arg in args),
    ).strip()
    if os.geteuid() != 0:
        cmd = "sudo -n " + cmd
    return cmd


def privileged_run(*args, timeout=DEFAULT_TIMEOUT):
    cmd = _privileged_cmd(*args)
    if not cmd:
        return "", PRIV_HINT, 1
    out, err, rc = run(cmd, timeout=timeout)
    combined = strip_ansi("\n".join(x for x in [err, out] if x).strip())
    if rc != 0:
        if (
            "a password is required" in combined
            or "terminal is required" in combined
            or "sudo:" in combined
            or ("seedbox-root-helper" in combined and "not found" in combined)
        ):
            return "", PRIV_HINT, rc
    return out, err, rc


def get_cpu():
    try:
        def sample():
            with open("/proc/stat") as handle:
                values = handle.readline().split()[1:]
            return int(values[0]) + int(values[2]), sum(int(x) for x in values)

        busy1, total1 = sample()
        time.sleep(0.5)
        busy2, total2 = sample()
        delta = total2 - total1
        return round(100 * (busy2 - busy1) / delta, 1) if delta > 0 else 0.0
    except Exception:
        return 0.0


def get_ram():
    try:
        out, _, _ = run("free -m | awk '/^Mem:/{print $2,$3,$4}'")
        total, used, free = map(int, out.split())
        return {"total": total, "used": used, "free": free, "pct": round(100 * used / total, 1)}
    except Exception:
        return {"total": 0, "used": 0, "free": 0, "pct": 0}


def get_swap():
    try:
        out, _, _ = run("free -m | awk '/^Swap:/{print $2,$3}'")
        parts = out.split()
        if len(parts) >= 2:
            total, used = int(parts[0]), int(parts[1])
            return {"total": total, "used": used, "pct": round(100 * used / total, 1) if total > 0 else 0}
    except Exception:
        pass
    return {"total": 0, "used": 0, "pct": 0}


def get_temps():
    temps = {}
    seen = set()

    def add_temp(name, value):
        if value is None:
            return
        try:
            value = round(float(value), 1)
        except Exception:
            return
        clean_name = str(name or "").strip() or "temp"
        if value <= 0 or value > 150:
            return
        if clean_name in seen:
            return
        temps[clean_name] = value
        seen.add(clean_name)

    out, _, rc = run("sensors 2>/dev/null | grep -E '(Core|Package|temp|Tdie|Tctl)' | head -10")
    if rc == 0 and out:
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) == 2:
                name = parts[0].strip()
                value = re.sub(r"[^\d.]", "", parts[1].strip().split()[0])
                add_temp(name, value)
    if not temps:
        for hwmon_path in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            hwmon = Path(hwmon_path)
            try:
                base_name = (hwmon / "name").read_text().strip()
            except Exception:
                base_name = hwmon.name
            for input_path in sorted(hwmon.glob("temp*_input")):
                try:
                    raw_value = input_path.read_text().strip()
                    temp_c = int(raw_value) / 1000
                except Exception:
                    continue
                label_path = Path(str(input_path).replace("_input", "_label"))
                try:
                    label = label_path.read_text().strip()
                except Exception:
                    label = ""
                sensor_name = label or "{} {}".format(base_name, input_path.stem.replace("_input", "")).strip()
                add_temp(sensor_name, temp_c)
                if len(temps) >= 10:
                    break
            if len(temps) >= 10:
                break
    if not temps:
        for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            try:
                zone_dir = Path(path).parent
                zone_type_path = zone_dir / "type"
                zone_name = zone_type_path.read_text().strip() if zone_type_path.exists() else zone_dir.name
                add_temp(zone_name, int(Path(path).read_text().strip()) / 1000)
            except Exception:
                pass
    return temps


def get_net_io():
    try:
        def read_net():
            rx = tx = 0
            with open("/proc/net/dev") as handle:
                for line in handle:
                    if ":" not in line:
                        continue
                    iface, data = line.split(":", 1)
                    if iface.strip() == "lo":
                        continue
                    values = data.split()
                    rx += int(values[0])
                    tx += int(values[8])
            return rx, tx

        rx1, tx1 = read_net()
        time.sleep(0.5)
        rx2, tx2 = read_net()
        return {"rx_kb": round((rx2 - rx1) * 2 / 1024, 1), "tx_kb": round((tx2 - tx1) * 2 / 1024, 1)}
    except Exception:
        return {"rx_kb": 0, "tx_kb": 0}


def get_disk_io():
    try:
        def read_disk():
            read_bytes = write_bytes = 0
            with open("/proc/diskstats") as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) >= 14 and re.match(r"^(sd[a-z]|nvme\d+n\d+|mmcblk\d+|hd[a-z])$", parts[2]):
                        read_bytes += int(parts[5]) * 512
                        write_bytes += int(parts[9]) * 512
            return read_bytes, write_bytes

        read1, write1 = read_disk()
        time.sleep(0.5)
        read2, write2 = read_disk()
        return {"read_kb": round((read2 - read1) * 2 / 1024, 1), "write_kb": round((write2 - write1) * 2 / 1024, 1)}
    except Exception:
        return {"read_kb": 0, "write_kb": 0}


def get_live_power_watts():
    for path in ["/sys/class/power_supply/BAT0/power_now", "/sys/class/power_supply/BAT1/power_now"]:
        try:
            value = int(Path(path).read_text().strip())
            if value > 0:
                return round(value / 1_000_000, 2)
        except Exception:
            pass
    for battery in ["BAT0", "BAT1"]:
        try:
            current = int(Path("/sys/class/power_supply/{}/current_now".format(battery)).read_text().strip())
            voltage = int(Path("/sys/class/power_supply/{}/voltage_now".format(battery)).read_text().strip())
            if current > 0 and voltage > 0:
                return round((current / 1e6) * (voltage / 1e6), 2)
        except Exception:
            pass
    out, _, rc = run(
        "upower -i $(upower -e | grep battery | head -1) 2>/dev/null | grep 'energy-rate'",
        timeout=5,
    )
    if rc == 0 and out:
        match = re.search(r"([\d.]+)\s*W", out)
        if match:
            value = float(match.group(1))
            if value > 0:
                return round(value, 2)
    return None


def get_system_uptime_seconds():
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except Exception:
        return time.time() - SERVER_START_TIME


def get_battery():
    def _extract_number(value):
        if value is None:
            return None
        match = re.search(r"(-?[\d.]+)", str(value))
        if not match:
            return None
        try:
            return float(match.group(1))
        except Exception:
            return None

    result = {
        "present": False,
        "pct": 0,
        "state": "",
        "charging": False,
        "health": 0,
        "watts": 0,
        "native_path": "",
        "vendor": "",
        "model": "",
        "serial": "",
        "updated": "",
        "warning_level": "",
        "technology": "",
        "time_to_empty": "",
        "time_to_full": "",
        "energy_wh": None,
        "energy_empty_wh": None,
        "energy_full_wh": None,
        "energy_full_design_wh": None,
        "voltage_v": None,
        "charge_start_threshold": None,
        "charge_end_threshold": None,
        "charge_threshold_supported": False,
        "icon_name": "",
    }
    for battery in ["BAT0", "BAT1"]:
        base = Path("/sys/class/power_supply/{}".format(battery))
        if not base.exists():
            continue
        result["present"] = True
        result["native_path"] = battery
        try:
            status = base.joinpath("status").read_text().strip()
            result["state"] = status
            result["charging"] = status.lower() in {"charging", "full"}
        except Exception:
            pass
        try:
            result["pct"] = float(base.joinpath("capacity").read_text().strip())
        except Exception:
            pass
        for full_name, design_name in [("energy_full", "energy_full_design"), ("charge_full", "charge_full_design")]:
            try:
                full = float(base.joinpath(full_name).read_text().strip())
                design = float(base.joinpath(design_name).read_text().strip())
                if full > 0 and design > 0:
                    result["health"] = round((full / design) * 100, 1)
                    break
            except Exception:
                continue
        try:
            power_now = float(base.joinpath("power_now").read_text().strip())
            if power_now > 0:
                result["watts"] = round(power_now / 1_000_000, 2)
        except Exception:
            try:
                current = float(base.joinpath("current_now").read_text().strip())
                voltage = float(base.joinpath("voltage_now").read_text().strip())
                if current > 0 and voltage > 0:
                    result["watts"] = round((current / 1_000_000) * (voltage / 1_000_000), 2)
            except Exception:
                pass
        try:
            voltage = float(base.joinpath("voltage_now").read_text().strip())
            if voltage > 0:
                result["voltage_v"] = round(voltage / 1_000_000, 3)
        except Exception:
            pass
        break
    try:
        out, _, rc = run("upower -i $(upower -e | grep BAT | head -1) 2>/dev/null", timeout=5)
        if rc == 0 and out:
            result["present"] = True
            for line in out.splitlines():
                line = line.strip()
                if ":" not in line:
                    continue
                key, value = [part.strip() for part in line.split(":", 1)]
                key = key.lower()
                if key == "native-path":
                    result["native_path"] = value
                elif key == "vendor":
                    result["vendor"] = value
                elif key == "model":
                    result["model"] = value
                elif key == "serial":
                    result["serial"] = value
                elif key == "updated":
                    result["updated"] = value
                elif key == "state":
                    result["state"] = value
                    result["charging"] = value.lower() in {"charging", "full"}
                elif key == "warning-level":
                    result["warning_level"] = value
                elif key == "percentage":
                    number = _extract_number(value)
                    if number is not None:
                        result["pct"] = number
                elif key == "capacity":
                    number = _extract_number(value)
                    if number is not None:
                        result["health"] = number
                elif key == "energy":
                    result["energy_wh"] = _extract_number(value)
                elif key == "energy-empty":
                    result["energy_empty_wh"] = _extract_number(value)
                elif key == "energy-full":
                    result["energy_full_wh"] = _extract_number(value)
                elif key == "energy-full-design":
                    result["energy_full_design_wh"] = _extract_number(value)
                elif key == "energy-rate":
                    number = _extract_number(value)
                    if number is not None:
                        result["watts"] = number
                elif key == "voltage":
                    result["voltage_v"] = _extract_number(value)
                elif key == "time to empty":
                    result["time_to_empty"] = value
                elif key == "time to full":
                    result["time_to_full"] = value
                elif key == "technology":
                    result["technology"] = value
                elif key == "charge-start-threshold":
                    result["charge_start_threshold"] = _extract_number(value)
                elif key == "charge-end-threshold":
                    result["charge_end_threshold"] = _extract_number(value)
                elif key == "charge-threshold-supported":
                    result["charge_threshold_supported"] = value.lower() == "yes"
                elif key == "icon-name":
                    result["icon_name"] = value.strip("'")
    except Exception:
        pass
    return result


def get_qbit_stats():
    try:
        out, _, rc = run(
            "curl -s --max-time 5 http://localhost:{}/api/v2/torrents/info".format(WEBUI_PORT),
            timeout=8,
        )
        if rc != 0 or not out:
            return {}
        torrents = json.loads(out)
        return {
            "total": len(torrents),
            "downloading": sum(1 for item in torrents if item.get("state") in ("downloading", "stalledDL", "metaDL", "checkingDL")),
            "seeding": sum(1 for item in torrents if item.get("state") in ("uploading", "stalledUP", "seeding", "forcedUP")),
            "paused": sum(1 for item in torrents if "paused" in item.get("state", "").lower()),
            "errored": sum(1 for item in torrents if "error" in item.get("state", "").lower()),
            "dl_speed_kb": round(sum(item.get("dlspeed", 0) for item in torrents) / 1024, 1),
            "ul_speed_kb": round(sum(item.get("upspeed", 0) for item in torrents) / 1024, 1),
            "dl_total_gb": round(sum(item.get("downloaded", 0) for item in torrents) / 1024 ** 3, 2),
            "ul_total_gb": round(sum(item.get("uploaded", 0) for item in torrents) / 1024 ** 3, 2),
        }
    except Exception:
        return {}


def _get_load_avg():
    try:
        return " ".join(Path("/proc/loadavg").read_text().split()[:3])
    except Exception:
        return "?"


def _parse_disk_pct(disk_info):
    pct = disk_info.get("pct") if isinstance(disk_info, dict) else ""
    if not pct:
        return 0
    try:
        return int(str(pct).strip().rstrip("%"))
    except Exception:
        return 0


def compute_health(status_payload):
    score = 100
    cpu = float(status_payload.get("cpu") or 0)
    ram_pct = float((status_payload.get("ram") or {}).get("pct") or 0)
    disk_pct = _parse_disk_pct(status_payload.get("disk") or {})
    vpn_ok = bool(status_payload.get("gluetun"))

    if cpu > 80:
        score -= min(20, int(cpu - 80) * 2)
    if ram_pct > 80:
        score -= min(20, int(ram_pct - 80) * 2)
    if disk_pct > 90:
        score -= min(20, (disk_pct - 90) * 2)
    if not vpn_ok:
        score -= 35

    score = max(0, min(100, score))
    if score >= 75:
        status = "good"
    elif score >= 45:
        status = "warning"
    else:
        status = "critical"
    return score, status


def build_status_snapshot():
    from services import docker_service, power_service

    docker = docker_service.docker_running()
    gluetun = docker_service.container_running("gluetun") if docker else False
    qbittorrent = docker_service.container_running("qbittorrent") if docker else False
    jellyfin = docker_service.container_running("jellyfin") if docker else False
    wetty = docker_service.container_running("wetty") if docker else False
    filebrowser = docker_service.container_running("filebrowser") if docker else False
    neko = docker_service.container_running("neko") if docker else False
    samba = docker_service.samba_running()
    disk_mounted = docker_service.disk_mounted()
    adguard_url = docker_service.adguard_url()
    adguard = docker_service.adguard_running() if adguard_url else False

    disk_info = {}
    if disk_mounted:
        out, _, _ = run("df -h {} | tail -1".format(MOUNT_POINT))
        parts = out.split()
        if len(parts) >= 5:
            disk_info = {"size": parts[1], "used": parts[2], "free": parts[3], "pct": parts[4]}

    local_ip = docker_service.local_ip()
    home_ip, _, _ = run(
        "wget -qO- --timeout=5 api.ipify.org 2>/dev/null || curl -s --max-time 5 api.ipify.org 2>/dev/null",
        timeout=8,
    )
    home_ip = home_ip.strip()
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", home_ip):
        home_ip = ""

    vpn_ip = docker_service.container_public_ip("qbittorrent") if qbittorrent else ""
    if not vpn_ip:
        ip_status = "unknown"
    elif vpn_ip == home_ip:
        ip_status = "leak"
    elif vpn_ip == AVISTAZ_IP:
        ip_status = "ok"
    else:
        ip_status = "mismatch"

    smb_shares = 0
    try:
        cfg = Path(SMB_CONF).read_text()
        smb_shares = len(re.findall(r"^\[(?!global|printers|print\$)", cfg, re.MULTILINE))
    except Exception:
        pass

    cpu = get_cpu()
    ram = get_ram()
    swap = get_swap()
    watts = get_live_power_watts()
    uptime_sec = get_system_uptime_seconds()
    uptime = run("uptime -p")[0] or "up"
    hostname = run("hostname")[0] or ""
    kernel = run("uname -sr")[0] or ""
    electricity = power_service.get_electricity_payload(cpu_usage=cpu, live_watts=watts)

    status_payload = {
        "gluetun": gluetun,
        "qbittorrent": qbittorrent,
        "jellyfin": jellyfin,
        "wetty": wetty,
        "filebrowser": filebrowser,
        "docker": docker,
        "neko": neko,
        "neko_port": NEKO_PORT,
        "neko_dl_path": docker_service.neko_get_dl_path(),
        "neko_stats": docker_service.neko_stats() if neko else {},
        "samba": samba,
        "adguard": adguard,
        "adguard_url": adguard_url,
        "pihole": adguard,
        "pihole_url": adguard_url,
        "smb_connections": docker_service.samba_connections() if samba else [],
        "smb_shares": smb_shares,
        "disk_mounted": disk_mounted,
        "disk": disk_info,
        "local_ip": local_ip,
        "hostname": hostname,
        "kernel": kernel,
        "home_ip": home_ip,
        "vpn_ip": vpn_ip,
        "avistaz_ip": AVISTAZ_IP,
        "ip_status": ip_status,
        "webui_port": WEBUI_PORT,
        "jellyfin_port": JELLYFIN_PORT,
        "wetty_port": WETTY_PORT,
        "filebrowser_port": FILEBROWSER_PORT,
        "cpu": cpu,
        "ram": ram,
        "swap": swap,
        "load": _get_load_avg(),
        "cores": run("nproc")[0],
        "uptime": uptime,
        "uptime_sec": uptime_sec,
        "temps": get_temps(),
        "net": get_net_io(),
        "disk_io": get_disk_io(),
        "qbit_stats": get_qbit_stats() if qbittorrent else {},
        "jellyfin_sessions": docker_service.jellyfin_sessions() if jellyfin else [],
        "battery": get_battery(),
        "watts": watts,
        "electricity": electricity,
    }
    health_score, health_status = compute_health(status_payload)
    status_payload["health_score"] = health_score
    status_payload["health_status"] = health_status
    return status_payload


def status_stream_response():
    def generate():
        while True:
            payload = build_status_snapshot()
            yield "data: {}\n\n".format(json.dumps(payload))
            time.sleep(3)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def get_processes():
    out, _, _ = run("ps -eo pid,comm,%mem,%cpu,etime --no-headers --sort=-%mem | head -20")
    processes = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4:
            processes.append(
                {
                    "pid": parts[0],
                    "name": parts[1],
                    "mem": parts[2],
                    "cpu": parts[3],
                    "time": parts[4].strip() if len(parts) > 4 else "",
                }
            )
    return processes


def kill_process(pid, sig_name="TERM"):
    if not str(pid).isdigit():
        return legacy_error_payload("Invalid PID"), 400
    pid = int(pid)
    if pid in (1, os.getpid()):
        return legacy_error_payload("Cannot kill that process"), 403

    sig_map = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL, "HUP": signal.SIGHUP}
    sig_name = sig_name or "TERM"
    try:
        os.kill(pid, sig_map.get(sig_name, signal.SIGTERM))
        return {"ok": True, "msg": "Sent {} to PID {}".format(sig_name, pid)}, 200
    except ProcessLookupError:
        return legacy_error_payload("PID {} not found".format(pid)), 404
    except PermissionError:
        _, _, rc = privileged_run("kill", str(pid), sig_name, timeout=10)
        if rc == 0:
            return {"ok": True, "msg": "Sent {} to PID {} (sudo)".format(sig_name, pid)}, 200
        return legacy_error_payload("Permission denied"), 403
    except Exception as exc:
        return legacy_error_payload(str(exc)), 500


def _parse_allowed_command(raw_cmd):
    raw_cmd = (raw_cmd or "").strip()
    if not raw_cmd:
        raise ValueError("No command")
    if any(token in raw_cmd for token in ["|", ";", "&&", "||", ">", "<", "$(", "`"]):
        raise ValueError("Shell operators are not allowed")
    args = shlex.split(raw_cmd)
    if not args:
        raise ValueError("No command")
    base = os.path.basename(args[0])
    if base not in ALLOWED_RUN_COMMANDS:
        raise ValueError("Command '{}' is not allowed".format(base))
    return args


def _append_command_log(command, exit_code):
    try:
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "command": command,
            "exit_code": exit_code,
        }
        Path(COMMAND_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(COMMAND_LOG_FILE, "a") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception as exc:
        log.warning("Could not write command log: %s", exc)


def run_command_response(raw_cmd):
    try:
        args = _parse_allowed_command(raw_cmd)
    except ValueError as exc:
        return json_error(str(exc), 403)

    def generate():
        timed_out = {"value": False}
        proc = None
        exit_code = 1
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                stdin=subprocess.DEVNULL,
                env={**os.environ, "TERM": "xterm"},
            )

            def _kill_proc():
                timed_out["value"] = True
                try:
                    proc.kill()
                except Exception:
                    pass

            timer = threading.Timer(DEFAULT_TIMEOUT, _kill_proc)
            timer.start()
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    clean = strip_ansi(line.rstrip())
                    if clean:
                        yield "data: {}\n\n".format(json.dumps(clean))
                proc.wait()
                exit_code = 124 if timed_out["value"] else proc.returncode
            finally:
                timer.cancel()
        except Exception as exc:
            yield "data: {}\n\n".format(json.dumps("ERROR: {}".format(exc)))
            exit_code = 1
        if timed_out["value"]:
            yield "data: {}\n\n".format(json.dumps("ERROR: timed out after {}s".format(DEFAULT_TIMEOUT)))
        _append_command_log(raw_cmd, exit_code)
        yield "data: {}\n\n".format(json.dumps("__DONE__:{}".format(exit_code)))

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
