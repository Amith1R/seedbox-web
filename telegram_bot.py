#!/usr/bin/env python3
"""Telegram bot for remote control and monitoring of the seedbox dashboard."""

from collections import deque
from functools import wraps
import json
import logging
import mimetypes
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import time

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

API_BASE = (os.getenv("SEEDBOX_API_BASE", "http://127.0.0.1:5000/api").rstrip("/") or "http://127.0.0.1:5000/api")
API_TIMEOUT = 5.0
STATUS_API_TIMEOUT = 15.0
BOT_TOKEN = os.getenv("SEEDBOX_TELEGRAM_BOT_TOKEN", "PUT_BOT_TOKEN_HERE")
ALERT_INTERVAL_SECONDS = 60
DOWNLOAD_INTERVAL_SECONDS = 12
MAX_MESSAGE_LENGTH = 3900
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_SEND_BYTES = 50 * 1024 * 1024
MAX_READ_BYTES = 100 * 1024
LARGE_FILE_BYTES = 2 * 1024 * 1024 * 1024
METRIC_HISTORY_SIZE = 100
CPU_ALERT_THRESHOLD = 90.0
CPU_ALERT_SUSTAINED_CHECKS = 3
DOWNLOAD_STALL_INTERVALS = 5
DOWNLOAD_PROGRESS_NOTIFY_STEP = 10.0
READABLE_TEXT_SUFFIXES = {
    ".txt", ".log", ".nfo", ".json", ".md", ".srt", ".ass", ".sub",
    ".py", ".sh", ".ini", ".cfg", ".conf", ".yaml", ".yml", ".toml",
    ".csv", ".xml", ".m3u", ".m3u8",
}
SAFE_MODE_BLOCKED = {"shutdown", "reboot", "delete", "kill", "run_command"}

OK = "OK"
WARN = "WARN"
ERROR = "ERROR"


def _detect_existing_path(candidates):
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return os.path.normpath(candidate)
    return os.path.normpath(candidates[0])


DOWNLOADS_ROOT = os.getenv(
    "SEEDBOX_TELEGRAM_DOWNLOADS_PATH",
    _detect_existing_path(["/mnt/exstore/Downloads", "/downloads"]),
)
MEDIA_ROOT = os.getenv(
    "SEEDBOX_TELEGRAM_MEDIA_PATH",
    _detect_existing_path(["/mnt/exstore", "/media"]),
)
HOME_ROOT = os.getenv("SEEDBOX_TELEGRAM_HOME_PATH", str(Path.home()))
VIRTUAL_BASE_MAP = {
    "/downloads": os.path.normpath(DOWNLOADS_ROOT),
    "/media": os.path.normpath(MEDIA_ROOT),
    "/home": os.path.normpath(HOME_ROOT),
}
ALLOWED_BASE_PATHS = list(VIRTUAL_BASE_MAP.keys())
UPLOADS_VIRTUAL_DIR = "/downloads/telegram_uploads"
UPLOADS_DIR = Path(os.path.normpath(os.path.join(VIRTUAL_BASE_MAP["/downloads"], "telegram_uploads")))
DEFAULT_FILES_PATH = "/downloads"
DEFAULT_DOWNLOAD_DEST = VIRTUAL_BASE_MAP["/downloads"]
QUICK_PATHS = {
    "downloads": ("/downloads", "Downloads"),
    "uploads": ("/downloads/telegram_uploads", "Telegram Uploads"),
    "media": ("/media", "Media"),
    "home": ("/home", "Home"),
}

SERVICE_ACTIONS = {
    "docker": {"start": "start_docker", "stop": "stop_docker", "restart": ("stop_docker", "start_docker")},
    "jellyfin": {"start": "start_jellyfin", "stop": "stop_jellyfin", "restart": "restart_jellyfin"},
    "qbittorrent": {"start": "start_qbittorrent", "stop": "stop_qbittorrent", "restart": "restart_qbittorrent"},
    "gluetun": {"start": "start_gluetun", "stop": "stop_gluetun", "restart": "restart_gluetun"},
    "wetty": {"start": "start_wetty", "stop": "stop_wetty", "restart": "restart_wetty"},
    "filebrowser": {"start": "start_filebrowser", "stop": "stop_filebrowser", "restart": "restart_filebrowser"},
    "neko": {"start": "start_neko", "stop": "stop_neko", "restart": "restart_neko"},
    "samba": {"start": "start_samba", "stop": "stop_samba", "restart": "restart_samba"},
}
SERVICE_ORDER = ["docker", "jellyfin", "qbittorrent", "gluetun", "wetty", "filebrowser", "neko", "samba"]
SERVICE_LABELS = {
    "docker": "Docker",
    "jellyfin": "Jellyfin",
    "qbittorrent": "qBittorrent",
    "gluetun": "VPN",
    "wetty": "Terminal",
    "filebrowser": "FileBrowser",
    "neko": "Browser",
    "samba": "Samba",
    "system": "System",
    "syslog": "Syslog",
}
SERVICE_ALIASES = {
    "docker": "docker",
    "jellyfin": "jellyfin",
    "qb": "qbittorrent",
    "qbit": "qbittorrent",
    "qbittorrent": "qbittorrent",
    "gluetun": "gluetun",
    "vpn": "gluetun",
    "wetty": "wetty",
    "filebrowser": "filebrowser",
    "files": "filebrowser",
    "neko": "neko",
    "browser": "neko",
    "samba": "samba",
}
LOG_SERVICE_ALIASES = {
    **SERVICE_ALIASES,
    "docker": "docker",
    "system": "system",
    "syslog": "syslog",
}
LOG_SERVICE_ORDER = ["qbittorrent", "gluetun", "jellyfin", "filebrowser", "wetty", "neko", "samba", "docker", "system", "syslog"]
DOWNLOAD_FAILURE_STATES = {"failed", "partial", "interrupted", "unknown", "cancelled"}

HELP_TEXT = """Seedbox Telegram Bot

Monitoring
/status or /s - server health with suggestions
/battery - battery percent, charging state, health, and draw
/downloads or /d - current download jobs
/log <service> - last 50 log lines
/files <path> - browse files with buttons
/processes or /p - top processes
/cpu_graph - recent CPU history
/ram_graph - recent RAM history

Services
/start_service <name>
/stop_service <name>
/restart_service <name>
/restart_all or /r
/stop_all

Power
/power_save_on
/power_save_off
/reboot
/shutdown
/quick - quick actions menu

Downloads
/download or /dl <url_or_magnet>
/cancel_download <job_id>

Files
/get <path> - send file to Telegram if under 50MB
/read <path> - read a text file up to 100KB
/mkdir <path>
/rename <path> <new_name>
/move <src1> | <src2> | <destination_folder>
/delete <path>

Safety
/alerts_on
/alerts_off
/safe_mode_on
/safe_mode_off
/auto_recovery_on
/auto_recovery_off

Allowed file roots
/downloads
/media
/home
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("seedbox.telegram")


def load_allowed_users():
    raw = os.getenv("SEEDBOX_TELEGRAM_ALLOWED_USERS", "").strip()
    if raw:
        users = []
        for item in raw.split(","):
            item = item.strip()
            if item.isdigit():
                users.append(int(item))
        if users:
            return users
    return [0]  # Replace 0 with your Telegram user ID.


ALLOWED_USERS = load_allowed_users()


class DashboardAPI:
    def __init__(self, base_url: str, timeout: float = API_TIMEOUT):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._default_timeout = timeout

    async def close(self):
        await self._client.aclose()

    async def get_json(self, path: str, params=None, timeout=None):
        response = await self._client.get(path, params=params, timeout=timeout or self._default_timeout)
        return self._parse_json(response)

    async def post_json(self, path: str, payload=None, timeout=None):
        response = await self._client.post(path, json=payload or {}, timeout=timeout or self._default_timeout)
        return self._parse_json(response)

    async def stream_lines(self, path: str, params=None, max_lines: int = 200, timeout=None):
        lines = []
        async with self._client.stream("GET", path, params=params, timeout=timeout or self._default_timeout) as response:
            response.raise_for_status()
            async for raw_line in response.aiter_lines():
                if not raw_line.startswith("data: "):
                    continue
                payload = raw_line[6:]
                try:
                    decoded = json.loads(payload)
                except json.JSONDecodeError:
                    decoded = payload
                if decoded == "__DONE__":
                    break
                if isinstance(decoded, str):
                    lines.append(decoded)
                elif decoded:
                    lines.append(json.dumps(decoded))
                if len(lines) >= max_lines:
                    break
        return lines

    @staticmethod
    def _parse_json(response: httpx.Response):
        try:
            return response.json()
        except ValueError as exc:
            if response.is_error:
                message = response.text.strip() or f"HTTP {response.status_code}"
                return {"ok": False, "success": False, "error": message, "msg": message}
            raise RuntimeError("Invalid JSON from dashboard") from exc


def is_authorized(update: Update):
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USERS)


def authorized_only(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update):
            return
        try:
            await handler(update, context)
        except httpx.TimeoutException:
            await reply_text(update, f"{ERROR} Error contacting server")
        except httpx.HTTPError:
            log.exception("HTTP error")
            await reply_text(update, f"{ERROR} Error contacting server")
        except ValueError as exc:
            await reply_text(update, f"{ERROR} {exc}")
        except Exception:
            log.exception("Unhandled bot error")
            await reply_text(update, f"{ERROR} Unexpected error")

    return wrapper


def sanitize_filename(name: str):
    name = os.path.basename(name or "").strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name or "upload.bin"


def human_size(size):
    try:
        size = int(size)
    except Exception:
        return "--"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def to_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def parse_percent(value):
    if value is None:
        return 0.0
    if isinstance(value, str):
        value = value.strip().rstrip("%")
    return to_float(value, 0.0)


def normalize_virtual_path(raw_path: str):
    raw_path = (raw_path or "").strip()
    if not raw_path:
        raise ValueError("Path required")
    if not raw_path.startswith("/"):
        raise ValueError("Path must be absolute")
    normalized = posixpath.normpath(raw_path)
    if normalized == "/":
        raise ValueError("Root path is not allowed")
    for blocked in ["/etc", "/usr", "/bin"]:
        if normalized == blocked or normalized.startswith(blocked + "/"):
            raise ValueError("That path is not allowed")

    actual_items = sorted(VIRTUAL_BASE_MAP.items(), key=lambda item: len(item[1]), reverse=True)
    for virtual_base, actual_base in VIRTUAL_BASE_MAP.items():
        if normalized == virtual_base or normalized.startswith(virtual_base + "/"):
            suffix = normalized[len(virtual_base):].lstrip("/")
            actual_path = os.path.normpath(os.path.join(actual_base, suffix))
            if os.path.commonpath([actual_base, actual_path]) != actual_base:
                raise ValueError("Path escapes allowed root")
            return actual_path, normalized

    for virtual_base, actual_base in actual_items:
        if normalized == actual_base or normalized.startswith(actual_base + "/"):
            rel = os.path.relpath(normalized, actual_base)
            virtual_path = virtual_base if rel == "." else posixpath.join(virtual_base, rel.replace(os.sep, "/"))
            return os.path.normpath(normalized), virtual_path

    raise ValueError("Allowed roots: /downloads, /media, /home")


def actual_to_virtual(actual_path: str):
    actual_path = os.path.normpath(actual_path)
    items = sorted(VIRTUAL_BASE_MAP.items(), key=lambda item: len(item[1]), reverse=True)
    for virtual_base, actual_base in items:
        if actual_path == actual_base or actual_path.startswith(actual_base + os.sep):
            rel = os.path.relpath(actual_path, actual_base)
            return virtual_base if rel == "." else posixpath.join(virtual_base, rel.replace(os.sep, "/"))
    return actual_path


def ensure_local_path(raw_path: str, must_exist=False, expect_file=None):
    actual_path, virtual_path = normalize_virtual_path(raw_path)
    target = Path(actual_path)
    if must_exist and not target.exists():
        raise ValueError(f"Not found: {virtual_path}")
    if expect_file is True and target.exists() and not target.is_file():
        raise ValueError("Not a file")
    if expect_file is False and target.exists() and not target.is_dir():
        raise ValueError("Not a directory")
    return target, virtual_path


def upload_dir():
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    return UPLOADS_DIR


def safe_mode_enabled(context: ContextTypes.DEFAULT_TYPE):
    return bool(context.application.bot_data.get("safe_mode", False))


def auto_recovery_enabled(context: ContextTypes.DEFAULT_TYPE):
    return bool(context.application.bot_data.get("auto_recovery", False))


def alerts_enabled_users(application: Application):
    disabled = application.bot_data.setdefault("alerts_disabled", set())
    return [user_id for user_id in ALLOWED_USERS if user_id not in disabled]


def metrics_history(application: Application):
    return application.bot_data.setdefault(
        "metrics_history",
        {
            "cpu": deque(maxlen=METRIC_HISTORY_SIZE),
            "ram": deque(maxlen=METRIC_HISTORY_SIZE),
        },
    )


def short_token(application: Application, prefix: str, payload):
    counter = application.bot_data.get("token_counter", 0) + 1
    application.bot_data["token_counter"] = counter
    token = f"{prefix}{counter:x}"
    application.bot_data.setdefault("tokens", {})[token] = payload
    return token


def token_payload(application: Application, token: str):
    return application.bot_data.setdefault("tokens", {}).get(token)


async def reply_text(update: Update, text: str, reply_markup=None):
    message = update.effective_message
    if message:
        await message.reply_text(limit_text(text), reply_markup=reply_markup)


async def edit_or_reply(query, text: str, reply_markup=None):
    text = limit_text(text)
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    except BadRequest:
        await query.message.reply_text(text=text, reply_markup=reply_markup)


async def tracked_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    bucket: str,
    text: str,
    reply_markup=None,
    extra=None,
):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    data = context.application.bot_data.setdefault(bucket, {})
    text = limit_text(text)
    record = data.get(user_id)
    if record and record.get("chat_id") == chat_id:
        try:
            await context.application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=record["message_id"],
                text=text,
                reply_markup=reply_markup,
            )
            record.update({"text": text, **(extra or {})})
            return
        except TelegramError:
            pass
    sent = await update.effective_message.reply_text(text, reply_markup=reply_markup)
    data[user_id] = {"chat_id": sent.chat_id, "message_id": sent.message_id, "text": text, **(extra or {})}


async def update_tracked_view(
    application: Application,
    bucket: str,
    user_id: int,
    text: str,
    reply_markup=None,
    extra=None,
):
    data = application.bot_data.setdefault(bucket, {})
    record = data.get(user_id)
    if not record:
        return
    text = limit_text(text)
    if record.get("text") == text and not extra:
        return
    try:
        await application.bot.edit_message_text(
            chat_id=record["chat_id"],
            message_id=record["message_id"],
            text=text,
            reply_markup=reply_markup,
        )
        record.update({"text": text, **(extra or {})})
    except TelegramError:
        pass


def limit_text(text: str, limit: int = MAX_MESSAGE_LENGTH):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 14].rstrip() + "\n... truncated"


def status_markup(live_enabled: bool):
    label = "Live: ON" if live_enabled else "Live: OFF"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Refresh", callback_data="status:refresh"),
                InlineKeyboardButton(label, callback_data="status:toggle_live"),
            ],
            [
                InlineKeyboardButton("Quick Actions", callback_data="menu:quick"),
                InlineKeyboardButton("Main Menu", callback_data="menu:main"),
            ],
        ]
    )


def main_menu_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("System Status", callback_data="menu:status"),
                InlineKeyboardButton("Services", callback_data="menu:services"),
            ],
            [
                InlineKeyboardButton("Downloads", callback_data="menu:downloads"),
                InlineKeyboardButton("Files", callback_data="menu:files"),
            ],
            [
                InlineKeyboardButton("Logs", callback_data="menu:logs"),
                InlineKeyboardButton("Processes", callback_data="menu:processes"),
            ],
            [
                InlineKeyboardButton("Power", callback_data="menu:power"),
                InlineKeyboardButton("Quick", callback_data="menu:quick"),
            ],
            [InlineKeyboardButton("Help", callback_data="menu:help")],
        ]
    )


def services_menu_markup():
    rows = [
        [
            InlineKeyboardButton("Overview", callback_data="svc:overview"),
            InlineKeyboardButton("Restart All", callback_data="global:restart_all"),
        ],
        [InlineKeyboardButton("Stop All", callback_data="global:stop_all")],
    ]
    for index in range(0, len(SERVICE_ORDER), 2):
        pair = SERVICE_ORDER[index:index + 2]
        rows.append([InlineKeyboardButton(SERVICE_LABELS[item], callback_data=f"svc:{item}") for item in pair])
    rows.append([InlineKeyboardButton("Main Menu", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def service_actions_markup(service: str):
    label = SERVICE_LABELS.get(service, service)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"Start {label}", callback_data=f"service:start:{service}"),
                InlineKeyboardButton(f"Stop {label}", callback_data=f"service:stop:{service}"),
            ],
            [
                InlineKeyboardButton(f"Restart {label}", callback_data=f"service:restart:{service}"),
                InlineKeyboardButton("Logs", callback_data=f"logview:{service}"),
            ],
            [InlineKeyboardButton("Back", callback_data="menu:services")],
        ]
    )


def downloads_menu_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Refresh Jobs", callback_data="downloads:refresh"),
                InlineKeyboardButton("How To Start", callback_data="downloads:help"),
            ],
            [
                InlineKeyboardButton("Open Downloads", callback_data="files:quick:downloads"),
                InlineKeyboardButton("Main Menu", callback_data="menu:main"),
            ],
        ]
    )


def files_home_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Downloads", callback_data="files:quick:downloads"),
                InlineKeyboardButton("Uploads", callback_data="files:quick:uploads"),
            ],
            [
                InlineKeyboardButton("Media", callback_data="files:quick:media"),
                InlineKeyboardButton("Home", callback_data="files:quick:home"),
            ],
            [InlineKeyboardButton("Main Menu", callback_data="menu:main")],
        ]
    )


def logs_menu_markup():
    rows = []
    for index in range(0, len(LOG_SERVICE_ORDER), 2):
        pair = LOG_SERVICE_ORDER[index:index + 2]
        rows.append([InlineKeyboardButton(SERVICE_LABELS.get(item, item.title()), callback_data=f"logview:{item}") for item in pair])
    rows.append([InlineKeyboardButton("Main Menu", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def processes_menu_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Refresh", callback_data="processes:refresh")],
            [InlineKeyboardButton("Main Menu", callback_data="menu:main")],
        ]
    )


def power_menu_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Power Save On", callback_data="power:save_on"),
                InlineKeyboardButton("Power Save Off", callback_data="power:save_off"),
            ],
            [InlineKeyboardButton("Battery", callback_data="power:battery")],
            [
                InlineKeyboardButton("Reboot", callback_data="confirm:reboot"),
                InlineKeyboardButton("Shutdown", callback_data="confirm:shutdown"),
            ],
            [InlineKeyboardButton("Main Menu", callback_data="menu:main")],
        ]
    )


def quick_menu_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Restart All", callback_data="global:restart_all"),
                InlineKeyboardButton("Stop All", callback_data="global:stop_all"),
            ],
            [
                InlineKeyboardButton("Check Disk", callback_data="quick:disk"),
                InlineKeyboardButton("Restart VPN", callback_data="quick:restart_vpn"),
            ],
            [InlineKeyboardButton("Clean Temp", callback_data="quick:cleanup")],
            [InlineKeyboardButton("Main Menu", callback_data="menu:main")],
        ]
    )


def help_menu_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Services", callback_data="menu:services"),
                InlineKeyboardButton("Downloads", callback_data="menu:downloads"),
            ],
            [
                InlineKeyboardButton("Files", callback_data="menu:files"),
                InlineKeyboardButton("Quick", callback_data="menu:quick"),
            ],
            [InlineKeyboardButton("Main Menu", callback_data="menu:main")],
        ]
    )


def confirm_markup(application: Application, kind: str, payload: dict, cancel_menu: str = "menu:main"):
    token = short_token(application, "c", {"kind": kind, "payload": payload, "cancel_menu": cancel_menu})
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm", callback_data=f"confirmdo:{token}"),
                InlineKeyboardButton("Cancel", callback_data=f"confirmcancel:{token}"),
            ]
        ]
    )


def downloads_help_text():
    return (
        "Downloads\n"
        "Start a new download with:\n"
        "/download <url_or_magnet>\n\n"
        "Examples:\n"
        "/download https://example.com/file.mkv\n"
        "/download magnet:?xt=urn:btih:...\n\n"
        "Use /downloads to monitor progress."
    )


def safe_mode_text(enabled: bool):
    return "Safe mode is ON. Delete, kill, reboot, shutdown, and run_command are blocked." if enabled else "Safe mode is OFF."


def align_line(label: str, value: str):
    return f"{label:<8} {value}"


def battery_present(data):
    battery = data.get("battery") or {}
    return bool(isinstance(battery, dict) and battery.get("present"))


def battery_state_text(battery):
    state = str((battery or {}).get("state") or "").strip().lower()
    if not state:
        return "unknown"
    if state == "charging":
        return "charging"
    if state == "discharging":
        return "on battery"
    if state == "full":
        return "full"
    if state == "not charging":
        return "plugged in"
    return state


def disk_marker(disk_pct: float):
    if disk_pct >= 92:
        return "!!"
    if disk_pct >= 85:
        return "!"
    return ""


def health_marker(health_status: str):
    status = (health_status or "").lower()
    if status == "critical":
        return " !!"
    if status == "warning":
        return " !"
    return ""


def power_marker(watts: float | None):
    if watts is None:
        return ""
    if watts >= 20:
        return " high"
    if watts >= 10:
        return " active"
    return " idle"


def extract_temperature(data):
    temps = data.get("temps") or {}
    if not isinstance(temps, dict) or not temps:
        return "--"
    values = []
    for value in temps.values():
        val = to_float(value, None)
        if val is not None:
            values.append(val)
    if not values:
        return "--"
    return f"{max(values):.0f}C"


def status_text(data, application: Application):
    cpu = to_float(data.get("cpu"), 0.0)
    ram = parse_percent((data.get("ram") or {}).get("pct"))
    disk = parse_percent((data.get("disk") or {}).get("pct"))
    temp = extract_temperature(data)
    vpn_ok = bool(data.get("gluetun"))
    vpn_ip = data.get("vpn_ip") or data.get("home_ip") or "--"
    vpn_line = f"OK ({vpn_ip})" if vpn_ok else "DOWN"
    health = data.get("health_score")
    health_status = data.get("health_status") or "--"
    electricity = data.get("electricity") or {}
    currency = electricity.get("currency") or "Rs"
    today_cost = electricity.get("today_cost")
    month_cost = electricity.get("month_cost")
    monthly_24x7_cost = electricity.get("monthly_24x7_cost")
    effective_watt = electricity.get("effective_watt", electricity.get("watts"))
    live_watts = electricity.get("live_watts", data.get("watts"))
    today_hours = electricity.get("today_hours") or "--"
    rate = electricity.get("rate")
    battery = data.get("battery") or {}

    def money(value):
        return f"{currency} {to_float(value):.2f}" if value is not None else "--"

    lines = [
        "SYSTEM STATUS",
        "",
        align_line("CPU", f"{cpu:.1f}%"),
        align_line("RAM", f"{ram:.1f}%"),
        align_line("Disk", f"{disk:.1f}% {disk_marker(disk)}".strip()),
        align_line("Temp", temp),
        align_line("Load", str(data.get("load") or "--")),
        "",
        f"VPN      {vpn_line}",
    ]
    if health is not None:
        lines.append(f"Health   {health}/100 ({health_status}){health_marker(health_status)}")

    lines.extend(
        [
            "",
            "POWER & COST",
            align_line("Live", f"{live_watts if live_watts is not None else '--'} W"),
            align_line("Estimate", f"{effective_watt if effective_watt is not None else '--'} W{power_marker(to_float(effective_watt, 0.0) if effective_watt is not None else None)}"),
            align_line("Today", f"{money(today_cost)}  ({today_hours})"),
            align_line("Month", money(month_cost)),
            align_line("24x7", money(monthly_24x7_cost)),
        ]
    )
    if battery_present(data):
        lines.append(align_line("Battery", f"{to_float(battery.get('pct')):.0f}% {battery_state_text(battery)}"))
        extras = []
        if battery.get("health"):
            extras.append(f"health {to_float(battery.get('health')):.0f}%")
        if battery.get("watts"):
            extras.append(f"{to_float(battery.get('watts')):.1f} W")
        if extras:
            lines.append(align_line("Batt Info", " | ".join(extras)))
    if rate is not None:
        lines.append(align_line("Rate", f"{currency} {to_float(rate):.2f}/kWh"))

    suggestions = []
    if disk >= 85:
        suggestions.append(f"Disk is high ({disk:.0f}%) -> Run cleanup? (/cleanup)")
    if cpu >= 85:
        suggestions.append(f"CPU is high ({cpu:.0f}%) -> Check processes (/processes)")
    if today_cost is not None and month_cost is not None and monthly_24x7_cost is not None:
        if to_float(month_cost) > 0 and to_float(monthly_24x7_cost) > to_float(month_cost) * 2:
            suggestions.append(f"Projected monthly cost is {money(monthly_24x7_cost)} -> Review power save? (/power_save_on)")
    if suggestions:
        lines.extend(["", "Suggestions"] + suggestions)

    lines.extend(
        [
            "",
            "BOT MODES",
            f"Alerts       {'ON' if alerts_enabled_users(application) else 'OFF'}",
            f"Safe Mode    {'ON' if application.bot_data.get('safe_mode') else 'OFF'}",
            f"Auto Recover {'ON' if application.bot_data.get('auto_recovery') else 'OFF'}",
        ]
    )
    return "\n".join(lines)


def battery_text(data):
    battery = data.get("battery") or {}
    if not battery_present(data):
        return "BATTERY\n\nNo battery detected on this host."
    lines = [
        "BATTERY",
        "",
        align_line("Level", f"{to_float(battery.get('pct')):.0f}%"),
        align_line("State", battery_state_text(battery)),
    ]
    if battery.get("health"):
        lines.append(align_line("Health", f"{to_float(battery.get('health')):.0f}%"))
    if battery.get("watts"):
        lines.append(align_line("Draw", f"{to_float(battery.get('watts')):.1f} W"))
    if data.get("watts") is not None:
        lines.append(align_line("System", f"{to_float(data.get('watts')):.1f} W"))
    uptime = data.get("uptime")
    if uptime:
        lines.append(align_line("Uptime", str(uptime)))
    return "\n".join(lines)


def service_status_summary(data):
    lines = ["SERVICES", ""]
    for service in SERVICE_ORDER:
        state = "running" if data.get(service) else "stopped"
        lines.append(f"{SERVICE_LABELS.get(service, service):<12} {state}")
    if data.get("ip_status"):
        lines.extend(["", f"VPN/IP       {data['ip_status']}"])
    return "\n".join(lines)


def format_jobs(jobs):
    if not isinstance(jobs, list) or not jobs:
        return "DOWNLOADS\n\nNo jobs found."
    active = [job for job in jobs if str(job.get("status", "")).lower() not in {"done", "failed", "cancelled", "partial", "interrupted", "unknown"}]
    display = active or sorted(jobs, key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)[:6]
    lines = ["DOWNLOADS", ""]
    for job in display[:8]:
        progress = job.get("progress") or {}
        pct = progress.get("pct")
        pct_text = f"{to_float(pct):.0f}%" if pct is not None else "--"
        status = str(job.get("status", "?")).upper()
        lines.append(f"Job {job.get('id', '?')}  {status}  {pct_text}")
        current = job.get("current_url") or (job.get("urls") or [""])[0]
        if current:
            lines.append(f"  {current[:88]}{'...' if len(current) > 88 else ''}")
        dest = actual_to_virtual(job.get("dest") or "")
        if dest:
            lines.append(f"  -> {dest}")
        lines.append("")
    return limit_text("\n".join(lines))


def build_ascii_graph(values, title: str):
    if not values:
        return f"{title}\nNo samples yet."
    palette = " .:-=+*#%@"
    sampled = list(values)[-50:]
    chars = []
    for value in sampled:
        idx = min(len(palette) - 1, max(0, int(round((to_float(value) / 100) * (len(palette) - 1)))))
        chars.append(palette[idx])
    return (
        f"{title}\n"
        "0%    25%    50%    75%   100%\n"
        + "".join(chars)
        + f"\nMin {min(sampled):.1f}%  Max {max(sampled):.1f}%  Now {sampled[-1]:.1f}%"
    )


def is_text_file(path: Path):
    mime, _ = mimetypes.guess_type(str(path))
    if mime and mime.startswith("text/"):
        return True
    return path.suffix.lower() in READABLE_TEXT_SUFFIXES


def read_text_preview(path: Path):
    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        raise ValueError("Binary file cannot be read as text")
    snippet = data[: MAX_READ_BYTES + 1]
    text = snippet.decode("utf-8", errors="replace")
    truncated = len(data) > MAX_READ_BYTES
    if truncated:
        text = text[:MAX_READ_BYTES] + "\n... truncated"
    return text


def cleanup_temp_files():
    root = upload_dir()
    removed = 0
    freed = 0
    now = time.time()
    for item in root.iterdir():
        try:
            if not item.is_file():
                continue
            stat = item.stat()
            old = now - stat.st_mtime > 7 * 24 * 3600
            temporary = item.suffix.lower() in {".tmp", ".part", ".partial"} or stat.st_size == 0
            if old or temporary:
                freed += stat.st_size
                item.unlink()
                removed += 1
        except Exception:
            continue
    return removed, freed


def register_file_path(application: Application, actual_path: str):
    return short_token(application, "p", {"path": actual_path})


def file_browser_text(payload):
    path_virtual = actual_to_virtual(payload.get("path") or "")
    summary = payload.get("summary") or {}
    lines = [
        "Files",
        f"Path: {path_virtual}",
        "Dirs: {dirs} | Files: {files} | Size: {size}".format(
            dirs=summary.get("dirs", 0),
            files=summary.get("files", 0),
            size=summary.get("total_size_str", "--"),
        ),
        "",
        "Tap a folder to open it. Tap file actions to download, move, delete, or rename.",
    ]
    return "\n".join(lines)


def file_browser_markup(application: Application, payload):
    rows = []
    parent = payload.get("parent")
    if parent:
        try:
            normalize_virtual_path(actual_to_virtual(parent))
            rows.append([InlineKeyboardButton(".. Parent", callback_data=f"nav:{register_file_path(application, parent)}")])
        except ValueError:
            pass

    entries = payload.get("entries") or []
    dirs = [entry for entry in entries if entry.get("type") == "dir"][:8]
    files = [entry for entry in entries if entry.get("type") == "file"][:6]

    for entry in dirs:
        token = register_file_path(application, entry["path"])
        rows.append([InlineKeyboardButton(f"[DIR] {entry['name']}", callback_data=f"nav:{token}")])

    for entry in files:
        token = register_file_path(application, entry["path"])
        size = entry.get("size") or 0
        large = " !" if size >= LARGE_FILE_BYTES else ""
        title = f"[FILE] {entry['name']} ({entry.get('size_str') or '--'}){large}"
        rows.append([InlineKeyboardButton(title[:60], callback_data=f"fileinfo:{token}")])
        rows.append(
            [
                InlineKeyboardButton("Download", callback_data=f"fileget:{token}"),
                InlineKeyboardButton("Move", callback_data=f"filemove:{token}"),
                InlineKeyboardButton("Delete", callback_data=f"filedel:{token}"),
                InlineKeyboardButton("Rename", callback_data=f"fileren:{token}"),
            ]
        )

    if len(entries) > len(dirs) + len(files):
        rows.append([InlineKeyboardButton("List is truncated - use /files <path> for more", callback_data="noop")])
    rows.append(
        [
            InlineKeyboardButton("Refresh", callback_data=f"nav:{register_file_path(application, payload['path'])}"),
            InlineKeyboardButton("Locations", callback_data="menu:files"),
        ]
    )
    rows.append([InlineKeyboardButton("Main Menu", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


async def api(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["api"]


async def api_status(context: ContextTypes.DEFAULT_TYPE):
    return await (await api(context)).get_json("/status", timeout=STATUS_API_TIMEOUT)


async def api_action(context: ContextTypes.DEFAULT_TYPE, action: str):
    return await (await api(context)).post_json("/action", {"action": action})


async def run_service_action(context: ContextTypes.DEFAULT_TYPE, service: str, verb: str):
    mapping = SERVICE_ACTIONS.get(service)
    if not mapping or verb not in mapping:
        raise ValueError("Invalid service")
    actions = mapping[verb]
    if isinstance(actions, str):
        actions = (actions,)
    result = {"ok": True, "msg": "Done"}
    for action in actions:
        result = await api_action(context, action)
        if not (result.get("ok") or result.get("success")):
            break
    return result


def format_action_result(payload, default_ok="Done", default_error="Action failed"):
    success = bool(payload.get("ok") or payload.get("success"))
    message = payload.get("msg") or payload.get("message") or payload.get("error") or (default_ok if success else default_error)
    prefix = OK if success else ERROR
    return f"{prefix} {message}"


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE, *, live_override=None):
    payload = await api_status(context)
    live_enabled = context.application.bot_data.setdefault("status_views", {}).get(update.effective_user.id, {}).get("live", False)
    if live_override is not None:
        live_enabled = live_override
    await tracked_message(
        update,
        context,
        "status_views",
        status_text(payload, context.application),
        reply_markup=status_markup(live_enabled),
        extra={"live": live_enabled},
    )


async def show_downloads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = await (await api(context)).get_json("/download/jobs")
    await tracked_message(update, context, "download_views", format_jobs(payload), reply_markup=downloads_menu_markup())


async def show_file_browser(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_path: str):
    actual_path, _ = normalize_virtual_path(raw_path)
    payload = await (await api(context)).get_json("/files/list", params={"path": actual_path})
    if payload.get("ok") is False or payload.get("success") is False:
        await reply_text(update, format_action_result(payload))
        return
    await reply_text(update, file_browser_text(payload), reply_markup=file_browser_markup(context.application, payload))


async def show_file_browser_query(query, context: ContextTypes.DEFAULT_TYPE, actual_path: str):
    payload = await (await api(context)).get_json("/files/list", params={"path": actual_path})
    if payload.get("ok") is False or payload.get("success") is False:
        await edit_or_reply(query, format_action_result(payload), files_home_markup())
        return
    await edit_or_reply(query, file_browser_text(payload), file_browser_markup(context.application, payload))


async def send_file_to_chat(application: Application, chat_id: int, actual_path: str):
    path = Path(actual_path)
    if not path.exists() or not path.is_file():
        raise ValueError("File not found")
    size = path.stat().st_size
    if size > MAX_SEND_BYTES:
        return f"File too large, path: {actual_to_virtual(actual_path)}"
    with path.open("rb") as handle:
        await application.bot.send_document(
            chat_id=chat_id,
            document=handle,
            filename=path.name,
            caption=f"{actual_to_virtual(actual_path)}\n{human_size(size)}",
        )
    return f"{OK} Sent {actual_to_virtual(actual_path)}"


def parse_move_segments(args):
    raw = " ".join(args).strip()
    if not raw:
        raise ValueError("Usage: /move <src1> | <src2> | <destination_folder>")
    segments = [segment.strip() for segment in raw.split("|") if segment.strip()]
    if len(segments) < 2:
        raise ValueError("Usage: /move <src1> | <src2> | <destination_folder>")
    return segments[:-1], segments[-1]


def format_move_result(payload):
    text = format_action_result(payload, default_ok="Move completed", default_error="Move failed")
    errors = payload.get("errors") or []
    if errors:
        details = []
        for item in errors[:5]:
            path = item.get("path") or "?"
            details.append(f"- {actual_to_virtual(path)} -> {item.get('error') or 'Failed'}")
        text += "\n\nIssues\n" + "\n".join(details)
        if len(errors) > 5:
            text += f"\n... {len(errors) - 5} more"
    return text


async def execute_confirmed_action(application: Application, token: str):
    payload = token_payload(application, token)
    if not payload:
        return f"{ERROR} Confirmation expired", main_menu_markup()
    kind = payload.get("kind")
    data = payload.get("payload") or {}
    api_client = application.bot_data["api"]

    if application.bot_data.get("safe_mode") and kind in {"shutdown", "reboot", "delete", "kill"}:
        return f"{ERROR} Safe mode is ON", main_menu_markup()

    if kind == "shutdown":
        result = await api_client.post_json("/action", {"action": "shutdown"})
        return format_action_result(result), power_menu_markup()
    if kind == "reboot":
        result = await api_client.post_json("/action", {"action": "reboot"})
        return format_action_result(result), power_menu_markup()
    if kind == "delete":
        result = await api_client.post_json("/files/delete", {"path": data["actual_path"]})
        return format_action_result(result), files_home_markup()
    if kind == "kill":
        result = await api_client.post_json("/kill_process", {"pid": data["pid"], "signal": "TERM"})
        return format_action_result(result), processes_menu_markup()
    return f"{ERROR} Invalid confirmation", main_menu_markup()


def parse_service_name(raw: str):
    raw = (raw or "").strip().lower()
    if not raw:
        return None
    return SERVICE_ALIASES.get(raw)


def parse_log_service(raw: str):
    raw = (raw or "").strip().lower()
    if not raw:
        return None
    return LOG_SERVICE_ALIASES.get(raw)


def parse_rename_args(args):
    if len(args) < 2:
        raise ValueError("Usage: /rename <path> <new_name>")
    return " ".join(args[:-1]), args[-1].strip()


@authorized_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Seedbox bot is ready.\n\n"
        "Main sections:\n"
        "System Status\n"
        "Services\n"
        "Downloads\n"
        "Files\n"
        "Logs\n"
        "Processes\n"
        "Power\n"
        "Quick Actions"
    )
    await reply_text(update, text, reply_markup=main_menu_markup())


@authorized_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_text(update, HELP_TEXT, reply_markup=help_menu_markup())


@authorized_only
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_text(update, "Choose a section below.", reply_markup=main_menu_markup())


@authorized_only
async def quick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_text(update, "Quick actions", reply_markup=quick_menu_markup())


@authorized_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_status(update, context)


@authorized_only
async def downloads_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_downloads(update, context)


@authorized_only
async def battery_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = await api_status(context)
    await reply_text(update, battery_text(payload), reply_markup=power_menu_markup())


@authorized_only
async def processes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = await (await api(context)).get_json("/processes")
    text = format_processes(payload)
    await reply_text(update, text, reply_markup=processes_menu_markup())


def format_processes(processes):
    if not isinstance(processes, list) or not processes:
        return "Top Processes\nNo process data available."
    lines = ["Top Processes"]
    for proc in processes[:12]:
        lines.append(
            "{pid} {name} | CPU {cpu}% | MEM {mem}% | {time}".format(
                pid=proc.get("pid", "?"),
                name=proc.get("name", "?"),
                cpu=proc.get("cpu", "?"),
                mem=proc.get("mem", "?"),
                time=proc.get("time", ""),
            )
        )
    return limit_text("\n".join(lines))


@authorized_only
async def start_service_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await service_command(update, context, "start")


@authorized_only
async def stop_service_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await service_command(update, context, "stop")


@authorized_only
async def restart_service_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await service_command(update, context, "restart")


async def service_command(update: Update, context: ContextTypes.DEFAULT_TYPE, verb: str):
    service = parse_service_name(" ".join(context.args))
    if not service:
        raise ValueError("Usage: /{}_service <docker|jellyfin|qbittorrent|gluetun|wetty|filebrowser|neko|samba>".format(verb))
    result = await run_service_action(context, service, verb)
    await reply_text(update, format_action_result(result), reply_markup=service_actions_markup(service))


@authorized_only
async def restart_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = await api_action(context, "restart_seedbox")
    await reply_text(update, format_action_result(result), reply_markup=services_menu_markup())


@authorized_only
async def stop_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = await api_action(context, "stop_seedbox")
    await reply_text(update, format_action_result(result), reply_markup=services_menu_markup())


@authorized_only
async def reboot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if safe_mode_enabled(context):
        raise ValueError(safe_mode_text(True))
    markup = confirm_markup(context.application, "reboot", {}, cancel_menu="menu:power")
    await reply_text(update, "Confirm reboot?", reply_markup=markup)


@authorized_only
async def shutdown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if safe_mode_enabled(context):
        raise ValueError(safe_mode_text(True))
    markup = confirm_markup(context.application, "shutdown", {}, cancel_menu="menu:power")
    await reply_text(update, "Confirm shutdown?", reply_markup=markup)


@authorized_only
async def power_save_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = await api_action(context, "power_save")
    await reply_text(update, format_action_result(result), reply_markup=power_menu_markup())


@authorized_only
async def power_save_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = await api_action(context, "restore_power_save")
    await reply_text(update, format_action_result(result), reply_markup=power_menu_markup())


@authorized_only
async def alerts_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data.setdefault("alerts_disabled", set()).discard(update.effective_user.id)
    await reply_text(update, f"{OK} Alerts enabled")


@authorized_only
async def alerts_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data.setdefault("alerts_disabled", set()).add(update.effective_user.id)
    await reply_text(update, f"{OK} Alerts disabled")


@authorized_only
async def safe_mode_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["safe_mode"] = True
    await reply_text(update, safe_mode_text(True))


@authorized_only
async def safe_mode_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["safe_mode"] = False
    await reply_text(update, safe_mode_text(False))


@authorized_only
async def auto_recovery_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["auto_recovery"] = True
    await reply_text(update, f"{OK} Auto recovery enabled")


@authorized_only
async def auto_recovery_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["auto_recovery"] = False
    await reply_text(update, f"{OK} Auto recovery disabled")


@authorized_only
async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = " ".join(context.args).strip()
    if not target:
        raise ValueError("Usage: /download <url_or_magnet>")
    payload = await (await api(context)).post_json(
        "/download/start",
        {"urls": target, "dest": DEFAULT_DOWNLOAD_DEST, "method": "auto"},
    )
    text = format_action_result(payload, default_ok="Download started", default_error="Download failed")
    if payload.get("job_id"):
        text += f"\nJob ID: {payload['job_id']}"
    await reply_text(update, text, reply_markup=downloads_menu_markup())


@authorized_only
async def cancel_download_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    job_id = " ".join(context.args).strip()
    if not job_id:
        raise ValueError("Usage: /cancel_download <job_id>")
    payload = await (await api(context)).post_json(f"/download/cancel/{job_id}")
    await reply_text(update, format_action_result(payload), reply_markup=downloads_menu_markup())


@authorized_only
async def log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service = parse_log_service(" ".join(context.args))
    if not service:
        raise ValueError("Usage: /log <service>")
    lines = await (await api(context)).stream_lines(f"/logs/{service}", max_lines=220)
    text = "No logs available." if not lines else f"Logs: {SERVICE_LABELS.get(service, service)}\n" + "\n".join(lines[-50:])
    await reply_text(update, text, reply_markup=logs_menu_markup())


@authorized_only
async def files_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_path = " ".join(context.args).strip() or DEFAULT_FILES_PATH
    await show_file_browser(update, context, raw_path)


@authorized_only
async def mkdir_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_path = " ".join(context.args).strip()
    if not raw_path:
        raise ValueError("Usage: /mkdir <path>")
    actual_path, virtual_path = normalize_virtual_path(raw_path)
    normalized = PurePosixPath(virtual_path)
    if str(normalized) == "/":
        raise ValueError("Invalid path")
    base_virtual = str(normalized.parent)
    name = normalized.name
    base_actual, _ = normalize_virtual_path(base_virtual)
    payload = await (await api(context)).post_json("/files/mkdir", {"path": base_actual, "name": name})
    await reply_text(update, format_action_result(payload), reply_markup=files_home_markup())


@authorized_only
async def rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    old_raw, new_name = parse_rename_args(context.args)
    if "/" in new_name or "\\" in new_name or not new_name:
        raise ValueError("Invalid new file name")
    target, _ = ensure_local_path(old_raw, must_exist=True)
    payload = await (await api(context)).post_json("/files/rename", {"path": str(target), "name": new_name})
    await reply_text(update, format_action_result(payload), reply_markup=files_home_markup())


@authorized_only
async def move_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source_raw_paths, destination_raw = parse_move_segments(context.args)
    destination_target, _ = ensure_local_path(destination_raw, must_exist=True, expect_file=False)
    actual_paths = []
    for raw_path in source_raw_paths:
        target, _ = ensure_local_path(raw_path, must_exist=True)
        actual_paths.append(str(target))
    payload = await (await api(context)).post_json("/files/move", {"paths": actual_paths, "destination": str(destination_target)})
    await reply_text(update, format_move_result(payload), reply_markup=files_home_markup())


@authorized_only
async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if safe_mode_enabled(context):
        raise ValueError(safe_mode_text(True))
    raw_path = " ".join(context.args).strip()
    target, virtual_path = ensure_local_path(raw_path, must_exist=True)
    markup = confirm_markup(
        context.application,
        "delete",
        {"actual_path": str(target), "virtual_path": virtual_path},
        cancel_menu="menu:files",
    )
    await reply_text(update, f"Confirm delete?\n{virtual_path}", reply_markup=markup)


@authorized_only
async def get_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_path = " ".join(context.args).strip()
    target, _ = ensure_local_path(raw_path, must_exist=True, expect_file=True)
    message = await send_file_to_chat(context.application, update.effective_chat.id, str(target))
    if message:
        await reply_text(update, message, reply_markup=files_home_markup())


@authorized_only
async def read_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_path = " ".join(context.args).strip()
    target, virtual_path = ensure_local_path(raw_path, must_exist=True, expect_file=True)
    if target.stat().st_size > MAX_READ_BYTES * 4:
        raise ValueError("File is too large to read safely")
    if not is_text_file(target):
        raise ValueError("Only text files can be read")
    await reply_text(update, f"{virtual_path}\n\n{read_text_preview(target)}", reply_markup=files_home_markup())


@authorized_only
async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if safe_mode_enabled(context):
        raise ValueError(safe_mode_text(True))
    pid = " ".join(context.args).strip()
    if not pid.isdigit():
        raise ValueError("Usage: /kill <pid>")
    markup = confirm_markup(context.application, "kill", {"pid": pid}, cancel_menu="menu:processes")
    await reply_text(update, f"Confirm kill of PID {pid}?", reply_markup=markup)


@authorized_only
async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    removed, freed = cleanup_temp_files()
    await reply_text(update, f"{OK} Cleaned {removed} temp file(s), freed {human_size(freed)}", reply_markup=quick_menu_markup())


@authorized_only
async def cpu_graph_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history = metrics_history(context.application)["cpu"]
    await reply_text(update, build_ascii_graph(history, "CPU History"))


@authorized_only
async def ram_graph_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history = metrics_history(context.application)["ram"]
    await reply_text(update, build_ascii_graph(history, "RAM History"))


@authorized_only
async def document_upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.effective_message.document
    if not document:
        return
    if document.file_size and document.file_size > MAX_UPLOAD_BYTES:
        await reply_text(update, f"{ERROR} Upload limit is 20MB")
        return
    target_dir = upload_dir()
    filename = sanitize_filename(document.file_name or "upload.bin")
    destination = target_dir / filename
    if destination.exists():
        destination = target_dir / f"{int(time.time())}_{filename}"
    telegram_file = await document.get_file()
    await telegram_file.download_to_drive(custom_path=str(destination))
    await reply_text(
        update,
        f"{OK} Uploaded to {actual_to_virtual(str(destination))}\nSize: {human_size(destination.stat().st_size)}",
        reply_markup=files_home_markup(),
    )


@authorized_only
async def text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.application.bot_data.setdefault("pending_input", {}).get(update.effective_user.id)
    if not pending:
        return
    message = (update.effective_message.text or "").strip()
    if not message:
        return
    if pending["kind"] == "rename":
        new_name = sanitize_filename(message)
        if not new_name or new_name == "upload.bin":
            await reply_text(update, f"{ERROR} Invalid new name")
            return
        payload = await (await api(context)).post_json("/files/rename", {"path": pending["actual_path"], "name": new_name})
        context.application.bot_data["pending_input"].pop(update.effective_user.id, None)
        await reply_text(update, format_action_result(payload), reply_markup=files_home_markup())
        return
    if pending["kind"] == "move_destination":
        destination_target, _ = ensure_local_path(message, must_exist=True, expect_file=False)
        payload = await (await api(context)).post_json(
            "/files/move",
            {"paths": pending["actual_paths"], "destination": str(destination_target)},
        )
        context.application.bot_data["pending_input"].pop(update.effective_user.id, None)
        await reply_text(update, format_move_result(payload), reply_markup=files_home_markup())


async def notify_enabled_users(application: Application, text: str):
    for user_id in alerts_enabled_users(application):
        try:
            await application.bot.send_message(chat_id=user_id, text=limit_text(text))
        except TelegramError:
            log.exception("Could not send alert to %s", user_id)


async def refresh_live_status_views(application: Application, payload):
    views = application.bot_data.setdefault("status_views", {})
    for user_id, view in list(views.items()):
        if not view.get("live"):
            continue
        text = status_text(payload, application)
        await update_tracked_view(application, "status_views", user_id, text, reply_markup=status_markup(True), extra={"live": True})


async def alert_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    api_client = app.bot_data["api"]
    try:
        status = await api_client.get_json("/status", timeout=STATUS_API_TIMEOUT)
    except Exception:
        log.exception("Status alert poll failed")
        return

    history = metrics_history(app)
    history["cpu"].append(to_float(status.get("cpu"), 0.0))
    history["ram"].append(parse_percent((status.get("ram") or {}).get("pct")))
    await refresh_live_status_views(app, status)

    state = app.bot_data.setdefault(
        "alert_state",
        {
            "disk": "normal",
            "vpn_down": False,
            "cpu_high_count": 0,
            "cpu_alerted": False,
            "battery_low": False,
        },
    )

    disk_pct = parse_percent((status.get("disk") or {}).get("pct"))
    current_disk_state = "critical" if disk_pct > 92 else "warning" if disk_pct > 85 else "normal"
    if current_disk_state != state["disk"]:
        if current_disk_state == "critical":
            await notify_enabled_users(app, f"{WARN} Disk usage is critical at {disk_pct:.1f}%")
        elif current_disk_state == "warning":
            await notify_enabled_users(app, f"{WARN} Disk usage is high at {disk_pct:.1f}%")
        elif state["disk"] != "normal":
            await notify_enabled_users(app, f"{OK} Disk usage returned to normal at {disk_pct:.1f}%")
        state["disk"] = current_disk_state

    vpn_down = not bool(status.get("gluetun"))
    if vpn_down != state["vpn_down"]:
        if vpn_down:
            await notify_enabled_users(app, f"{WARN} VPN is down")
        else:
            await notify_enabled_users(app, f"{OK} VPN recovered")
        state["vpn_down"] = vpn_down

    cpu = to_float(status.get("cpu"), 0.0)
    if cpu > CPU_ALERT_THRESHOLD:
        state["cpu_high_count"] += 1
        if state["cpu_high_count"] >= CPU_ALERT_SUSTAINED_CHECKS and not state["cpu_alerted"]:
            await notify_enabled_users(app, f"{WARN} CPU stayed above {CPU_ALERT_THRESHOLD:.0f}% ({cpu:.1f}%)")
            state["cpu_alerted"] = True
    else:
        if state["cpu_alerted"] and cpu < 85:
            await notify_enabled_users(app, f"{OK} CPU usage recovered to {cpu:.1f}%")
        state["cpu_high_count"] = 0
        state["cpu_alerted"] = False

    battery = status.get("battery") or {}
    battery_low = (
        bool(battery.get("present"))
        and str(battery.get("state") or "").lower() == "discharging"
        and to_float(battery.get("pct"), 0.0) <= 20
    )
    if battery_low and not state["battery_low"]:
        await notify_enabled_users(
            app,
            f"{WARN} Battery low: {to_float(battery.get('pct'), 0.0):.0f}% ({battery_state_text(battery)})",
        )
    elif state["battery_low"] and not battery_low:
        await notify_enabled_users(app, f"{OK} Battery status recovered")
    state["battery_low"] = battery_low

    if auto_recovery_enabled(context):
        recovery = app.bot_data.setdefault("auto_recovery_state", {"vpn_last": 0, "qbit_last": 0})
        now = time.time()
        if vpn_down and now - recovery["vpn_last"] > 600:
            try:
                result = await api_client.post_json("/action", {"action": "restart_gluetun"})
                recovery["vpn_last"] = now
                await notify_enabled_users(app, f"{WARN} Auto recovery: {format_action_result(result)}")
            except Exception:
                log.exception("VPN auto recovery failed")


async def download_monitor_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    api_client = app.bot_data["api"]
    try:
        jobs = await api_client.get_json("/download/jobs")
    except Exception:
        log.exception("Download poll failed")
        return

    views = app.bot_data.setdefault("download_views", {})
    text = format_jobs(jobs)
    for user_id in list(views.keys()):
        await update_tracked_view(app, "download_views", user_id, text, reply_markup=downloads_menu_markup())

    state = app.bot_data.setdefault("download_state", {})
    seen = set()
    stalled_detected = False
    for job in jobs if isinstance(jobs, list) else []:
        job_id = job.get("id")
        if not job_id:
            continue
        seen.add(job_id)
        current = state.setdefault(
            job_id,
            {"pct": 0.0, "status": "", "stagnant": 0, "notified_pct": 0.0, "stall_alerted": False},
        )
        progress = job.get("progress") or {}
        pct = to_float(progress.get("pct"), 0.0)
        status_name = str(job.get("status", "")).lower()

        if status_name == current["status"] and abs(pct - current["pct"]) < 0.1 and status_name not in {"done"}:
            current["stagnant"] += 1
        else:
            current["stagnant"] = 0

        if pct >= current["notified_pct"] + DOWNLOAD_PROGRESS_NOTIFY_STEP and status_name not in DOWNLOAD_FAILURE_STATES and status_name != "done":
            await notify_enabled_users(app, f"{OK} Download {job_id} reached {pct:.0f}%")
            current["notified_pct"] = pct

        if status_name == "done" and current["status"] != "done":
            await notify_enabled_users(app, f"{OK} Download finished: {job_id}")
        elif status_name in DOWNLOAD_FAILURE_STATES and status_name != current["status"]:
            await notify_enabled_users(app, f"{WARN} Download failed or stalled: {job_id} ({status_name})")

        if current["stagnant"] >= DOWNLOAD_STALL_INTERVALS and not current["stall_alerted"] and status_name not in DOWNLOAD_FAILURE_STATES and status_name != "done":
            stalled_detected = True
            await notify_enabled_users(app, f"{WARN} Download stalled: {job_id}")
            current["stall_alerted"] = True

        current["pct"] = pct
        current["status"] = status_name

    for job_id in list(state.keys()):
        if job_id not in seen:
            state.pop(job_id, None)

    if stalled_detected and app.bot_data.get("auto_recovery"):
        recovery = app.bot_data.setdefault("auto_recovery_state", {"vpn_last": 0, "qbit_last": 0})
        now = time.time()
        if now - recovery["qbit_last"] > 900:
            try:
                result = await api_client.post_json("/action", {"action": "restart_qbittorrent"})
                recovery["qbit_last"] = now
                await notify_enabled_users(app, f"{WARN} Auto recovery: {format_action_result(result)}")
            except Exception:
                log.exception("qBittorrent auto recovery failed")


@authorized_only
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data or ""

    if data == "noop":
        return
    if data == "menu:main":
        await edit_or_reply(query, "Choose a section below.", main_menu_markup())
        return
    if data == "menu:help":
        await edit_or_reply(query, HELP_TEXT, help_menu_markup())
        return
    if data == "menu:quick":
        await edit_or_reply(query, "Quick actions", quick_menu_markup())
        return
    if data == "menu:status":
        payload = await api_status(context)
        context.application.bot_data.setdefault("status_views", {})[update.effective_user.id] = {
            "chat_id": query.message.chat_id,
            "message_id": query.message.message_id,
            "text": status_text(payload, context.application),
            "live": context.application.bot_data.setdefault("status_views", {}).get(update.effective_user.id, {}).get("live", False),
        }
        await edit_or_reply(query, status_text(payload, context.application), status_markup(context.application.bot_data["status_views"][update.effective_user.id]["live"]))
        return
    if data == "status:refresh":
        payload = await api_status(context)
        live = context.application.bot_data.setdefault("status_views", {}).get(update.effective_user.id, {}).get("live", False)
        await edit_or_reply(query, status_text(payload, context.application), status_markup(live))
        return
    if data == "status:toggle_live":
        views = context.application.bot_data.setdefault("status_views", {})
        record = views.setdefault(update.effective_user.id, {"chat_id": query.message.chat_id, "message_id": query.message.message_id, "text": ""})
        record["live"] = not record.get("live", False)
        payload = await api_status(context)
        await edit_or_reply(query, status_text(payload, context.application), status_markup(record["live"]))
        return
    if data == "menu:services":
        await edit_or_reply(query, "Services", services_menu_markup())
        return
    if data == "svc:overview":
        payload = await api_status(context)
        await edit_or_reply(query, service_status_summary(payload), services_menu_markup())
        return
    if data.startswith("svc:"):
        service = data.split(":", 1)[1]
        await edit_or_reply(query, f"Service: {SERVICE_LABELS.get(service, service)}", service_actions_markup(service))
        return
    if data.startswith("service:"):
        _, verb, service = data.split(":", 2)
        result = await run_service_action(context, service, verb)
        await edit_or_reply(query, format_action_result(result), service_actions_markup(service))
        return
    if data == "global:restart_all":
        result = await api_action(context, "restart_seedbox")
        await edit_or_reply(query, format_action_result(result), services_menu_markup())
        return
    if data == "global:stop_all":
        result = await api_action(context, "stop_seedbox")
        await edit_or_reply(query, format_action_result(result), services_menu_markup())
        return
    if data == "menu:downloads":
        jobs = await (await api(context)).get_json("/download/jobs")
        context.application.bot_data.setdefault("download_views", {})[update.effective_user.id] = {
            "chat_id": query.message.chat_id,
            "message_id": query.message.message_id,
            "text": format_jobs(jobs),
        }
        await edit_or_reply(query, format_jobs(jobs), downloads_menu_markup())
        return
    if data == "downloads:refresh":
        jobs = await (await api(context)).get_json("/download/jobs")
        await edit_or_reply(query, format_jobs(jobs), downloads_menu_markup())
        return
    if data == "downloads:help":
        await edit_or_reply(query, downloads_help_text(), downloads_menu_markup())
        return
    if data == "menu:files":
        await edit_or_reply(query, "Files\nChoose a safe location below.", files_home_markup())
        return
    if data.startswith("files:quick:"):
        key = data.split(":", 2)[2]
        target = QUICK_PATHS.get(key)
        if not target:
            await edit_or_reply(query, f"{ERROR} Unknown location", files_home_markup())
            return
        actual_path, _ = normalize_virtual_path(target[0])
        await show_file_browser_query(query, context, actual_path)
        return
    if data.startswith("nav:"):
        token = data.split(":", 1)[1]
        payload = token_payload(context.application, token)
        if not payload:
            await edit_or_reply(query, f"{ERROR} Navigation expired", files_home_markup())
            return
        await show_file_browser_query(query, context, payload["path"])
        return
    if data.startswith("fileinfo:"):
        token = data.split(":", 1)[1]
        payload = token_payload(context.application, token)
        if not payload:
            await edit_or_reply(query, f"{ERROR} File entry expired", files_home_markup())
            return
        target = Path(payload["path"])
        if not target.exists():
            await edit_or_reply(query, f"{ERROR} File not found", files_home_markup())
            return
        text = (
            f"File: {actual_to_virtual(str(target))}\n"
            f"Size: {human_size(target.stat().st_size)}"
        )
        await edit_or_reply(
            query,
            text,
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Download", callback_data=f"fileget:{token}"),
                        InlineKeyboardButton("Move", callback_data=f"filemove:{token}"),
                        InlineKeyboardButton("Delete", callback_data=f"filedel:{token}"),
                        InlineKeyboardButton("Rename", callback_data=f"fileren:{token}"),
                    ],
                    [InlineKeyboardButton("Back", callback_data=f"nav:{register_file_path(context.application, str(target.parent))}")],
                ]
            ),
        )
        return
    if data.startswith("fileget:"):
        token = data.split(":", 1)[1]
        payload = token_payload(context.application, token)
        if not payload:
            await edit_or_reply(query, f"{ERROR} File entry expired", files_home_markup())
            return
        message = await send_file_to_chat(context.application, query.message.chat_id, payload["path"])
        if message:
            await query.message.reply_text(limit_text(message))
        return
    if data.startswith("filedel:"):
        if safe_mode_enabled(context):
            await edit_or_reply(query, f"{ERROR} {safe_mode_text(True)}", files_home_markup())
            return
        token = data.split(":", 1)[1]
        payload = token_payload(context.application, token)
        if not payload:
            await edit_or_reply(query, f"{ERROR} File entry expired", files_home_markup())
            return
        target = Path(payload["path"])
        markup = confirm_markup(
            context.application,
            "delete",
            {"actual_path": str(target), "virtual_path": actual_to_virtual(str(target))},
            cancel_menu="menu:files",
        )
        await edit_or_reply(query, f"Confirm delete?\n{actual_to_virtual(str(target))}", markup)
        return
    if data.startswith("filemove:"):
        token = data.split(":", 1)[1]
        payload = token_payload(context.application, token)
        if not payload:
            await edit_or_reply(query, f"{ERROR} File entry expired", files_home_markup())
            return
        target = Path(payload["path"])
        context.application.bot_data.setdefault("pending_input", {})[update.effective_user.id] = {
            "kind": "move_destination",
            "actual_paths": [str(target)],
        }
        await edit_or_reply(
            query,
            "Send the destination folder for:\n{}\n\nExample: /downloads/archive".format(actual_to_virtual(str(target))),
            files_home_markup(),
        )
        return
    if data.startswith("fileren:"):
        token = data.split(":", 1)[1]
        payload = token_payload(context.application, token)
        if not payload:
            await edit_or_reply(query, f"{ERROR} File entry expired", files_home_markup())
            return
        target = Path(payload["path"])
        context.application.bot_data.setdefault("pending_input", {})[update.effective_user.id] = {
            "kind": "rename",
            "actual_path": str(target),
        }
        await edit_or_reply(query, f"Send the new name for:\n{actual_to_virtual(str(target))}", files_home_markup())
        return
    if data == "menu:logs":
        await edit_or_reply(query, "Pick a service log.", logs_menu_markup())
        return
    if data.startswith("logview:"):
        service = data.split(":", 1)[1]
        lines = await (await api(context)).stream_lines(f"/logs/{service}", max_lines=220)
        text = "No logs available." if not lines else f"Logs: {SERVICE_LABELS.get(service, service)}\n" + "\n".join(lines[-50:])
        await edit_or_reply(query, text, logs_menu_markup())
        return
    if data == "menu:processes":
        payload = await (await api(context)).get_json("/processes")
        await edit_or_reply(query, format_processes(payload), processes_menu_markup())
        return
    if data == "processes:refresh":
        payload = await (await api(context)).get_json("/processes")
        await edit_or_reply(query, format_processes(payload), processes_menu_markup())
        return
    if data == "menu:power":
        await edit_or_reply(query, "Power controls", power_menu_markup())
        return
    if data == "power:save_on":
        result = await api_action(context, "power_save")
        await edit_or_reply(query, format_action_result(result), power_menu_markup())
        return
    if data == "power:save_off":
        result = await api_action(context, "restore_power_save")
        await edit_or_reply(query, format_action_result(result), power_menu_markup())
        return
    if data == "power:battery":
        payload = await api_status(context)
        await edit_or_reply(query, battery_text(payload), power_menu_markup())
        return
    if data == "confirm:reboot":
        if safe_mode_enabled(context):
            await edit_or_reply(query, f"{ERROR} {safe_mode_text(True)}", power_menu_markup())
            return
        await edit_or_reply(query, "Confirm reboot?", confirm_markup(context.application, "reboot", {}, cancel_menu="menu:power"))
        return
    if data == "confirm:shutdown":
        if safe_mode_enabled(context):
            await edit_or_reply(query, f"{ERROR} {safe_mode_text(True)}", power_menu_markup())
            return
        await edit_or_reply(query, "Confirm shutdown?", confirm_markup(context.application, "shutdown", {}, cancel_menu="menu:power"))
        return
    if data.startswith("confirmdo:"):
        token = data.split(":", 1)[1]
        text, markup = await execute_confirmed_action(context.application, token)
        await edit_or_reply(query, text, markup)
        return
    if data.startswith("confirmcancel:"):
        token = data.split(":", 1)[1]
        payload = token_payload(context.application, token) or {}
        cancel_menu = payload.get("cancel_menu", "menu:main")
        if cancel_menu == "menu:power":
            await edit_or_reply(query, "Cancelled.", power_menu_markup())
        elif cancel_menu == "menu:processes":
            await edit_or_reply(query, "Cancelled.", processes_menu_markup())
        elif cancel_menu == "menu:files":
            await edit_or_reply(query, "Cancelled.", files_home_markup())
        else:
            await edit_or_reply(query, "Cancelled.", main_menu_markup())
        return
    if data == "quick:disk":
        payload = await api_status(context)
        disk = parse_percent((payload.get("disk") or {}).get("pct"))
        await edit_or_reply(query, f"Disk usage: {disk:.1f}% {disk_marker(disk)}".strip(), quick_menu_markup())
        return
    if data == "quick:restart_vpn":
        result = await api_action(context, "restart_gluetun")
        await edit_or_reply(query, format_action_result(result), quick_menu_markup())
        return
    if data == "quick:cleanup":
        removed, freed = cleanup_temp_files()
        await edit_or_reply(query, f"{OK} Cleaned {removed} temp file(s), freed {human_size(freed)}", quick_menu_markup())
        return
    await edit_or_reply(query, f"{ERROR} Unknown action", main_menu_markup())


async def post_init(application: Application):
    upload_dir()
    application.bot_data["api"] = DashboardAPI(API_BASE, timeout=API_TIMEOUT)
    application.bot_data.setdefault("safe_mode", False)
    application.bot_data.setdefault("auto_recovery", False)
    application.bot_data.setdefault("alerts_disabled", set())
    metrics_history(application)
    application.job_queue.run_repeating(alert_job, interval=ALERT_INTERVAL_SECONDS, first=10)
    application.job_queue.run_repeating(download_monitor_job, interval=DOWNLOAD_INTERVAL_SECONDS, first=15)


async def post_shutdown(application: Application):
    api_client = application.bot_data.get("api")
    if api_client:
        await api_client.close()


def build_application():
    allowed_filter = filters.User(user_id=ALLOWED_USERS)
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command, filters=allowed_filter))
    app.add_handler(CommandHandler("help", help_command, filters=allowed_filter))
    app.add_handler(CommandHandler("menu", menu_command, filters=allowed_filter))
    app.add_handler(CommandHandler("quick", quick_command, filters=allowed_filter))
    app.add_handler(CommandHandler("status", status_command, filters=allowed_filter))
    app.add_handler(CommandHandler("s", status_command, filters=allowed_filter))
    app.add_handler(CommandHandler("battery", battery_command, filters=allowed_filter))
    app.add_handler(CommandHandler("downloads", downloads_command, filters=allowed_filter))
    app.add_handler(CommandHandler("d", downloads_command, filters=allowed_filter))
    app.add_handler(CommandHandler("processes", processes_command, filters=allowed_filter))
    app.add_handler(CommandHandler("p", processes_command, filters=allowed_filter))
    app.add_handler(CommandHandler("start_service", start_service_command, filters=allowed_filter))
    app.add_handler(CommandHandler("stop_service", stop_service_command, filters=allowed_filter))
    app.add_handler(CommandHandler("restart_service", restart_service_command, filters=allowed_filter))
    app.add_handler(CommandHandler("restart_all", restart_all_command, filters=allowed_filter))
    app.add_handler(CommandHandler("r", restart_all_command, filters=allowed_filter))
    app.add_handler(CommandHandler("stop_all", stop_all_command, filters=allowed_filter))
    app.add_handler(CommandHandler("reboot", reboot_command, filters=allowed_filter))
    app.add_handler(CommandHandler("shutdown", shutdown_command, filters=allowed_filter))
    app.add_handler(CommandHandler("power_save_on", power_save_on_command, filters=allowed_filter))
    app.add_handler(CommandHandler("power_save_off", power_save_off_command, filters=allowed_filter))
    app.add_handler(CommandHandler("alerts_on", alerts_on_command, filters=allowed_filter))
    app.add_handler(CommandHandler("alerts_off", alerts_off_command, filters=allowed_filter))
    app.add_handler(CommandHandler("safe_mode_on", safe_mode_on_command, filters=allowed_filter))
    app.add_handler(CommandHandler("safe_mode_off", safe_mode_off_command, filters=allowed_filter))
    app.add_handler(CommandHandler("auto_recovery_on", auto_recovery_on_command, filters=allowed_filter))
    app.add_handler(CommandHandler("auto_recovery_off", auto_recovery_off_command, filters=allowed_filter))
    app.add_handler(CommandHandler("download", download_command, filters=allowed_filter))
    app.add_handler(CommandHandler("dl", download_command, filters=allowed_filter))
    app.add_handler(CommandHandler("cancel_download", cancel_download_command, filters=allowed_filter))
    app.add_handler(CommandHandler("log", log_command, filters=allowed_filter))
    app.add_handler(CommandHandler("files", files_command, filters=allowed_filter))
    app.add_handler(CommandHandler("mkdir", mkdir_command, filters=allowed_filter))
    app.add_handler(CommandHandler("rename", rename_command, filters=allowed_filter))
    app.add_handler(CommandHandler("move", move_command, filters=allowed_filter))
    app.add_handler(CommandHandler("mv", move_command, filters=allowed_filter))
    app.add_handler(CommandHandler("delete", delete_command, filters=allowed_filter))
    app.add_handler(CommandHandler("get", get_command, filters=allowed_filter))
    app.add_handler(CommandHandler("read", read_command, filters=allowed_filter))
    app.add_handler(CommandHandler("kill", kill_command, filters=allowed_filter))
    app.add_handler(CommandHandler("cleanup", cleanup_command, filters=allowed_filter))
    app.add_handler(CommandHandler("cpu_graph", cpu_graph_command, filters=allowed_filter))
    app.add_handler(CommandHandler("ram_graph", ram_graph_command, filters=allowed_filter))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(MessageHandler(allowed_filter & filters.Document.ALL, document_upload_handler))
    app.add_handler(MessageHandler(allowed_filter & filters.TEXT & ~filters.COMMAND, text_input_handler))
    return app


def main():
    if BOT_TOKEN == "PUT_BOT_TOKEN_HERE":
        raise SystemExit("Set BOT_TOKEN in telegram_bot.py or SEEDBOX_TELEGRAM_BOT_TOKEN before running.")
    if not ALLOWED_USERS or ALLOWED_USERS == [0]:
        raise SystemExit("Set ALLOWED_USERS in telegram_bot.py or SEEDBOX_TELEGRAM_ALLOWED_USERS before running.")

    application = build_application()
    log.info("Starting Telegram bot")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
