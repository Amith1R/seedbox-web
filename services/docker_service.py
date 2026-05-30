import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from services.system_service import (
    AVISTAZ_IP,
    COMPOSE_DIR,
    JELLYFIN_PORT,
    MOUNT_POINT,
    NEKO_PORT,
    PRIV_HINT,
    SAMBA_SERVICE,
    SMB_CONF,
    human_size,
    legacy_error_payload,
    privileged_run,
    run,
)

MANAGED_CONTAINERS = {"gluetun", "qbittorrent", "jellyfin", "wetty", "filebrowser", "neko"}
ADGUARD_URL = os.environ.get("SEEDBOX_ADGUARD_URL", "http://127.0.0.1").strip()


def compose_up(*services, timeout=90, no_start=False):
    args = ["compose-up"]
    if no_start:
        args.append("--no-start")
    args.extend(services)
    return privileged_run(*args, timeout=timeout)


def compose_down(timeout=60):
    return privileged_run("compose-down", timeout=timeout)


def _extract_conflicting_containers(text):
    names = set()
    for match in re.finditer(r'container name "(/[^"]+)" is already in use', text or "", re.IGNORECASE):
        name = match.group(1).lstrip("/")
        if name in MANAGED_CONTAINERS:
            names.add(name)
    return names


def compose_up_resilient(*services, timeout=90, no_start=False):
    out, err, rc = compose_up(*services, timeout=timeout, no_start=no_start)
    if rc == 0:
        return out, err, rc
    details = "\n".join(x for x in [err, out] if x).strip()
    conflicts = _extract_conflicting_containers(details)
    if not conflicts:
        return out, err, rc
    rm_out, rm_err, rm_rc = privileged_run("container-rm", *sorted(conflicts), timeout=30)
    if rm_rc != 0:
        merged = "\n".join(x for x in [details, rm_err, rm_out] if x).strip()
        return out, merged, rc
    retry_out, retry_err, retry_rc = compose_up(*services, timeout=timeout, no_start=no_start)
    if retry_rc == 0:
        recovered = "Recovered by removing stale containers: {}.".format(", ".join(sorted(conflicts)))
        return "\n".join(x for x in [recovered, retry_out] if x).strip(), retry_err, retry_rc
    merged = "\n".join(
        x for x in [
            "Removed stale containers: {}.".format(", ".join(sorted(conflicts))),
            retry_err,
            retry_out,
        ] if x
    ).strip()
    return retry_out, merged, retry_rc


def docker_running():
    _, _, rc = privileged_run("docker-info", timeout=8)
    return rc == 0


def container_running(name):
    out, _, rc = privileged_run("container-running", name, timeout=10)
    return rc == 0 and bool(out.strip())


def container_exists(name):
    out, _, rc = privileged_run("container-exists", name, timeout=10)
    return rc == 0 and bool(out.strip())


def samba_running():
    _, _, rc = run("systemctl is-active --quiet {}".format(SAMBA_SERVICE))
    if rc == 0:
        return True
    _, _, rc2 = run("pgrep smbd")
    return rc2 == 0


def disk_mounted():
    _, _, rc = run("mountpoint -q {}".format(MOUNT_POINT))
    return rc == 0


def mount_disk():
    out, err, rc = privileged_run("mount-disk", timeout=25)
    if rc == 0:
        return True, out.strip() or "disk"
    return False, err or out or "Mount failed"


def local_ip():
    forced = os.environ.get("SEEDBOX_LOCAL_IP", "").strip()
    if forced:
        return forced
    ip, _, _ = run("hostname -I")
    candidates = [part.strip() for part in ip.split() if part.strip()]
    preferred = [part for part in candidates if re.match(r"^(192\.168\.|10\.|172\.(1[6-9]|2\d|3[0-1])\.)", part)]
    return (preferred or candidates or [""])[0]


def adguard_url():
    return ADGUARD_URL


def adguard_running():
    parsed = urlparse(ADGUARD_URL if "://" in ADGUARD_URL else "http://" + ADGUARD_URL)
    if not parsed.hostname:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    out, _, rc = run(
        "curl -k -L -s --max-time 5 -o /dev/null -w '%{{http_code}}' '{}'".format(parsed.geturl()),
        timeout=7,
    )
    if rc == 0 and out.strip().isdigit():
        return int(out.strip()) < 500
    _, _, rc = run("timeout 5 bash -lc 'cat < /dev/null > /dev/tcp/{}/{}'".format(parsed.hostname, port), timeout=7)
    return rc == 0


def pihole_url():
    return adguard_url()


def pihole_running():
    return adguard_running()


def container_public_ip(name):
    out, _, rc = privileged_run("container-ipify", name, timeout=12)
    if rc == 0:
        ip = out.strip()
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
            return ip
    return ""


def samba_connections():
    out, _, rc = privileged_run("smbstatus", "--brief", timeout=10)
    if rc != 0 or not out:
        return []
    conns = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit():
            conns.append({"pid": parts[0], "user": parts[1], "machine": parts[2]})
    return conns


def jellyfin_sessions():
    out, _, rc = run("curl -s --max-time 5 http://localhost:{}/Sessions".format(JELLYFIN_PORT), timeout=8)
    if rc != 0 or not out or not out.strip().startswith("["):
        return []
    try:
        sessions = json.loads(out)
    except Exception:
        return []
    active = []
    for session in sessions if isinstance(sessions, list) else []:
        now_playing = session.get("NowPlayingItem")
        if now_playing:
            active.append(
                {
                    "user": session.get("UserName", "?"),
                    "title": now_playing.get("Name", "?"),
                    "type": now_playing.get("Type", "?"),
                    "client": session.get("Client", "?"),
                    "play_method": session.get("PlayState", {}).get("PlayMethod", "?"),
                    "is_paused": session.get("PlayState", {}).get("IsPaused", False),
                }
            )
    return active


def neko_stats():
    out, _, _ = privileged_run("container-stats", "neko", timeout=10)
    if out and "|" in out:
        parts = out.strip().split("|")
        if len(parts) >= 2:
            return {"cpu": parts[0], "mem": parts[1]}
    return {}


def neko_update_ip():
    compose_file = os.path.join(COMPOSE_DIR, "docker-compose.yml")
    try:
        ip = local_ip()
        if not ip:
            return False, "Could not detect IP"
        content = Path(compose_file).read_text()
        updated = re.sub(r"NEKO_NAT1TO1=[\d.]+", "NEKO_NAT1TO1={}".format(ip), content)
        if updated != content:
            Path(compose_file).write_text(updated)
            return True, "Updated NAT IP to {}".format(ip)
        return True, "NAT IP already {}".format(ip)
    except Exception as exc:
        return False, str(exc)


def neko_fix_nat_ip():
    return neko_update_ip()


def neko_get_dl_path():
    try:
        content = Path(os.path.join(COMPOSE_DIR, "docker-compose.yml")).read_text()
        match = re.search(r"(/[^:\s]+):/home/neko/Downloads", content)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "/mnt/exstore/Downloads"


def neko_set_dl_path(path):
    compose_file = os.path.join(COMPOSE_DIR, "docker-compose.yml")
    try:
        content = Path(compose_file).read_text()
        updated = re.sub(r"(/[^:\s]+):/home/neko/Downloads", "{}:/home/neko/Downloads".format(path), content)
        Path(compose_file).write_text(updated)
        return True, "OK"
    except Exception:
        return False, "Could not update compose file"


def build_neko_status():
    docker = docker_running()
    running = container_running("neko") if docker else False
    exists = container_exists("neko") if docker else False
    mem_usage = ""
    if running:
        out, _, _ = privileged_run("container-mem", "neko", timeout=10)
        mem_usage = out.strip()
    ip = local_ip()
    return {
        "running": running,
        "exists": exists,
        "url": "http://{}:{}".format(ip, NEKO_PORT) if running else "",
        "local_ip": ip,
        "mem_usage": mem_usage,
        "download_path": neko_get_dl_path(),
    }


def get_log_command(service):
    commands = {
        "gluetun": ("container-logs", "gluetun", "--tail", "100"),
        "qbittorrent": ("container-logs", "qbittorrent", "--tail", "100"),
        "jellyfin": ("container-logs", "jellyfin", "--tail", "100"),
        "wetty": ("container-logs", "wetty", "--tail", "100"),
        "filebrowser": ("container-logs", "filebrowser", "--tail", "100"),
        "neko": ("container-logs", "neko", "--tail", "100"),
        "adguard": ("journal", "AdGuardHome", "--tail", "80"),
        "pihole": ("journal", "pihole-FTL", "--tail", "80"),
        "seedbox-web": ("journal", "seedbox-web", "--tail", "80"),
        "telegram-bot": ("journal", "seedbox-telegram-bot", "--tail", "80"),
        "samba": ("journal", "smbd", "--tail", "80"),
        "docker": ("journal", "docker", "--tail", "80"),
        "system": None,
        "syslog": ("journal", "system", "--tail", "80"),
    }
    if service not in commands:
        return None
    if service == "system":
        return "tail -100 {} 2>/dev/null || echo 'No log yet'".format(os.path.expanduser("~/seedbox/seedbox.log"))
    return commands[service]


def _stop_all():
    compose_down(timeout=60)
    privileged_run("service", "stop", SAMBA_SERVICE, timeout=10)
    privileged_run("docker-stop-all", timeout=30)
    privileged_run("service", "stop", "docker", "docker.socket", "containerd", timeout=15)
    privileged_run("unmount-disk", "--lazy", timeout=10)


def _restore_all():
    mount_disk()
    privileged_run("service", "start", "docker", timeout=25)
    time.sleep(5)
    compose_up_resilient(timeout=90)
    privileged_run("service", "start", SAMBA_SERVICE, timeout=10)


def handle_action(body):
    if not body:
        return legacy_error_payload("No request body"), 400
    action = body.get("action", "")

    if action == "samba_config":
        try:
            return {"ok": True, "msg": Path(SMB_CONF).read_text()}, 200
        except Exception as exc:
            return legacy_error_payload("Cannot read smb.conf: {}".format(exc)), 500

    if action == "mount_disk":
        ok, msg = mount_disk()
        return {"ok": ok, "msg": "Mounted ({}) OK".format(msg) if ok else msg}, 200

    if action == "unmount_disk":
        _, err, rc = privileged_run("unmount-disk", timeout=10)
        if rc != 0:
            _, err, rc = privileged_run("unmount-disk", "--lazy", timeout=10)
        return {"ok": rc == 0, "msg": "Disk unmounted!" if rc == 0 else "Failed: {}".format(err)}, 200

    if action == "remount_disk":
        privileged_run("unmount-disk", "--lazy", timeout=10)
        time.sleep(1)
        ok, msg = mount_disk()
        return {"ok": ok, "msg": "Remounted OK" if ok else msg}, 200

    if action == "stop_docker":
        compose_down(timeout=60)
        privileged_run("docker-stop-all", timeout=30)
        privileged_run("service", "stop", "docker", timeout=15)
        privileged_run("service", "stop", "docker.socket", "containerd", timeout=10)
        _, _, rc = privileged_run("service-is-active", "docker", timeout=8)
        return {"ok": rc != 0, "msg": "Docker stopped!" if rc != 0 else "May still be running"}, 200

    if action == "shutdown":
        threading.Thread(
            target=lambda: (time.sleep(2), _stop_all(), time.sleep(2), privileged_run("shutdown-now", timeout=20)),
            daemon=True,
        ).start()
        return {"ok": True, "msg": "Shutting down safely..."}, 200

    if action == "reboot":
        threading.Thread(
            target=lambda: (time.sleep(2), _stop_all(), time.sleep(2), privileged_run("reboot-now", timeout=20)),
            daemon=True,
        ).start()
        return {"ok": True, "msg": "Rebooting safely..."}, 200

    if action == "power_save":
        threading.Thread(
            target=lambda: (_stop_all(), privileged_run("service", "stop", "cups", "avahi-daemon", "bluetooth", timeout=10)),
            daemon=True,
        ).start()
        return {"ok": True, "msg": "Power save active. SSH stays up."}, 200

    if action == "restore_power_save":
        threading.Thread(target=_restore_all, daemon=True).start()
        return {"ok": True, "msg": "Restoring full seedbox stack..."}, 200

    if action == "check_ip":
        vpn_ip = container_public_ip("qbittorrent")
        if vpn_ip == AVISTAZ_IP:
            return {"ok": True, "msg": "VPN OK ({})".format(vpn_ip)}, 200
        if not vpn_ip:
            return {"ok": False, "msg": "No VPN IP yet"}, 200
        return {"ok": False, "msg": "IP mismatch: got {}, expected {}".format(vpn_ip, AVISTAZ_IP)}, 200

    if action == "start_neko":
        neko_fix_nat_ip()
        out, err, rc = compose_up_resilient("neko", timeout=60)
        ip = local_ip()
        dl_path = neko_get_dl_path()
        if rc == 0:
            return {
                "ok": True,
                "msg": "Neko started!\nURL: http://{}:{}\nPassword: amit\nDownloads -> {}".format(ip, NEKO_PORT, dl_path),
            }, 200
        return legacy_error_payload("Failed to start Neko: {}".format(err or out)), 200

    if action == "stop_neko":
        _, err, rc = privileged_run("container-stop", "neko", timeout=20)
        return {"ok": rc == 0, "msg": "Neko stopped. Resources freed." if rc == 0 else "Failed: {}".format(err)}, 200

    if action == "restart_neko":
        neko_fix_nat_ip()
        privileged_run("container-stop", "neko", timeout=20)
        time.sleep(2)
        out, err, rc = compose_up_resilient("neko", timeout=60)
        return {"ok": rc == 0, "msg": "Neko restarted!" if rc == 0 else "Failed: {}".format(err or out)}, 200

    if action == "neko_logs":
        out, err, _ = privileged_run("container-logs", "neko", "--tail", "80", timeout=15)
        return {"ok": True, "msg": out or err or "No logs"}, 200

    if action == "neko_set_download_path":
        path = body.get("path", "").strip()
        if not path:
            return legacy_error_payload("No path provided"), 400
        from services.system_service import safe_path

        try:
            path = safe_path(path)
        except ValueError as exc:
            return legacy_error_payload(str(exc)), 400
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as exc:
            return legacy_error_payload("Cannot create: {}".format(exc)), 400
        ok, msg = neko_set_dl_path(path)
        if not ok:
            return legacy_error_payload(msg), 200
        if container_running("neko"):
            neko_fix_nat_ip()
            privileged_run("container-stop", "neko", timeout=20)
            time.sleep(2)
            compose_up_resilient("neko", timeout=60)
            return {"ok": True, "msg": "Download path set to {}\nNeko restarted.".format(path)}, 200
        return {"ok": True, "msg": "Download path set to {}. Start Neko to apply.".format(path)}, 200

    actions_map = {
        "start_seedbox": lambda: compose_up_resilient(timeout=90),
        "stop_seedbox": lambda: compose_down(timeout=60),
        "restart_seedbox": lambda: (compose_down(timeout=60), time.sleep(2), compose_up_resilient(timeout=90))[2],
        "start_qbittorrent": lambda: compose_up_resilient("gluetun", "qbittorrent", timeout=60),
        "stop_qbittorrent": lambda: privileged_run("container-stop", "qbittorrent", timeout=20),
        "restart_qbittorrent": lambda: privileged_run("container-restart", "qbittorrent", timeout=20),
        "start_gluetun": lambda: compose_up_resilient("gluetun", timeout=60),
        "stop_gluetun": lambda: (
            privileged_run("container-stop", "qbittorrent", timeout=20),
            privileged_run("container-stop", "gluetun", timeout=20)
        )[1],
        "restart_gluetun": lambda: (
            privileged_run("container-restart", "gluetun", timeout=20),
            privileged_run("container-start", "qbittorrent", timeout=20)
        )[1],
        "start_jellyfin": lambda: compose_up_resilient("jellyfin", timeout=60),
        "stop_jellyfin": lambda: privileged_run("container-stop", "jellyfin", timeout=30),
        "restart_jellyfin": lambda: privileged_run("container-restart", "jellyfin", timeout=30),
        "jellyfin_logs": lambda: privileged_run("container-logs", "jellyfin", "--tail", "80", timeout=15),
        "start_wetty": lambda: compose_up_resilient("wetty", timeout=60),
        "stop_wetty": lambda: privileged_run("container-stop", "wetty", timeout=20),
        "restart_wetty": lambda: (
            privileged_run("container-stop", "wetty", timeout=20),
            compose_up_resilient("wetty", timeout=60)
        )[1],
        "start_filebrowser": lambda: privileged_run("container-start", "filebrowser", timeout=20),
        "stop_filebrowser": lambda: privileged_run("container-stop", "filebrowser", timeout=20),
        "restart_filebrowser": lambda: privileged_run("container-restart", "filebrowser", timeout=20),
        "start_samba": lambda: privileged_run("service", "start", SAMBA_SERVICE, timeout=15),
        "stop_samba": lambda: privileged_run("service", "stop", SAMBA_SERVICE, timeout=15),
        "restart_samba": lambda: privileged_run("service", "restart", SAMBA_SERVICE, timeout=15),
        "samba_status": lambda: privileged_run("smbstatus", timeout=15),
        "samba_testparm": lambda: run("testparm -s 2>&1"),
        "start_docker": lambda: privileged_run("service", "start", "docker", timeout=25),
        "df": lambda: run("df -h | grep -v tmpfs | grep -v loop | grep -v udev"),
        "lsblk": lambda: run("lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,LABEL 2>/dev/null"),
        "docker_ps": lambda: privileged_run("docker-ps", timeout=20),
        "docker_images": lambda: privileged_run("docker-images", timeout=20),
        "docker_stats": lambda: privileged_run("docker-stats", timeout=20),
        "ping_avistaz": lambda: run("ping -c 4 {} 2>&1".format(AVISTAZ_IP)),
        "vainfo": lambda: run("vainfo 2>&1"),
        "jellyfin_transcode_check": lambda: privileged_run("jellyfin-transcodes", timeout=15),
    }
    info_actions = {
        "jellyfin_logs",
        "samba_status",
        "samba_testparm",
        "df",
        "lsblk",
        "docker_ps",
        "docker_images",
        "docker_stats",
        "ping_avistaz",
        "vainfo",
        "jellyfin_transcode_check",
    }
    msg_map = {
        "start_seedbox": ("Seedbox started!", "Start failed"),
        "stop_seedbox": ("Seedbox stopped!", "Stop failed"),
        "restart_seedbox": ("Seedbox restarted!", "Restart failed"),
        "start_qbittorrent": ("qBittorrent started!", "Start failed"),
        "stop_qbittorrent": ("qBittorrent stopped!", "Stop failed"),
        "restart_qbittorrent": ("qBittorrent restarted!", "Restart failed"),
        "start_gluetun": ("Gluetun started!", "Start failed"),
        "stop_gluetun": ("Gluetun stopped!", "Stop failed"),
        "restart_gluetun": ("Gluetun restarted!", "Restart failed"),
        "start_jellyfin": ("Jellyfin started!", "Start failed"),
        "stop_jellyfin": ("Jellyfin stopped!", "Stop failed"),
        "restart_jellyfin": ("Jellyfin restarted!", "Restart failed"),
        "jellyfin_logs": ("", "No logs"),
        "start_wetty": ("Wetty started!", "Start failed"),
        "stop_wetty": ("Wetty stopped!", "Stop failed"),
        "restart_wetty": ("Wetty restarted!", "Restart failed"),
        "start_filebrowser": ("FileBrowser started!", "Start failed"),
        "stop_filebrowser": ("FileBrowser stopped!", "Stop failed"),
        "restart_filebrowser": ("FileBrowser restarted!", "Restart failed"),
        "start_samba": ("Samba started!", "Start failed"),
        "stop_samba": ("Samba stopped!", "Stop failed"),
        "restart_samba": ("Samba restarted!", "Restart failed"),
        "samba_status": ("", "Error"),
        "samba_testparm": ("", "Error"),
        "start_docker": ("Docker started!", "Start failed"),
        "df": ("", "Error"),
        "lsblk": ("", "Error"),
        "docker_ps": ("", "Error"),
        "docker_images": ("", "Error"),
        "docker_stats": ("", "Error"),
        "ping_avistaz": ("", "Ping failed"),
        "vainfo": ("", "Error"),
        "jellyfin_transcode_check": ("", "Error"),
    }

    if action in actions_map:
        out, err, rc = actions_map[action]()
        if action in info_actions:
            return {"ok": True, "msg": out or err or "No output"}, 200
        ok_msg, err_msg = msg_map[action]
        return {"ok": rc == 0, "msg": ok_msg if rc == 0 else "{}: {}".format(err_msg, err or out)}, 200

    return legacy_error_payload("Unknown action: {}".format(action)), 200


def ensure_neko_created():
    def _do():
        time.sleep(10)
        if not docker_running():
            return
        if container_exists("neko"):
            return
        neko_update_ip()
        out, err, rc = compose_up_resilient("neko", timeout=180, no_start=True)
        if rc != 0:
            _, retry_err, retry_rc = compose_up_resilient("neko", timeout=180)
            if retry_rc != 0:
                from services.system_service import log

                log.warning("Neko create failed: %s", retry_err or err)

    threading.Thread(target=_do, daemon=True).start()
