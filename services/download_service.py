import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
import hashlib
from datetime import datetime
from pathlib import Path

from flask import Response, stream_with_context

from services import file_service
from services.system_service import (
    DOWNLOAD_SCRIPT,
    KEEP_OUTPUT_LINES,
    MAX_OUTPUT_LINES,
    MOUNT_POINT_BASE,
    PRESET_DIRS,
    STATE_DIR,
    WEBUI_PORT,
    VPN_PROXY,
    has_tool,
    human_size,
    legacy_error_payload,
    run,
    safe_path,
    strip_ansi,
    build_status_snapshot,
    get_processes,
)

JOB_DB = STATE_DIR / "jobs.sqlite3"
VIKI_SCRIPT_CANDIDATES = [
    os.environ.get("SEEDBOX_VIKI_SCRIPT", "").strip(),
    "/home/amit/Downloads/torrent/viki_subs_download.sh",
    "/home/amit/torrent/viki_subs_download.sh",
    "/Users/amith/Downloads/torrent/viki_subs_download.sh",
]
_jobs = {}
_jobs_lock = threading.Lock()
_db_lock = threading.Lock()
_initialized = False

YTDLP_RE = re.compile(r"\[download\]\s+([\d.]+%)\s+of\s+([\d.]+\S+)\s+at\s+([\d.]+\S+/s)\s+ETA\s+(\S+)")
ARIA2_RE = re.compile(r"\[#\S+\s+([\d.]+\S+)/([\d.]+\S+)\((\d+%)\)\s+CN:\d+\s+DL:([\d.]+\S+/s)")
WGET_RE = re.compile(r"(\d+)%\s+[\d.]+[KMGT]?\s+([\d.]+[KMGT]?/s)")
VIKI_NAME_RE = re.compile(r'^\s*([A-Z_]+)="([^"]*)"', re.MULTILINE)
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".webm", ".m4v"}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
SUBTITLE_CONVERT_EXTS = {".ass", ".ssa", ".vtt", ".sub"}
MEDIA_SIDECAR_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".nfo", ".jpg", ".jpeg", ".png"}
ARCHIVE_FORMATS = {"zip": "zip", "tar.gz": "gztar"}
JELLYFIN_URL = os.environ.get("SEEDBOX_JELLYFIN_URL", "http://127.0.0.1:8096").strip()
JELLYFIN_API_KEY = os.environ.get("SEEDBOX_JELLYFIN_API_KEY", "").strip()


def _db():
    conn = sqlite3.connect(str(JOB_DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_db():
    with _db_lock:
        conn = _db()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS download_jobs (
                    id TEXT PRIMARY KEY,
                    command TEXT,
                    status TEXT,
                    progress TEXT,
                    output_tail TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    payload TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def _serialize_job(job):
    payload = {
        "kind": job.get("kind", "download"),
        "label": job.get("label", ""),
        "rerun_data": job.get("rerun_data", {}),
        "urls": job.get("urls", []),
        "total": job.get("total", 0),
        "dest": job.get("dest", ""),
        "method": job.get("method", "auto"),
        "pid": job.get("pid"),
        "current_idx": job.get("current_idx", 0),
        "current_url": job.get("current_url", ""),
        "current_cmd": job.get("current_cmd", ""),
        "failed_urls": job.get("failed_urls", []),
        "verified_files": job.get("verified_files", []),
        "started": job.get("started"),
        "finished": job.get("finished"),
    }
    return json.dumps(payload)


def _public_job(job):
    clean = dict(job)
    clean.pop("rerun_data", None)
    return clean


def _persist_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        output_tail = "\n".join(job.get("output", [])[-KEEP_OUTPUT_LINES:])
        progress = json.dumps(job.get("progress", {}))
        updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        job["updated_at"] = updated_at
        created_at = job.get("created_at") or updated_at

    with _db_lock:
        conn = _db()
        try:
            conn.execute(
                """
                INSERT INTO download_jobs (id, command, status, progress, output_tail, created_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    command=excluded.command,
                    status=excluded.status,
                    progress=excluded.progress,
                    output_tail=excluded.output_tail,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    payload=excluded.payload
                """,
                (
                    job["id"],
                    job.get("current_cmd") or job.get("command") or job.get("method") or "",
                    job.get("status", "queued"),
                    progress,
                    output_tail,
                    created_at,
                    updated_at,
                    _serialize_job(job),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def _load_jobs():
    with _db_lock:
        conn = _db()
        try:
            rows = conn.execute("SELECT * FROM download_jobs ORDER BY created_at ASC").fetchall()
        finally:
            conn.close()

    loaded = {}
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        status = row["status"]
        if status in {"queued", "running"}:
            status = "interrupted"
        loaded[row["id"]] = {
            "id": row["id"],
            "kind": payload.get("kind", "download"),
            "label": payload.get("label", ""),
            "rerun_data": payload.get("rerun_data", {}),
            "status": status,
            "urls": payload.get("urls", []),
            "total": payload.get("total", 0),
            "dest": payload.get("dest", ""),
            "method": payload.get("method", "auto"),
            "output": (row["output_tail"] or "").splitlines()[-KEEP_OUTPUT_LINES:],
            "pid": None,
            "current_idx": payload.get("current_idx", 0),
            "current_url": payload.get("current_url", ""),
            "current_cmd": payload.get("current_cmd") or row["command"] or "",
            "failed_urls": payload.get("failed_urls", []),
            "progress": json.loads(row["progress"] or "{}"),
            "verified_files": payload.get("verified_files", []),
            "started": payload.get("started"),
            "finished": payload.get("finished"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "command": row["command"],
        }
        if row["status"] in {"queued", "running"}:
            loaded[row["id"]]["output"].append("Recovered after restart: previous job state was interrupted.")
    return loaded


def init_download_store():
    global _initialized
    if _initialized:
        return
    _initialized = True
    _ensure_db()
    with _jobs_lock:
        _jobs.clear()
        _jobs.update(_load_jobs())
    for job_id in list(_jobs.keys()):
        _persist_job(job_id)


def _disk_free_gb(path):
    try:
        return shutil.disk_usage(path).free / 1024 ** 3
    except Exception:
        return 999.0


def _push_output(job_id, line, is_progress=False):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        if is_progress:
            if job["output"] and job["output"][-1].startswith(">>"):
                job["output"][-1] = line
            else:
                job["output"].append(line)
        else:
            job["output"].append(line)
        if len(job["output"]) > MAX_OUTPUT_LINES:
            job["output"] = job["output"][-KEEP_OUTPUT_LINES:]
    _persist_job(job_id)


def _set_job_fields(job_id, **updates):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.update(updates)
    _persist_job(job_id)


def _parse_progress(line):
    match = YTDLP_RE.search(line)
    if match:
        pct, size, speed, eta = match.groups()
        return (
            ">> {}  of {}  at {}  ETA {}".format(pct, size, speed, eta),
            {"pct": float(pct.rstrip("%")), "size": size, "speed": speed, "eta": eta},
        )
    match = ARIA2_RE.search(line)
    if match:
        done, total, pct, speed = match.groups()
        return (
            ">> {}  {}/{}  at {}".format(pct, done, total, speed),
            {"pct": float(pct.rstrip("%")), "size": total, "speed": speed, "eta": "?"},
        )
    match = WGET_RE.search(line)
    if match:
        pct, speed = match.groups()
        return ">> {}%  at {}".format(pct, speed), {"pct": float(pct), "speed": speed, "eta": "?"}
    return None, {}


def _qbit_save_path(dest):
    dest = (dest or "").strip()
    if dest == "/mnt/exstore":
        return "/downloads"
    if dest.startswith("/mnt/exstore/"):
        return "/downloads/" + dest[len("/mnt/exstore/"):]
    return dest


def _pixeldrain_api_url(url):
    match = re.search(r"https?://pixeldrain\.com/(?:u|api/file)/([A-Za-z0-9]+)", url or "", re.IGNORECASE)
    if not match:
        return None
    return "https://pixeldrain.com/api/file/{}".format(match.group(1))


def _should_bypass_download_script(urls, method):
    if method not in ("auto", "direct", "curl", "wget", "aria2c", "ytdlp"):
        return False
    for url in urls or []:
        if _pixeldrain_api_url(url):
            return True
    return False


def _build_cmds(url, dest, method, proxy=""):
    lowered = url.lower().split("?")[0]
    if method == "auto":
        direct_exts = (
            ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv",
            ".mp3", ".flac", ".wav", ".m4a", ".aac",
            ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
            ".iso", ".img", ".deb", ".rpm", ".exe", ".dmg",
            ".jpg", ".jpeg", ".png", ".gif", ".pdf",
        )
        url_method = "direct" if any(lowered.endswith(ext) for ext in direct_exts) else "auto"
    else:
        url_method = method

    p_arg = "--proxy {}".format(proxy) if proxy else ""
    all_proxy = "--all-proxy={}".format(proxy) if proxy else ""
    wget_proxy = "-e use_proxy=yes -e https_proxy={0} -e http_proxy={0}".format(proxy) if proxy else ""
    tag = " [VPN]" if proxy else ""
    filename = re.sub(r'[/<>:"|?*\\]', "_", os.path.basename(url.split("?")[0]) or "dl_{}".format(int(time.time())))
    outfile = os.path.join(dest, filename)
    commands = []
    pixeldrain_url = _pixeldrain_api_url(url)

    if url.startswith("magnet:"):
        commands.append(
            (
                "qbittorrent",
                "curl -s 'http://localhost:{}/api/v2/torrents/add' -F 'urls={}' -F 'savepath={}'".format(
                    WEBUI_PORT, url, _qbit_save_path(dest)
                ),
            )
        )
        return commands

    if pixeldrain_url:
        if has_tool("curl"):
            commands.append(
                (
                    "pixeldrain-curl{}".format(tag),
                    "cd '{}' && curl -fL --retry 5 --retry-delay 5 --retry-all-errors "
                    "--user-agent 'Mozilla/5.0' {} -J -O '{}'".format(dest, p_arg, pixeldrain_url),
                )
            )
        if has_tool("wget"):
            commands.append(
                (
                    "pixeldrain-wget{}".format(tag),
                    "cd '{}' && wget --content-disposition --trust-server-names "
                    "--tries=5 --waitretry=5 --user-agent='Mozilla/5.0' {} '{}' 2>&1".format(dest, wget_proxy, pixeldrain_url),
                )
            )
        if has_tool("yt-dlp"):
            commands.append(
                (
                    "yt-dlp{}".format(tag),
                    "yt-dlp --continue --no-part --retries 5 --fragment-retries 5 "
                    "--merge-output-format mkv --add-metadata --newline {} -P '{}' '{}'".format(p_arg, dest, url),
                )
            )
        return commands

    if has_tool("yt-dlp") and has_tool("aria2c"):
        commands.append(
            (
                "yt-dlp+aria2c{}".format(tag),
                "yt-dlp --continue --no-part --retries 5 --fragment-retries 5 "
                "--external-downloader aria2c "
                "--external-downloader-args 'aria2c:-x 16 -s 16 -k 1M --retry-wait=5' "
                "--merge-output-format mkv --add-metadata --newline {} -P '{}' '{}'".format(p_arg, dest, url),
            )
        )
    if has_tool("yt-dlp"):
        commands.append(
            (
                "yt-dlp{}".format(tag),
                "yt-dlp --continue --no-part --retries 5 --fragment-retries 5 "
                "--merge-output-format mkv --add-metadata --newline {} -P '{}' '{}'".format(p_arg, dest, url),
            )
        )
    if url_method == "auto" and has_tool("gallery-dl"):
        commands.append(("gallery-dl{}".format(tag), "gallery-dl --directory '{}' --retries 5 {} '{}'".format(dest, p_arg, url)))
    if has_tool("aria2c"):
        commands.append(
            (
                "aria2c{}".format(tag),
                "aria2c -x 16 -s 16 -k 1M --retry-wait=5 --max-tries=5 "
                "--continue=true --auto-file-renaming=false --console-log-level=notice "
                "--summary-interval=5 --dir='{}' {} '{}'".format(dest, all_proxy, url),
            )
        )
    if has_tool("wget"):
        commands.append(
            (
                "wget{}".format(tag),
                "wget --continue --tries=5 --waitretry=5 --progress=bar:force "
                "--directory-prefix='{}' --user-agent='Mozilla/5.0' {} '{}' 2>&1".format(dest, wget_proxy, url),
            )
        )
    if has_tool("curl"):
        commands.append(
            (
                "curl{}".format(tag),
                "curl -L -C - --retry 5 --retry-delay 5 --retry-all-errors --progress-bar "
                "--user-agent 'Mozilla/5.0' {} -o '{}' '{}'".format(p_arg, outfile, url),
            )
        )
    if not commands:
        commands.append(("wget-fallback", "wget -O '{}' '{}' 2>&1".format(outfile, url)))
    return commands


def _run_cmd(job_id, tool, cmd):
    _push_output(job_id, "  -> {}".format(tool))
    _set_job_fields(job_id, current_cmd=tool, command=tool)
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        _set_job_fields(job_id, pid=proc.pid)
        assert proc.stdout is not None
        for raw in proc.stdout:
            clean = strip_ansi(raw.rstrip())
            if not clean.strip():
                continue
            progress_line, progress_data = _parse_progress(clean)
            if progress_line:
                _push_output(job_id, progress_line, is_progress=True)
                _set_job_fields(job_id, progress=progress_data)
            else:
                _push_output(job_id, clean)
        proc.wait()
        _set_job_fields(job_id, pid=None)
        return proc.returncode
    except Exception as exc:
        _push_output(job_id, "  ERROR: {}".format(exc))
        _set_job_fields(job_id, pid=None)
        return 1


def _verify_downloads(job_id, dest, marker_time):
    try:
        new_files = []
        skip_exts = {".json", ".aria2", ".part", ".ytdl", ".tmp", ".log"}
        for path in sorted(Path(dest).rglob("*")):
            if path.is_file() and path.suffix not in skip_exts:
                try:
                    if path.stat().st_mtime >= marker_time - 5:
                        size = path.stat().st_size
                        if size > 0:
                            new_files.append(
                                {
                                    "name": path.name,
                                    "size": human_size(size),
                                    "size_raw": size,
                                    "path": str(path),
                                }
                            )
                except Exception:
                    pass
        if new_files:
            _push_output(job_id, "  Downloaded files:")
            total = 0
            for item in new_files:
                _push_output(job_id, "    {} ({})".format(item["name"], item["size"]))
                total += item["size_raw"]
            _push_output(job_id, "    Total: {}".format(human_size(total)))
            _set_job_fields(
                job_id,
                verified_files=[{"name": item["name"], "size": item["size"], "path": item["path"]} for item in new_files],
            )
        else:
            _push_output(job_id, "  Warning: No new files detected")
    except Exception as exc:
        _push_output(job_id, "  Verify error: {}".format(exc))


def _candidate_viki_script():
    for candidate in VIKI_SCRIPT_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def get_viki_defaults():
    defaults = {
        "base_url": "https://api.viki.io/v4/videos",
        "app_param": "app=100000a",
        "stream_id": "129923624",
        "token": "",
        "lang": "en",
        "ext": "v",
        "script_path": "",
    }
    script = _candidate_viki_script()
    if not script:
        return defaults
    defaults["script_path"] = str(script)
    try:
        content = script.read_text(errors="replace")
    except Exception:
        return defaults
    found = dict(VIKI_NAME_RE.findall(content))
    defaults["base_url"] = found.get("BASE_URL") or defaults["base_url"]
    defaults["app_param"] = found.get("APP_PARAM") or defaults["app_param"]
    defaults["stream_id"] = found.get("STREAM_ID") or defaults["stream_id"]
    token = found.get("TOKEN") or defaults["token"]
    defaults["token"] = "" if token == "PASTE_YOUR_TOKEN_HERE" else token
    defaults["lang"] = found.get("LANG") or defaults["lang"]
    return defaults


def _parse_first_video_id(first_id, ext):
    first_id = str(first_id or "").strip()
    ext = str(ext or "").strip() or None
    match = re.match(r"^(\d+)([A-Za-z]+)?$", first_id)
    if not match:
        raise ValueError("First video id must look like 1267229v")
    number = int(match.group(1))
    inferred_ext = match.group(2) or ""
    final_ext = ext or inferred_ext or "v"
    return number, final_ext


def _viki_url(base_url, video_id, lang, app_param, stream_id, token):
    query = [str(app_param or "").strip(), "stream_id={}".format(stream_id)]
    if str(token or "").strip():
        query.append("token={}".format(token))
    return "{}/{}/auth_subtitles/{}.vtt?{}".format(
        base_url.rstrip("/"),
        video_id,
        lang,
        "&".join(part for part in query if part),
    )


def _same_file_contents(path_a, path_b):
    try:
        return Path(path_a).read_bytes() == Path(path_b).read_bytes()
    except Exception:
        return False


def _natural_key(value):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(value or ""))]


def _episode_token(text):
    text = str(text or "")
    patterns = [
        r"(s\d{1,2}e\d{1,3})",
        r"(\d{1,2}x\d{1,3})",
        r"(ep(?:isode)?[ ._-]?\d{1,3})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return re.sub(r"[^a-z0-9]", "", match.group(1).lower())
    return ""


def _normalize_media_stem(text):
    value = re.sub(r"[\W_]+", " ", str(text or "").lower()).strip()
    value = re.sub(r"\b(eng|english|en|sub|subs|subtitle|subtitles|forced|cc|sdh|default)\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _subtitle_suffix(stem):
    tail = re.split(r"[ ._-]+", str(stem or "").strip())
    if not tail:
        return ""
    last = tail[-1].lower()
    if re.fullmatch(r"[a-z]{2,3}", last):
        return last
    if last in {"forced", "sdh", "cc", "default", "english", "eng"}:
        return last
    return ""


def _safe_rename_target(parent, filename):
    cleaned = re.sub(r"\s+", " ", filename).strip()
    return Path(parent) / cleaned


def _media_clean_name(name):
    path = Path(name)
    stem = path.stem.replace(".", " ").replace("_", " ")
    stem = re.sub(r"\s*-\s*", " - ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    stem = re.sub(r"\bS(\d{1,2}) E(\d{1,3})\b", lambda m: "S{}E{}".format(m.group(1).zfill(2), m.group(2).zfill(2)), stem)
    return "{}{}".format(stem, path.suffix)


def _iter_files(folder, recursive=True):
    base = Path(folder)
    if recursive:
        for path in base.rglob("*"):
            if path.is_file():
                yield path
    else:
        for path in base.iterdir():
            if path.is_file():
                yield path


def _safe_job_time_label():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _report_filename(prefix, period, ext="md"):
    return "{}-{}-{}.{}".format(prefix, period, _safe_job_time_label(), ext.lstrip("."))


def _file_hash(path):
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _video_key(path):
    return _normalize_media_stem(Path(path).stem)


def _is_temp_candidate(path):
    name = path.name.lower()
    return (
        name in {".ds_store", "thumbs.db"}
        or name.endswith((".tmp", ".temp", ".part", ".crdownload", ".aria2", ".pyc", ".pyo", ".cache"))
        or name.startswith("~$")
    )


def _is_log_candidate(path):
    name = path.name.lower()
    return (
        name.endswith((".log", ".out", ".err"))
        or ".log." in name
        or (name.endswith(".gz") and "log" in name)
    )


def _download_viki_subtitle(job_id, config, final_name, video_id, summary):
    dest = config["dest"]
    srt_file = Path(dest) / "{}.srt".format(final_name)
    vtt_file = Path(dest) / ".{}.download.vtt".format(final_name)
    temp_srt = Path(dest) / ".{}.download.srt".format(final_name)
    existed_before = srt_file.exists()

    url = _viki_url(config["base_url"], video_id, config["lang"], config["app_param"], config["stream_id"], config["token"])
    _push_output(job_id, "Downloading {}".format(final_name))

    curl_cmd = "curl -sS -L --max-time 20 -w '%{{http_code}}' -o {} {}".format(
        shlex.quote(str(vtt_file)),
        shlex.quote(url),
    )
    out, err, rc = run(curl_cmd, timeout=30)
    http_code = (out or "").strip()
    if rc != 0 or http_code != "200":
        _push_output(job_id, "ERROR at {} (HTTP {})".format(final_name, http_code or rc))
        for tmp in (vtt_file, temp_srt):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        summary["failed"].append(srt_file.name)
        return False

    ffmpeg_cmd = "ffmpeg -loglevel error -y -i {} {}".format(
        shlex.quote(str(vtt_file)),
        shlex.quote(str(temp_srt)),
    )
    _, err, rc = run(ffmpeg_cmd, timeout=30)
    try:
        vtt_file.unlink(missing_ok=True)
    except Exception:
        pass
    if rc != 0:
        _push_output(job_id, "ERROR converting {}: {}".format(final_name, err or "ffmpeg failed"))
        try:
            temp_srt.unlink(missing_ok=True)
        except Exception:
            pass
        summary["failed"].append(srt_file.name)
        return False

    if existed_before:
        if _same_file_contents(srt_file, temp_srt):
            _push_output(job_id, "Unchanged {}".format(srt_file.name))
            summary["unchanged"].append(srt_file.name)
            try:
                temp_srt.unlink(missing_ok=True)
            except Exception:
                pass
            return True
        _push_output(job_id, "Updated {}".format(srt_file.name))
        summary["updated"].append(srt_file.name)
    else:
        _push_output(job_id, "Created {}".format(srt_file.name))
        summary["created"].append(srt_file.name)

    temp_srt.replace(srt_file)
    return True


def _run_viki_job(job_id, config):
    dest = config["dest"]
    try:
        os.makedirs(dest, exist_ok=True)
    except Exception as exc:
        _push_output(job_id, "ERROR: Cannot create destination: {}".format(exc))
        _set_job_fields(job_id, status="failed", finished=time.strftime("%H:%M:%S"))
        return

    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="viki-subs")
    _push_output(job_id, "  Destination: {}".format(dest))
    _push_output(job_id, "  Language: {} | stream_id: {} | ext: {}".format(config["lang"], config["stream_id"], config["ext"]))
    failed = False
    current_video_num = config["start_num"]
    special_eps = set(config["special_eps"])
    summary = {"created": [], "updated": [], "unchanged": [], "failed": []}

    for ep in range(1, config["ep_count"] + 1):
        ep_padded = "{:02d}".format(ep)
        video_id = "{}{}".format(current_video_num, config["ext"])
        final_name = config["base_name"].replace("##", ep_padded)
        _set_job_fields(job_id, current_idx=ep, current_url=video_id, progress={"pct": round((ep - 1) * 100 / max(config["ep_count"], 1), 1)})
        if not _download_viki_subtitle(job_id, config, final_name, video_id, summary):
            failed = True
            break

        current_video_num += 1
        if ep in special_eps:
            special_video_id = "{}{}".format(current_video_num, config["ext"])
            special_name = "{}.SPECIAL".format(final_name)
            if not _download_viki_subtitle(job_id, config, special_name, special_video_id, summary):
                failed = True
                break
            current_video_num += 1

    _set_job_fields(job_id, current_idx=config["ep_count"], progress={"pct": 100.0} if not failed else {})
    if any(summary.values()):
        _push_output(job_id, "-" * 50)
        if summary["created"]:
            _push_output(job_id, "  Created: {}".format(", ".join(summary["created"])))
        if summary["updated"]:
            _push_output(job_id, "  Updated: {}".format(", ".join(summary["updated"])))
        if summary["unchanged"]:
            _push_output(job_id, "  Unchanged: {}".format(", ".join(summary["unchanged"])))
        if summary["failed"]:
            _push_output(job_id, "  Failed: {}".format(", ".join(summary["failed"])))
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- {}".format(job_id, "DONE" if not failed else "FAILED"))
    _set_job_fields(job_id, status="done" if not failed else "failed", finished=time.strftime("%H:%M:%S"), pid=None)


def _run_subtitle_shift_job(job_id, config):
    folder = config["folder"]
    seconds = config["seconds"]
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="subtitle-shift")
    _push_output(job_id, "  Folder: {}".format(folder))
    _push_output(job_id, "  Shift: {} seconds".format(seconds))

    try:
        subtitle_files = sorted(
            entry for entry in Path(folder).iterdir()
            if entry.is_file() and entry.suffix.lower() == ".srt"
        )
    except Exception as exc:
        _push_output(job_id, "ERROR: {}".format(exc))
        _set_job_fields(job_id, status="failed", finished=time.strftime("%H:%M:%S"), pid=None)
        return

    if not subtitle_files:
        _push_output(job_id, "ERROR: No .srt files found in this folder")
        _set_job_fields(job_id, status="failed", finished=time.strftime("%H:%M:%S"), pid=None)
        return

    updated = []
    unchanged = []
    failed = []
    backups = []

    total = len(subtitle_files)
    for idx, subtitle_file in enumerate(subtitle_files, start=1):
        _set_job_fields(
            job_id,
            current_idx=idx,
            current_url=str(subtitle_file),
            progress={"pct": round((idx - 1) * 100 / max(total, 1), 1)},
        )
        _push_output(job_id, "[{}/{}] {}".format(idx, total, subtitle_file.name))
        payload, status = file_service.shift_subtitle(str(subtitle_file), seconds)
        if status == 200 and payload.get("ok"):
            if payload.get("changed"):
                updated.append(subtitle_file.name)
                if payload.get("backup_path"):
                    backups.append(Path(payload["backup_path"]).name)
                _push_output(job_id, "  Updated {}{}".format(
                    subtitle_file.name,
                    " -> backup {}".format(Path(payload["backup_path"]).name) if payload.get("backup_path") else "",
                ))
            else:
                unchanged.append(subtitle_file.name)
                _push_output(job_id, "  Unchanged {}".format(subtitle_file.name))
        else:
            failed.append(subtitle_file.name)
            _push_output(job_id, "  Failed {}: {}".format(
                subtitle_file.name,
                payload.get("msg") or payload.get("error") or "unknown error",
            ))

    final_status = "done" if not failed else ("partial" if len(failed) < total else "failed")
    _set_job_fields(job_id, current_idx=total, progress={"pct": 100.0})
    _push_output(job_id, "-" * 50)
    if updated:
        _push_output(job_id, "  Updated: {}".format(", ".join(updated)))
    if unchanged:
        _push_output(job_id, "  Unchanged: {}".format(", ".join(unchanged)))
    if failed:
        _push_output(job_id, "  Failed: {}".format(", ".join(failed)))
    if backups:
        _push_output(job_id, "  Backups: {}".format(", ".join(backups[:8]) + (" ..." if len(backups) > 8 else "")))
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- {}".format(job_id, final_status.upper()))
    _set_job_fields(job_id, status=final_status, finished=time.strftime("%H:%M:%S"), pid=None)


def _run_subtitle_convert_job(job_id, config):
    folder = config["folder"]
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="subtitle-convert")
    _push_output(job_id, "  Folder: {}".format(folder))
    sources = sorted(
        [entry for entry in Path(folder).iterdir() if entry.is_file() and entry.suffix.lower() in SUBTITLE_CONVERT_EXTS],
        key=lambda p: _natural_key(p.name),
    )
    if not sources:
        _push_output(job_id, "ERROR: No convertible subtitle files found")
        _set_job_fields(job_id, status="failed", finished=time.strftime("%H:%M:%S"), pid=None)
        return

    created, updated, unchanged, failed = [], [], [], []
    total = len(sources)
    for idx, source in enumerate(sources, start=1):
        _set_job_fields(job_id, current_idx=idx, current_url=str(source), progress={"pct": round((idx - 1) * 100 / max(total, 1), 1)})
        target = source.with_suffix(".srt")
        temp_target = source.with_name(".{}.converted.srt".format(source.stem))
        _push_output(job_id, "[{}/{}] {}".format(idx, total, source.name))
        _, err, rc = run(
            "ffmpeg -loglevel error -y -i {} {}".format(shlex.quote(str(source)), shlex.quote(str(temp_target))),
            timeout=45,
        )
        if rc != 0 or not temp_target.exists():
            failed.append(source.name)
            _push_output(job_id, "  Failed: {}".format(err or "ffmpeg conversion failed"))
            try:
                temp_target.unlink(missing_ok=True)
            except Exception:
                pass
            continue
        if target.exists():
            if _same_file_contents(target, temp_target):
                unchanged.append(target.name)
                _push_output(job_id, "  Unchanged {}".format(target.name))
                temp_target.unlink(missing_ok=True)
                continue
            backup = Path(str(target) + ".bak")
            shutil.copyfile(str(target), str(backup))
            temp_target.replace(target)
            updated.append(target.name)
            _push_output(job_id, "  Updated {} -> backup {}".format(target.name, backup.name))
        else:
            temp_target.replace(target)
            created.append(target.name)
            _push_output(job_id, "  Created {}".format(target.name))

    final_status = "done" if not failed else ("partial" if len(failed) < total else "failed")
    _set_job_fields(job_id, progress={"pct": 100.0})
    _push_output(job_id, "-" * 50)
    if created:
        _push_output(job_id, "  Created: {}".format(", ".join(created)))
    if updated:
        _push_output(job_id, "  Updated: {}".format(", ".join(updated)))
    if unchanged:
        _push_output(job_id, "  Unchanged: {}".format(", ".join(unchanged)))
    if failed:
        _push_output(job_id, "  Failed: {}".format(", ".join(failed)))
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- {}".format(job_id, final_status.upper()))
    _set_job_fields(job_id, status=final_status, finished=time.strftime("%H:%M:%S"), pid=None)


def _match_subtitle_targets(folder):
    videos = sorted(
        [entry for entry in Path(folder).iterdir() if entry.is_file() and entry.suffix.lower() in VIDEO_EXTS],
        key=lambda p: _natural_key(p.name),
    )
    subtitles = sorted(
        [entry for entry in Path(folder).iterdir() if entry.is_file() and entry.suffix.lower() in SUBTITLE_EXTS],
        key=lambda p: _natural_key(p.name),
    )
    if not videos or not subtitles:
        return videos, subtitles, {}

    matched = {}
    remaining_subs = []
    video_by_token = {}
    for video in videos:
        token = _episode_token(video.stem)
        if token:
            video_by_token.setdefault(token, []).append(video)
    video_norm = {video: _normalize_media_stem(video.stem) for video in videos}

    for subtitle in subtitles:
        target_video = None
        token = _episode_token(subtitle.stem)
        if token and len(video_by_token.get(token, [])) == 1:
            target_video = video_by_token[token][0]
        else:
            sub_norm = _normalize_media_stem(subtitle.stem)
            candidates = [video for video, norm in video_norm.items() if norm and (norm in sub_norm or sub_norm in norm)]
            if len(candidates) == 1:
                target_video = candidates[0]
        if target_video:
            matched[subtitle] = target_video
        else:
            remaining_subs.append(subtitle)

    if len(remaining_subs) and len(videos) == len(subtitles):
        unmatched_videos = [video for video in videos if video not in matched.values()]
        for subtitle, video in zip(sorted(remaining_subs, key=lambda p: _natural_key(p.name)), sorted(unmatched_videos, key=lambda p: _natural_key(p.name))):
            matched[subtitle] = video
    elif len(videos) == 1:
        for subtitle in remaining_subs:
            matched[subtitle] = videos[0]

    return videos, subtitles, matched


def _run_subtitle_match_job(job_id, config):
    folder = config["folder"]
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="subtitle-match")
    _push_output(job_id, "  Folder: {}".format(folder))
    videos, subtitles, matched = _match_subtitle_targets(folder)
    if not videos:
        _push_output(job_id, "ERROR: No video files found")
        _set_job_fields(job_id, status="failed", finished=time.strftime("%H:%M:%S"), pid=None)
        return
    if not subtitles:
        _push_output(job_id, "ERROR: No subtitle files found")
        _set_job_fields(job_id, status="failed", finished=time.strftime("%H:%M:%S"), pid=None)
        return

    renamed, unchanged, failed = [], [], []
    total = len(subtitles)
    for idx, subtitle in enumerate(subtitles, start=1):
        _set_job_fields(job_id, current_idx=idx, current_url=str(subtitle), progress={"pct": round((idx - 1) * 100 / max(total, 1), 1)})
        target_video = matched.get(subtitle)
        if not target_video:
            failed.append(subtitle.name)
            _push_output(job_id, "[{}/{}] {} -> no matching video".format(idx, total, subtitle.name))
            continue
        suffix = _subtitle_suffix(subtitle.stem)
        new_name = "{}{}{}".format(target_video.stem, ".{}".format(suffix) if suffix else "", subtitle.suffix.lower())
        target_path = subtitle.with_name(new_name)
        _push_output(job_id, "[{}/{}] {} -> {}".format(idx, total, subtitle.name, target_path.name))
        if subtitle == target_path:
            unchanged.append(subtitle.name)
            _push_output(job_id, "  Unchanged")
            continue
        if target_path.exists():
            failed.append(subtitle.name)
            _push_output(job_id, "  Failed: target already exists")
            continue
        try:
            subtitle.rename(target_path)
            renamed.append(target_path.name)
            _push_output(job_id, "  Renamed")
        except Exception as exc:
            failed.append(subtitle.name)
            _push_output(job_id, "  Failed: {}".format(exc))

    final_status = "done" if not failed else ("partial" if len(failed) < total else "failed")
    _set_job_fields(job_id, progress={"pct": 100.0})
    _push_output(job_id, "-" * 50)
    if renamed:
        _push_output(job_id, "  Renamed: {}".format(", ".join(renamed)))
    if unchanged:
        _push_output(job_id, "  Unchanged: {}".format(", ".join(unchanged)))
    if failed:
        _push_output(job_id, "  Failed: {}".format(", ".join(failed)))
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- {}".format(job_id, final_status.upper()))
    _set_job_fields(job_id, status=final_status, finished=time.strftime("%H:%M:%S"), pid=None)


def _run_media_rename_job(job_id, config):
    folder = config["folder"]
    include_dirs = config["include_dirs"]
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="media-rename")
    _push_output(job_id, "  Folder: {}".format(folder))
    candidates = []
    for entry in sorted(Path(folder).iterdir(), key=lambda p: _natural_key(p.name)):
        if entry.is_dir() and include_dirs:
            candidates.append(entry)
        elif entry.is_file() and (entry.suffix.lower() in VIDEO_EXTS | SUBTITLE_EXTS | MEDIA_SIDECAR_EXTS):
            candidates.append(entry)
    if not candidates:
        _push_output(job_id, "ERROR: No matching media files or folders found")
        _set_job_fields(job_id, status="failed", finished=time.strftime("%H:%M:%S"), pid=None)
        return

    renamed, unchanged, failed = [], [], []
    total = len(candidates)
    for idx, entry in enumerate(candidates, start=1):
        _set_job_fields(job_id, current_idx=idx, current_url=str(entry), progress={"pct": round((idx - 1) * 100 / max(total, 1), 1)})
        new_name = _media_clean_name(entry.name)
        target = _safe_rename_target(entry.parent, new_name)
        _push_output(job_id, "[{}/{}] {}".format(idx, total, entry.name))
        if entry == target:
            unchanged.append(entry.name)
            _push_output(job_id, "  Unchanged")
            continue
        if target.exists():
            failed.append(entry.name)
            _push_output(job_id, "  Failed: target already exists")
            continue
        try:
            entry.rename(target)
            renamed.append(target.name)
            _push_output(job_id, "  Renamed -> {}".format(target.name))
        except Exception as exc:
            failed.append(entry.name)
            _push_output(job_id, "  Failed: {}".format(exc))

    final_status = "done" if not failed else ("partial" if len(failed) < total else "failed")
    _set_job_fields(job_id, progress={"pct": 100.0})
    _push_output(job_id, "-" * 50)
    if renamed:
        _push_output(job_id, "  Renamed: {}".format(", ".join(renamed)))
    if unchanged:
        _push_output(job_id, "  Unchanged: {}".format(", ".join(unchanged)))
    if failed:
        _push_output(job_id, "  Failed: {}".format(", ".join(failed)))
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- {}".format(job_id, final_status.upper()))
    _set_job_fields(job_id, status=final_status, finished=time.strftime("%H:%M:%S"), pid=None)


def _run_media_organize_job(job_id, config):
    source = Path(config["source"])
    destination = Path(config["destination"])
    mode = config["mode"]
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="media-organize")
    _push_output(job_id, "  Source: {}".format(source))
    _push_output(job_id, "  Destination: {}".format(destination))
    _push_output(job_id, "  Mode: {}".format(mode))
    destination.mkdir(parents=True, exist_ok=True)

    candidates = sorted(
        [entry for entry in source.iterdir() if entry.is_file() and (entry.suffix.lower() in VIDEO_EXTS | MEDIA_SIDECAR_EXTS)],
        key=lambda p: _natural_key(p.name),
    )
    if not candidates:
        _push_output(job_id, "ERROR: No media files found in source folder")
        _set_job_fields(job_id, status="failed", finished=time.strftime("%H:%M:%S"), pid=None)
        return

    videos = [entry for entry in candidates if entry.suffix.lower() in VIDEO_EXTS]
    token_map = {}
    norm_map = {}
    for video in videos:
        token = _episode_token(video.stem)
        if token:
            token_map.setdefault(token, []).append(video)
        norm_map[video] = _normalize_media_stem(video.stem)

    moved, unchanged, failed = [], [], []
    total = len(candidates)
    for idx, entry in enumerate(candidates, start=1):
        _set_job_fields(job_id, current_idx=idx, current_url=str(entry), progress={"pct": round((idx - 1) * 100 / max(total, 1), 1)})
        if mode == "flat":
            target_dir = destination
        else:
            target_video = entry if entry.suffix.lower() in VIDEO_EXTS else None
            if not target_video:
                token = _episode_token(entry.stem)
                if token and len(token_map.get(token, [])) == 1:
                    target_video = token_map[token][0]
                elif len(videos) == 1:
                    target_video = videos[0]
                else:
                    entry_norm = _normalize_media_stem(entry.stem)
                    matches = [video for video, norm in norm_map.items() if norm and (norm in entry_norm or entry_norm in norm)]
                    if len(matches) == 1:
                        target_video = matches[0]
            if not target_video:
                failed.append(entry.name)
                _push_output(job_id, "[{}/{}] {} -> no matching video folder".format(idx, total, entry.name))
                continue
            target_dir = destination / target_video.stem
            target_dir.mkdir(parents=True, exist_ok=True)

        target_path = target_dir / entry.name
        _push_output(job_id, "[{}/{}] {} -> {}".format(idx, total, entry.name, target_path))
        if entry.parent == target_dir:
            unchanged.append(entry.name)
            _push_output(job_id, "  Already organized")
            continue
        if target_path.exists():
            failed.append(entry.name)
            _push_output(job_id, "  Failed: target already exists")
            continue
        try:
            shutil.move(str(entry), str(target_path))
            moved.append(entry.name)
            _push_output(job_id, "  Moved")
        except Exception as exc:
            failed.append(entry.name)
            _push_output(job_id, "  Failed: {}".format(exc))

    final_status = "done" if not failed else ("partial" if len(failed) < total else "failed")
    _set_job_fields(job_id, progress={"pct": 100.0})
    _push_output(job_id, "-" * 50)
    if moved:
        _push_output(job_id, "  Moved: {}".format(", ".join(moved)))
    if unchanged:
        _push_output(job_id, "  Unchanged: {}".format(", ".join(unchanged)))
    if failed:
        _push_output(job_id, "  Failed: {}".format(", ".join(failed)))
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- {}".format(job_id, final_status.upper()))
    _set_job_fields(job_id, status=final_status, finished=time.strftime("%H:%M:%S"), pid=None)


def _run_jellyfin_rescan_job(job_id, config):
    url = (config.get("url") or JELLYFIN_URL).rstrip("/")
    api_key = str(config.get("api_key") or "").strip()
    refresh_url = "{}/Library/Refresh".format(url)
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="jellyfin-rescan")
    _push_output(job_id, "  Jellyfin URL: {}".format(url))
    if api_key:
        _push_output(job_id, "  Using API token from job/environment")
        cmd = "curl -fsS -X POST --max-time 20 -H {} {}".format(
            shlex.quote("X-Emby-Token: {}".format(api_key)),
            shlex.quote(refresh_url),
        )
    else:
        _push_output(job_id, "  No API token provided, trying unauthenticated refresh")
        cmd = "curl -fsS -X POST --max-time 20 {}".format(shlex.quote(refresh_url))
    out, err, rc = run(cmd, timeout=30)
    if rc == 0:
        _push_output(job_id, "  Refresh triggered successfully")
        if out:
            _push_output(job_id, "  Response: {}".format(out[:300]))
        final_status = "done"
    else:
        _push_output(job_id, "  Failed: {}".format(err or out or "request failed"))
        if not api_key:
            _push_output(job_id, "  Hint: set SEEDBOX_JELLYFIN_API_KEY or provide a token in the job form if Jellyfin requires auth.")
        final_status = "failed"
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- {}".format(job_id, final_status.upper()))
    _set_job_fields(job_id, status=final_status, finished=time.strftime("%H:%M:%S"), pid=None, progress={"pct": 100.0})


def _run_bulk_rename_job(job_id, config):
    folder = config["folder"]
    recursive = config["recursive"]
    find_text = config["find_text"]
    replace_text = config["replace_text"]
    include_dirs = config["include_dirs"]
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="bulk-rename")
    _push_output(job_id, "  Folder: {}".format(folder))
    _push_output(job_id, "  Replace: '{}' -> '{}'".format(find_text, replace_text))
    entries = []
    base = Path(folder)
    walker = base.rglob("*") if recursive else base.iterdir()
    for entry in walker:
        if entry.is_file() or (include_dirs and entry.is_dir()):
            if find_text.lower() in entry.name.lower():
                entries.append(entry)
    entries.sort(key=lambda p: len(str(p)), reverse=True)
    if not entries:
        _push_output(job_id, "ERROR: No matching items found")
        _set_job_fields(job_id, status="failed", finished=time.strftime("%H:%M:%S"), pid=None)
        return
    renamed, unchanged, failed = [], [], []
    total = len(entries)
    for idx, entry in enumerate(entries, start=1):
        _set_job_fields(job_id, current_idx=idx, current_url=str(entry), progress={"pct": round((idx - 1) * 100 / max(total, 1), 1)})
        pattern = re.compile(re.escape(find_text), re.IGNORECASE)
        new_name = pattern.sub(replace_text, entry.name)
        target = entry.with_name(new_name)
        _push_output(job_id, "[{}/{}] {}".format(idx, total, entry.name))
        if new_name == entry.name:
            unchanged.append(entry.name)
            _push_output(job_id, "  Unchanged")
            continue
        if target.exists():
            failed.append(entry.name)
            _push_output(job_id, "  Failed: target already exists")
            continue
        try:
            entry.rename(target)
            renamed.append("{} -> {}".format(entry.name, target.name))
            _push_output(job_id, "  Renamed -> {}".format(target.name))
        except Exception as exc:
            failed.append(entry.name)
            _push_output(job_id, "  Failed: {}".format(exc))
    final_status = "done" if not failed else ("partial" if len(failed) < total else "failed")
    _set_job_fields(job_id, progress={"pct": 100.0})
    _push_output(job_id, "-" * 50)
    if renamed:
        _push_output(job_id, "  Renamed: {}".format(", ".join(renamed[:10]) + (" ..." if len(renamed) > 10 else "")))
    if unchanged:
        _push_output(job_id, "  Unchanged: {}".format(len(unchanged)))
    if failed:
        _push_output(job_id, "  Failed: {}".format(", ".join(failed[:10]) + (" ..." if len(failed) > 10 else "")))
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- {}".format(job_id, final_status.upper()))
    _set_job_fields(job_id, status=final_status, finished=time.strftime("%H:%M:%S"), pid=None)


def _run_empty_folder_cleanup_job(job_id, config):
    folder = config["folder"]
    recursive = config["recursive"]
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="empty-folder-cleanup")
    _push_output(job_id, "  Folder: {}".format(folder))
    dirs = [p for p in Path(folder).rglob("*") if p.is_dir()] if recursive else [p for p in Path(folder).iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: len(str(p)), reverse=True)
    removed, failed = [], []
    total = len(dirs) or 1
    for idx, directory in enumerate(dirs, start=1):
        _set_job_fields(job_id, current_idx=idx, current_url=str(directory), progress={"pct": round((idx - 1) * 100 / max(total, 1), 1)})
        try:
            if any(directory.iterdir()):
                continue
            directory.rmdir()
            removed.append(str(directory))
            _push_output(job_id, "[{}/{}] Removed {}".format(idx, total, directory))
        except Exception as exc:
            failed.append(str(directory))
            _push_output(job_id, "[{}/{}] Failed {}: {}".format(idx, total, directory, exc))
    final_status = "done" if not failed else ("partial" if removed else "failed")
    _set_job_fields(job_id, progress={"pct": 100.0})
    _push_output(job_id, "-" * 50)
    _push_output(job_id, "  Removed empty folders: {}".format(len(removed)))
    if failed:
        _push_output(job_id, "  Failed folders: {}".format(len(failed)))
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- {}".format(job_id, final_status.upper()))
    _set_job_fields(job_id, status=final_status, finished=time.strftime("%H:%M:%S"), pid=None)


def _run_duplicate_scan_job(job_id, config):
    folder = config["folder"]
    recursive = config["recursive"]
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="duplicate-scan")
    _push_output(job_id, "  Folder: {}".format(folder))
    files = list(_iter_files(folder, recursive=recursive))
    if not files:
        _push_output(job_id, "ERROR: No files found")
        _set_job_fields(job_id, status="failed", finished=time.strftime("%H:%M:%S"), pid=None)
        return
    by_size = {}
    for path in files:
        by_size.setdefault(path.stat().st_size, []).append(path)
    candidates = [group for size, group in by_size.items() if len(group) > 1 and size > 0]
    duplicates = []
    total = sum(len(group) for group in candidates) or 1
    seen = 0
    for group in candidates:
        by_hash = {}
        for path in group:
            seen += 1
            _set_job_fields(job_id, current_idx=seen, current_url=str(path), progress={"pct": round((seen - 1) * 100 / max(total, 1), 1)})
            try:
                digest = _file_hash(path)
                by_hash.setdefault(digest, []).append(path)
            except Exception as exc:
                _push_output(job_id, "  Failed hashing {}: {}".format(path.name, exc))
        for same_hash in by_hash.values():
            if len(same_hash) > 1:
                duplicates.append(same_hash)
    if duplicates:
        for idx, group in enumerate(duplicates[:20], start=1):
            _push_output(job_id, "Duplicate group {}:".format(idx))
            for path in group:
                _push_output(job_id, "  {} ({})".format(path, human_size(path.stat().st_size)))
    else:
        _push_output(job_id, "  No duplicate files detected")
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- DONE".format(job_id))
    _set_job_fields(job_id, status="done", finished=time.strftime("%H:%M:%S"), pid=None, progress={"pct": 100.0})


def _run_largest_files_job(job_id, config):
    folder = config["folder"]
    recursive = config["recursive"]
    limit = config["limit"]
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="largest-files")
    _push_output(job_id, "  Folder: {}".format(folder))
    files = list(_iter_files(folder, recursive=recursive))
    largest = sorted(files, key=lambda p: p.stat().st_size, reverse=True)[:limit]
    if not largest:
        _push_output(job_id, "ERROR: No files found")
        _set_job_fields(job_id, status="failed", finished=time.strftime("%H:%M:%S"), pid=None)
        return
    for idx, path in enumerate(largest, start=1):
        _push_output(job_id, "{:02d}. {} ({})".format(idx, path, human_size(path.stat().st_size)))
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- DONE".format(job_id))
    _set_job_fields(job_id, status="done", finished=time.strftime("%H:%M:%S"), pid=None, progress={"pct": 100.0})


def _run_folder_size_job(job_id, config):
    folder = config["folder"]
    recursive = config["recursive"]
    limit = config["limit"]
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="folder-size")
    _push_output(job_id, "  Folder: {}".format(folder))
    base = Path(folder)
    targets = [p for p in base.iterdir() if p.is_dir()]
    sizes = []
    total = len(targets) or 1
    for idx, directory in enumerate(targets, start=1):
        _set_job_fields(job_id, current_idx=idx, current_url=str(directory), progress={"pct": round((idx - 1) * 100 / max(total, 1), 1)})
        size = sum(path.stat().st_size for path in _iter_files(directory, recursive=True if recursive else False))
        sizes.append((directory, size))
    sizes.sort(key=lambda item: item[1], reverse=True)
    if not sizes:
        _push_output(job_id, "ERROR: No subfolders found")
        _set_job_fields(job_id, status="failed", finished=time.strftime("%H:%M:%S"), pid=None)
        return
    for idx, (directory, size) in enumerate(sizes[:limit], start=1):
        _push_output(job_id, "{:02d}. {} ({})".format(idx, directory, human_size(size)))
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- DONE".format(job_id))
    _set_job_fields(job_id, status="done", finished=time.strftime("%H:%M:%S"), pid=None, progress={"pct": 100.0})


def _run_archive_extract_job(job_id, config):
    mode = config["mode"]
    source = Path(config["source"])
    destination = Path(config["destination"])
    archive_format = config["format"]
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="archive-extract")
    _push_output(job_id, "  Mode: {}".format(mode))
    _push_output(job_id, "  Source: {}".format(source))
    _push_output(job_id, "  Destination: {}".format(destination))
    try:
        destination.mkdir(parents=True, exist_ok=True)
        if mode == "archive":
            base_name = str(destination / "{}-{}".format(source.name, _safe_job_time_label()))
            archive_path = shutil.make_archive(base_name, ARCHIVE_FORMATS[archive_format], root_dir=str(source.parent), base_dir=source.name)
            _push_output(job_id, "  Created archive: {}".format(archive_path))
        else:
            shutil.unpack_archive(str(source), str(destination))
            _push_output(job_id, "  Extracted archive into {}".format(destination))
        final_status = "done"
    except Exception as exc:
        _push_output(job_id, "  Failed: {}".format(exc))
        final_status = "failed"
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- {}".format(job_id, final_status.upper()))
    _set_job_fields(job_id, status=final_status, finished=time.strftime("%H:%M:%S"), pid=None, progress={"pct": 100.0})


def _run_backup_folder_job(job_id, config):
    source = Path(config["source"])
    destination = Path(config["destination"])
    archive_format = config["format"]
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="backup-folder")
    _push_output(job_id, "  Source: {}".format(source))
    _push_output(job_id, "  Destination: {}".format(destination))
    try:
        destination.mkdir(parents=True, exist_ok=True)
        base_name = str(destination / "{}-backup-{}".format(source.name, _safe_job_time_label()))
        archive_path = shutil.make_archive(base_name, ARCHIVE_FORMATS[archive_format], root_dir=str(source.parent), base_dir=source.name)
        _push_output(job_id, "  Backup archive created: {}".format(archive_path))
        final_status = "done"
    except Exception as exc:
        _push_output(job_id, "  Failed: {}".format(exc))
        final_status = "failed"
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- {}".format(job_id, final_status.upper()))
    _set_job_fields(job_id, status=final_status, finished=time.strftime("%H:%M:%S"), pid=None, progress={"pct": 100.0})


def _run_orphan_subtitle_job(job_id, config):
    folder = config["folder"]
    recursive = config["recursive"]
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="orphan-subtitles")
    _push_output(job_id, "  Folder: {}".format(folder))
    files = list(_iter_files(folder, recursive=recursive))
    videos = [path for path in files if path.suffix.lower() in VIDEO_EXTS]
    subtitles = [path for path in files if path.suffix.lower() in SUBTITLE_EXTS]
    video_keys = {}
    subtitle_keys = {}
    for path in videos:
        video_keys.setdefault((str(path.parent), _video_key(path)), []).append(path)
    for path in subtitles:
        subtitle_keys.setdefault((str(path.parent), _video_key(path)), []).append(path)
    subs_without_video = []
    videos_without_sub = []
    for key, paths in subtitle_keys.items():
        if key not in video_keys:
            subs_without_video.extend(paths)
    for key, paths in video_keys.items():
        if key not in subtitle_keys:
            videos_without_sub.extend(paths)
    if subs_without_video:
        _push_output(job_id, "Subtitles without matching video:")
        for path in subs_without_video[:50]:
            _push_output(job_id, "  {}".format(path))
    if videos_without_sub:
        _push_output(job_id, "Videos without matching subtitles:")
        for path in videos_without_sub[:50]:
            _push_output(job_id, "  {}".format(path))
    if not subs_without_video and not videos_without_sub:
        _push_output(job_id, "  No orphan subtitle/video files found")
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- DONE".format(job_id))
    _set_job_fields(job_id, status="done", finished=time.strftime("%H:%M:%S"), pid=None, progress={"pct": 100.0})


def _run_temp_cleanup_job(job_id, config):
    folder = config["folder"]
    recursive = config["recursive"]
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="temp-cleanup")
    _push_output(job_id, "  Folder: {}".format(folder))
    files = list(_iter_files(folder, recursive=recursive))
    base = Path(folder)
    pycache_dirs = [entry for entry in (base.rglob("__pycache__") if recursive else base.iterdir()) if entry.is_dir() and entry.name == "__pycache__"]
    candidates = [path for path in files if _is_temp_candidate(path)] + pycache_dirs
    if not candidates:
        _push_output(job_id, "  Nothing to clean")
        _push_output(job_id, "=" * 50)
        _push_output(job_id, "  Job {} -- DONE".format(job_id))
        _set_job_fields(job_id, status="done", finished=time.strftime("%H:%M:%S"), pid=None, progress={"pct": 100.0})
        return

    removed, skipped, failed = [], [], []
    total = len(candidates)
    for idx, path in enumerate(candidates, start=1):
        _set_job_fields(job_id, current_idx=idx, current_url=str(path), progress={"pct": round((idx - 1) * 100 / max(total, 1), 1)})
        _push_output(job_id, "[{}/{}] {}".format(idx, total, path))
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(str(path))
            _push_output(job_id, "  Removed")
        except FileNotFoundError:
            skipped.append(str(path))
            _push_output(job_id, "  Skipped: already removed")
        except Exception as exc:
            failed.append(str(path))
            _push_output(job_id, "  Failed: {}".format(exc))
    final_status = "done" if not failed else ("partial" if removed else "failed")
    _push_output(job_id, "-" * 50)
    _push_output(job_id, "  Removed: {}".format(len(removed)))
    _push_output(job_id, "  Skipped: {}".format(len(skipped)))
    _push_output(job_id, "  Failed: {}".format(len(failed)))
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- {}".format(job_id, final_status.upper()))
    _set_job_fields(job_id, status=final_status, finished=time.strftime("%H:%M:%S"), pid=None, progress={"pct": 100.0})


def _run_log_cleanup_job(job_id, config):
    folder = config["folder"]
    recursive = config["recursive"]
    older_than_days = config["older_than_days"]
    cutoff = time.time() - max(0, older_than_days) * 86400
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="log-cleanup")
    _push_output(job_id, "  Folder: {}".format(folder))
    _push_output(job_id, "  Older than: {} day(s)".format(older_than_days))
    files = [path for path in _iter_files(folder, recursive=recursive) if _is_log_candidate(path)]
    if not files:
        _push_output(job_id, "  No matching log files found")
        _push_output(job_id, "=" * 50)
        _push_output(job_id, "  Job {} -- DONE".format(job_id))
        _set_job_fields(job_id, status="done", finished=time.strftime("%H:%M:%S"), pid=None, progress={"pct": 100.0})
        return

    removed, kept, failed = [], [], []
    total = len(files)
    for idx, path in enumerate(files, start=1):
        _set_job_fields(job_id, current_idx=idx, current_url=str(path), progress={"pct": round((idx - 1) * 100 / max(total, 1), 1)})
        age_days = max(0.0, (time.time() - path.stat().st_mtime) / 86400)
        _push_output(job_id, "[{}/{}] {} ({:.1f} days old)".format(idx, total, path.name, age_days))
        if older_than_days and path.stat().st_mtime > cutoff:
            kept.append(str(path))
            _push_output(job_id, "  Kept: newer than threshold")
            continue
        try:
            path.unlink()
            removed.append(str(path))
            _push_output(job_id, "  Removed")
        except Exception as exc:
            failed.append(str(path))
            _push_output(job_id, "  Failed: {}".format(exc))
    final_status = "done" if not failed else ("partial" if removed else "failed")
    _push_output(job_id, "-" * 50)
    _push_output(job_id, "  Removed: {}".format(len(removed)))
    _push_output(job_id, "  Kept: {}".format(len(kept)))
    _push_output(job_id, "  Failed: {}".format(len(failed)))
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- {}".format(job_id, final_status.upper()))
    _set_job_fields(job_id, status=final_status, finished=time.strftime("%H:%M:%S"), pid=None, progress={"pct": 100.0})


def _run_system_report_job(job_id, config):
    destination = Path(config["destination"])
    period = config["period"]
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="system-report")
    try:
        destination.mkdir(parents=True, exist_ok=True)
        report_path = destination / _report_filename("system-report", period)
        snapshot = build_status_snapshot()
        processes = get_processes()
        df_out = run("df -h", timeout=15)[0]
        lsblk_out = run("lsblk -f", timeout=15)[0]
        mounts_out = run("findmnt", timeout=15)[0]
        docker_out = run("docker ps --format '{{.Names}}\\t{{.Status}}'", timeout=15)[0]
        recent_jobs = sorted(list(_jobs.values()), key=lambda job: job.get("updated_at") or "", reverse=True)[:10]
        lines = [
            "# Seedbox System Report",
            "",
            "- Generated: {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "- Period label: {}".format(period),
            "- Hostname: {}".format(snapshot.get("hostname") or "?"),
            "- Kernel: {}".format(snapshot.get("kernel") or "?"),
            "- Health: {}/100 ({})".format(snapshot.get("health_score", "--"), snapshot.get("health_status", "unknown")),
            "- Uptime: {}".format(snapshot.get("uptime") or "?"),
            "- CPU: {}%".format(snapshot.get("cpu", "--")),
            "- RAM: {}%".format((snapshot.get("ram") or {}).get("pct", "--")),
            "- Disk used: {}".format((snapshot.get("disk") or {}).get("pct", "--")),
            "- VPN route: {}".format("protected" if snapshot.get("gluetun") else "down"),
            "- Local IP: {}".format(snapshot.get("local_ip") or "?"),
            "- VPN IP: {}".format(snapshot.get("vpn_ip") or "?"),
            "",
            "## Top Processes",
            "",
        ]
        for proc in processes[:12]:
            lines.append("- PID {} | {} | MEM {} | CPU {} | {}".format(proc.get("pid"), proc.get("name"), proc.get("mem"), proc.get("cpu"), proc.get("time")))
        lines.extend(["", "## Recent Jobs", ""])
        for job in recent_jobs:
            lines.append("- {} | {} | {} | {}".format(job.get("id"), job.get("label") or job.get("kind"), job.get("status"), job.get("updated_at") or ""))
        lines.extend([
            "", "## qBittorrent Stats", "", "```json", json.dumps(snapshot.get("qbit_stats") or {}, indent=2), "```",
            "", "## df -h", "", "```", df_out.strip(), "```",
            "", "## lsblk -f", "", "```", lsblk_out.strip(), "```",
            "", "## findmnt", "", "```", mounts_out.strip(), "```",
            "", "## docker ps", "", "```", docker_out.strip() or "docker unavailable", "```", "",
        ])
        report_path.write_text("\n".join(lines))
        _push_output(job_id, "  Wrote {}".format(report_path))
        _set_job_fields(
            job_id,
            verified_files=[
                {
                    "name": report_path.name,
                    "size": human_size(report_path.stat().st_size),
                    "path": str(report_path),
                }
            ],
        )
        final_status = "done"
    except Exception as exc:
        _push_output(job_id, "  Failed: {}".format(exc))
        final_status = "failed"
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- {}".format(job_id, final_status.upper()))
    _set_job_fields(job_id, status=final_status, finished=time.strftime("%H:%M:%S"), pid=None, progress={"pct": 100.0})


def _run_disk_snapshot_job(job_id, config):
    destination = Path(config["destination"])
    period = config["period"]
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command="disk-snapshot")
    try:
        destination.mkdir(parents=True, exist_ok=True)
        report_path = destination / _report_filename("disk-snapshot", period)
        df_out = run("df -h", timeout=15)[0]
        lsblk_out = run("lsblk -o NAME,FSTYPE,SIZE,FSAVAIL,FSUSE%,MOUNTPOINTS", timeout=15)[0]
        mount_out = run("findmnt -lo TARGET,SOURCE,FSTYPE,OPTIONS", timeout=15)[0]
        top_level = []
        root = Path(MOUNT_POINT_BASE)
        if root.exists():
            for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_dir():
                    out, _, _ = run("du -sh {}".format(shlex.quote(str(entry))), timeout=20)
                    top_level.append(out.strip() or entry.name)
        lines = [
            "# Seedbox Disk Snapshot",
            "",
            "- Generated: {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "- Period label: {}".format(period),
            "- Root: {}".format(MOUNT_POINT_BASE),
            "",
            "## Top-level Folder Sizes",
            "",
        ]
        lines.extend(["- {}".format(item) for item in top_level] or ["- No top-level folders found"])
        lines.extend([
            "", "## df -h", "", "```", df_out.strip(), "```",
            "", "## lsblk", "", "```", lsblk_out.strip(), "```",
            "", "## findmnt", "", "```", mount_out.strip(), "```", "",
        ])
        report_path.write_text("\n".join(lines))
        _push_output(job_id, "  Wrote {}".format(report_path))
        _set_job_fields(
            job_id,
            verified_files=[
                {
                    "name": report_path.name,
                    "size": human_size(report_path.stat().st_size),
                    "path": str(report_path),
                }
            ],
        )
        final_status = "done"
    except Exception as exc:
        _push_output(job_id, "  Failed: {}".format(exc))
        final_status = "failed"
    _push_output(job_id, "=" * 50)
    _push_output(job_id, "  Job {} -- {}".format(job_id, final_status.upper()))
    _set_job_fields(job_id, status=final_status, finished=time.strftime("%H:%M:%S"), pid=None, progress={"pct": 100.0})


def _run_download_job(job_id, urls, dest, method):
    _set_job_fields(job_id, status="running", started=time.strftime("%H:%M:%S"), command=method)
    try:
        os.makedirs(dest, exist_ok=True)
    except Exception as exc:
        _push_output(job_id, "ERROR: Cannot create destination: {}".format(exc))
        _set_job_fields(job_id, status="failed", finished=time.strftime("%H:%M:%S"))
        return

    free_gb = _disk_free_gb(dest)
    if free_gb < 1.0:
        _push_output(job_id, "  WARNING: Low disk: {:.1f} GB free".format(free_gb))
    else:
        _push_output(job_id, "  Disk free: {:.1f} GB -> {}".format(free_gb, dest))

    use_script = os.path.isfile(DOWNLOAD_SCRIPT) and not _should_bypass_download_script(urls, method)
    if os.path.isfile(DOWNLOAD_SCRIPT) and not use_script:
        _push_output(job_id, "  INFO: Using inline downloader for URL-specific compatibility")
    if use_script:
        cmd_parts = ["bash", DOWNLOAD_SCRIPT, "-d", dest, "--sequential"]
        for url in urls:
            cmd_parts += ["-u", url]
        env = {**os.environ, "NON_INTERACTIVE": "1", "DL_METHOD": method, "TERM": "xterm"}
        _push_output(job_id, "  Calling download.sh ({} URL(s))".format(len(urls)))
        _push_output(job_id, "-" * 50)
        try:
            proc = subprocess.Popen(
                cmd_parts,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                stdin=subprocess.DEVNULL,
            )
            _set_job_fields(job_id, pid=proc.pid, current_url=urls[0] if urls else "")
            assert proc.stdout is not None
            for raw in proc.stdout:
                clean = strip_ansi(raw.rstrip())
                if not clean:
                    continue
                progress_line, progress_data = _parse_progress(clean)
                if progress_line:
                    _push_output(job_id, progress_line, is_progress=True)
                    _set_job_fields(job_id, progress=progress_data)
                else:
                    _push_output(job_id, clean)
            proc.wait()
            rc = proc.returncode
            _set_job_fields(job_id, pid=None)
        except Exception as exc:
            _push_output(job_id, "ERROR: {}".format(exc))
            rc = 1
        final_status = "done" if rc == 0 else "failed"
        _push_output(job_id, "=" * 50)
        _push_output(job_id, "  Job {} -- {}".format(job_id, "SUCCESS" if rc == 0 else "FAILED (rc={})".format(rc)))
    else:
        _push_output(job_id, "  WARNING: download.sh not found - inline fallback")
        _push_output(job_id, "-" * 50)
        failed_urls = []
        for idx, url in enumerate(urls):
            if not url.strip():
                continue
            _set_job_fields(job_id, current_url=url, current_idx=idx + 1, progress={})
            marker_time = time.time()
            _push_output(job_id, "\n  [{}/{}] {}{}".format(idx + 1, len(urls), url[:80], "..." if len(url) > 80 else ""))
            if url.startswith("magnet:"):
                out, _, rc = run(
                    "curl -s 'http://localhost:{}/api/v2/torrents/add' -F 'urls={}' -F 'savepath={}'".format(
                        WEBUI_PORT, url, _qbit_save_path(dest)
                    ),
                    timeout=10,
                )
                if rc == 0 and "Ok" in out:
                    _push_output(job_id, "  OK: Added to qBittorrent")
                else:
                    _push_output(job_id, "  FAIL: {}".format(out))
                    failed_urls.append(url)
                continue
            commands = _build_cmds(url, dest, method)
            _push_output(job_id, "  Methods: {}".format(", ".join(tool for tool, _ in commands)))
            success = False
            for tool, command in commands:
                if success:
                    break
                if _run_cmd(job_id, tool, command) == 0:
                    success = True
                    _push_output(job_id, "  OK: {}".format(tool))
            if success:
                _verify_downloads(job_id, dest, marker_time)
            else:
                failed_urls.append(url)
                _push_output(job_id, "  FAIL: all methods failed")
        final_status = "done" if not failed_urls else ("partial" if len(failed_urls) < len(urls) else "failed")
        _set_job_fields(job_id, failed_urls=failed_urls)
        _push_output(job_id, "=" * 50)
        _push_output(job_id, "  Job {} -- {}".format(job_id, final_status.upper()))

    _set_job_fields(job_id, status=final_status, finished=time.strftime("%H:%M:%S"), pid=None, progress={})


def download_dirs():
    dirs = list(PRESET_DIRS)
    try:
        for entry in sorted(Path(MOUNT_POINT_BASE).iterdir()):
            if entry.is_dir():
                path = str(entry)
                if not any(item["path"] == path for item in dirs):
                    dirs.append({"label": entry.name, "path": path})
    except Exception:
        pass
    return dirs


def start_download(data):
    urls_raw = data.get("urls", "")
    dest = data.get("dest", MOUNT_POINT_BASE)
    method = data.get("method", "auto")
    custom_dest = data.get("custom_dest", "").strip()
    if custom_dest:
        dest = custom_dest
    try:
        dest = safe_path(dest)
    except ValueError as exc:
        return legacy_error_payload("Invalid path: {}".format(exc)), 400
    if isinstance(urls_raw, list):
        urls = [url.strip() for url in urls_raw if url.strip()]
    else:
        urls = [url.strip() for url in str(urls_raw).splitlines() if url.strip()]
    urls = list(dict.fromkeys(urls))
    if not urls:
        return legacy_error_payload("No URLs provided"), 400
    if method not in ("auto", "direct", "ytdlp", "aria2c", "wget", "curl"):
        method = "auto"

    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "kind": "download",
            "label": "Download queue",
            "rerun_data": {
                "type": "download",
                "urls": urls,
                "dest": dest,
                "method": method,
            },
            "status": "queued",
            "urls": urls,
            "total": len(urls),
            "dest": dest,
            "method": method,
            "output": ["Job {} -- {} URL(s) -> {}".format(job_id, len(urls), dest)],
            "pid": None,
            "current_idx": 0,
            "current_url": "",
            "current_cmd": "",
            "failed_urls": [],
            "progress": {},
            "verified_files": [],
            "started": None,
            "finished": None,
            "created_at": created_at,
            "updated_at": created_at,
            "command": method,
        }
    _persist_job(job_id)
    threading.Thread(target=_run_download_job, args=(job_id, urls, dest, method), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Download started -- job {}".format(job_id)}, 200


def start_viki_subtitles(data):
    defaults = get_viki_defaults()
    first_id = str(data.get("first_id", "")).strip()
    base_name = str(data.get("base_name", "")).strip()
    token = str(data.get("token", defaults["token"])).strip()
    if not first_id or not base_name:
        return legacy_error_payload("first_id and base_name are required"), 400
    if "##" not in base_name:
        return legacy_error_payload("base_name must contain ## as the episode placeholder"), 400
    try:
        ep_count = int(data.get("ep_count", 0))
    except Exception:
        ep_count = 0
    if ep_count <= 0:
        return legacy_error_payload("ep_count must be greater than 0"), 400
    dest = str(data.get("dest") or MOUNT_POINT_BASE).strip()
    try:
        dest = safe_path(dest)
    except ValueError as exc:
        return legacy_error_payload("Invalid destination: {}".format(exc)), 400

    try:
        start_num, ext = _parse_first_video_id(first_id, data.get("ext"))
    except ValueError as exc:
        return legacy_error_payload(str(exc)), 400

    special_raw = str(data.get("special_eps", "")).strip()
    special_eps = []
    if special_raw:
        for part in re.split(r"[\s,]+", special_raw):
            if not part:
                continue
            try:
                special_eps.append(int(part))
            except Exception:
                return legacy_error_payload("special_eps must contain only episode numbers"), 400

    config = {
        "base_url": str(data.get("base_url", defaults["base_url"])).strip() or defaults["base_url"],
        "app_param": str(data.get("app_param", defaults["app_param"])).strip() or defaults["app_param"],
        "stream_id": str(data.get("stream_id", defaults["stream_id"])).strip() or defaults["stream_id"],
        "token": token,
        "lang": str(data.get("lang", defaults["lang"])).strip() or defaults["lang"],
        "ext": ext,
        "dest": dest,
        "first_id": first_id,
        "start_num": start_num,
        "ep_count": ep_count,
        "base_name": base_name,
        "special_eps": special_eps,
    }

    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "kind": "viki",
            "label": "Viki subtitles",
            "rerun_data": {
                "type": "viki",
                "first_id": first_id,
                "ep_count": ep_count,
                "base_name": base_name,
                "special_eps": " ".join(str(ep) for ep in special_eps),
                "dest": dest,
                "stream_id": config["stream_id"],
                "ext": config["ext"],
                "lang": config["lang"],
                "app_param": config["app_param"],
                "token": config["token"],
                "base_url": config["base_url"],
            },
            "status": "queued",
            "urls": ["{} episodes from {}".format(ep_count, first_id)],
            "total": ep_count,
            "dest": dest,
            "method": "viki-subs",
            "output": ["Job {} -- Viki subtitles -> {}".format(job_id, dest)],
            "pid": None,
            "current_idx": 0,
            "current_url": "",
            "current_cmd": "",
            "failed_urls": [],
            "progress": {},
            "verified_files": [],
            "started": None,
            "finished": None,
            "created_at": created_at,
            "updated_at": created_at,
            "command": "viki-subs",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_viki_job, args=(job_id, config), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Viki subtitle job started -- {}".format(job_id)}, 200


def start_subtitle_shift_job(data):
    folder = str(data.get("folder") or "").strip()
    if not folder:
        return legacy_error_payload("folder is required"), 400
    try:
        folder = safe_path(folder)
    except ValueError as exc:
        return legacy_error_payload("Invalid folder: {}".format(exc)), 400

    target = Path(folder)
    if not target.exists() or not target.is_dir():
        return legacy_error_payload("Folder not found"), 404
    subtitle_count = len([entry for entry in target.iterdir() if entry.is_file() and entry.suffix.lower() == ".srt"])
    if subtitle_count <= 0:
        return legacy_error_payload("No .srt files found in this folder"), 404

    try:
        seconds = float(str(data.get("seconds", "")).strip())
    except Exception:
        return legacy_error_payload("seconds must be a valid number"), 400

    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "kind": "subtitle_shift",
            "label": "Subtitle timing shift",
            "rerun_data": {
                "type": "subtitle_shift",
                "folder": folder,
                "seconds": seconds,
            },
            "status": "queued",
            "urls": ["{} ({:+g}s)".format(folder, seconds)],
            "total": subtitle_count,
            "dest": folder,
            "method": "subtitle-shift",
            "output": ["Job {} -- Subtitle shift -> {}".format(job_id, folder)],
            "pid": None,
            "current_idx": 0,
            "current_url": "",
            "current_cmd": "",
            "failed_urls": [],
            "progress": {},
            "verified_files": [],
            "started": None,
            "finished": None,
            "created_at": created_at,
            "updated_at": created_at,
            "command": "subtitle-shift",
        }
    _persist_job(job_id)
    threading.Thread(
        target=_run_subtitle_shift_job,
        args=(job_id, {"folder": folder, "seconds": seconds}),
        daemon=True,
    ).start()
    return {"ok": True, "job_id": job_id, "msg": "Subtitle shift job started -- {}".format(job_id)}, 200


def start_subtitle_convert_job(data):
    folder = str(data.get("folder") or "").strip()
    if not folder:
        return legacy_error_payload("folder is required"), 400
    try:
        folder = safe_path(folder)
    except ValueError as exc:
        return legacy_error_payload("Invalid folder: {}".format(exc)), 400
    target = Path(folder)
    if not target.exists() or not target.is_dir():
        return legacy_error_payload("Folder not found"), 404
    subtitle_count = len([entry for entry in target.iterdir() if entry.is_file() and entry.suffix.lower() in SUBTITLE_CONVERT_EXTS])
    if subtitle_count <= 0:
        return legacy_error_payload("No convertible subtitle files found in this folder"), 404

    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "kind": "subtitle_convert",
            "label": "Subtitle convert to SRT",
            "rerun_data": {"type": "subtitle_convert", "folder": folder},
            "status": "queued",
            "urls": [folder],
            "total": subtitle_count,
            "dest": folder,
            "method": "subtitle-convert",
            "output": ["Job {} -- Subtitle convert -> {}".format(job_id, folder)],
            "pid": None,
            "current_idx": 0,
            "current_url": "",
            "current_cmd": "",
            "failed_urls": [],
            "progress": {},
            "verified_files": [],
            "started": None,
            "finished": None,
            "created_at": created_at,
            "updated_at": created_at,
            "command": "subtitle-convert",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_subtitle_convert_job, args=(job_id, {"folder": folder}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Subtitle convert job started -- {}".format(job_id)}, 200


def start_subtitle_match_job(data):
    folder = str(data.get("folder") or "").strip()
    if not folder:
        return legacy_error_payload("folder is required"), 400
    try:
        folder = safe_path(folder)
    except ValueError as exc:
        return legacy_error_payload("Invalid folder: {}".format(exc)), 400
    target = Path(folder)
    if not target.exists() or not target.is_dir():
        return legacy_error_payload("Folder not found"), 404
    videos, subtitles, _ = _match_subtitle_targets(folder)
    if not videos:
        return legacy_error_payload("No video files found in this folder"), 404
    if not subtitles:
        return legacy_error_payload("No subtitle files found in this folder"), 404

    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "kind": "subtitle_match",
            "label": "Subtitle rename / match",
            "rerun_data": {"type": "subtitle_match", "folder": folder},
            "status": "queued",
            "urls": [folder],
            "total": len(subtitles),
            "dest": folder,
            "method": "subtitle-match",
            "output": ["Job {} -- Subtitle rename/match -> {}".format(job_id, folder)],
            "pid": None,
            "current_idx": 0,
            "current_url": "",
            "current_cmd": "",
            "failed_urls": [],
            "progress": {},
            "verified_files": [],
            "started": None,
            "finished": None,
            "created_at": created_at,
            "updated_at": created_at,
            "command": "subtitle-match",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_subtitle_match_job, args=(job_id, {"folder": folder}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Subtitle match job started -- {}".format(job_id)}, 200


def start_media_rename_job(data):
    folder = str(data.get("folder") or "").strip()
    if not folder:
        return legacy_error_payload("folder is required"), 400
    try:
        folder = safe_path(folder)
    except ValueError as exc:
        return legacy_error_payload("Invalid folder: {}".format(exc)), 400
    include_dirs = bool(data.get("include_dirs", False))
    target = Path(folder)
    if not target.exists() or not target.is_dir():
        return legacy_error_payload("Folder not found"), 404

    candidates = [
        entry for entry in target.iterdir()
        if (entry.is_dir() and include_dirs) or (entry.is_file() and entry.suffix.lower() in VIDEO_EXTS | SUBTITLE_EXTS | MEDIA_SIDECAR_EXTS)
    ]
    if not candidates:
        return legacy_error_payload("No media files or folders found in this folder"), 404

    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "kind": "media_rename",
            "label": "Media rename cleanup",
            "rerun_data": {"type": "media_rename", "folder": folder, "include_dirs": include_dirs},
            "status": "queued",
            "urls": [folder],
            "total": len(candidates),
            "dest": folder,
            "method": "media-rename",
            "output": ["Job {} -- Media rename -> {}".format(job_id, folder)],
            "pid": None,
            "current_idx": 0,
            "current_url": "",
            "current_cmd": "",
            "failed_urls": [],
            "progress": {},
            "verified_files": [],
            "started": None,
            "finished": None,
            "created_at": created_at,
            "updated_at": created_at,
            "command": "media-rename",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_media_rename_job, args=(job_id, {"folder": folder, "include_dirs": include_dirs}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Media rename job started -- {}".format(job_id)}, 200


def start_media_organize_job(data):
    source = str(data.get("source") or "").strip()
    destination = str(data.get("destination") or "").strip()
    mode = str(data.get("mode") or "folder_per_video").strip()
    if not source or not destination:
        return legacy_error_payload("source and destination are required"), 400
    try:
        source = safe_path(source)
        destination = safe_path(destination)
    except ValueError as exc:
        return legacy_error_payload("Invalid path: {}".format(exc)), 400
    if mode not in {"flat", "folder_per_video"}:
        mode = "folder_per_video"
    src = Path(source)
    if not src.exists() or not src.is_dir():
        return legacy_error_payload("Source folder not found"), 404
    candidates = [entry for entry in src.iterdir() if entry.is_file() and entry.suffix.lower() in VIDEO_EXTS | MEDIA_SIDECAR_EXTS]
    if not candidates:
        return legacy_error_payload("No media files found in source folder"), 404

    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "kind": "media_organize",
            "label": "Media move / organize",
            "rerun_data": {"type": "media_organize", "source": source, "destination": destination, "mode": mode},
            "status": "queued",
            "urls": ["{} -> {}".format(source, destination)],
            "total": len(candidates),
            "dest": destination,
            "method": "media-organize",
            "output": ["Job {} -- Media organize -> {}".format(job_id, source)],
            "pid": None,
            "current_idx": 0,
            "current_url": "",
            "current_cmd": "",
            "failed_urls": [],
            "progress": {},
            "verified_files": [],
            "started": None,
            "finished": None,
            "created_at": created_at,
            "updated_at": created_at,
            "command": "media-organize",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_media_organize_job, args=(job_id, {"source": source, "destination": destination, "mode": mode}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Media organize job started -- {}".format(job_id)}, 200


def start_jellyfin_rescan_job(data):
    url = str(data.get("url") or JELLYFIN_URL).strip() or JELLYFIN_URL
    api_key = str(data.get("api_key") or JELLYFIN_API_KEY).strip()
    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "kind": "jellyfin_rescan",
            "label": "Jellyfin library rescan",
            "rerun_data": {"type": "jellyfin_rescan", "url": url, "api_key": api_key},
            "status": "queued",
            "urls": [url],
            "total": 1,
            "dest": url,
            "method": "jellyfin-rescan",
            "output": ["Job {} -- Jellyfin rescan -> {}".format(job_id, url)],
            "pid": None,
            "current_idx": 0,
            "current_url": "",
            "current_cmd": "",
            "failed_urls": [],
            "progress": {},
            "verified_files": [],
            "started": None,
            "finished": None,
            "created_at": created_at,
            "updated_at": created_at,
            "command": "jellyfin-rescan",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_jellyfin_rescan_job, args=(job_id, {"url": url, "api_key": api_key}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Jellyfin rescan job started -- {}".format(job_id)}, 200


def start_bulk_rename_job(data):
    folder = str(data.get("folder") or "").strip()
    find_text = str(data.get("find_text") or "").strip()
    replace_text = str(data.get("replace_text") or "")
    recursive = bool(data.get("recursive", True))
    include_dirs = bool(data.get("include_dirs", False))
    if not folder or not find_text:
        return legacy_error_payload("folder and find_text are required"), 400
    try:
        folder = safe_path(folder)
    except ValueError as exc:
        return legacy_error_payload("Invalid folder: {}".format(exc)), 400
    target = Path(folder)
    if not target.exists() or not target.is_dir():
        return legacy_error_payload("Folder not found"), 404
    entries = []
    walker = target.rglob("*") if recursive else target.iterdir()
    for entry in walker:
        if entry.is_file() or (include_dirs and entry.is_dir()):
            if find_text.lower() in entry.name.lower():
                entries.append(entry)
    if not entries:
        return legacy_error_payload("No matching files or folders found"), 404
    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "kind": "bulk_rename", "label": "Bulk rename",
            "rerun_data": {"type": "bulk_rename", "folder": folder, "find_text": find_text, "replace_text": replace_text, "recursive": recursive, "include_dirs": include_dirs},
            "status": "queued", "urls": [folder], "total": len(entries), "dest": folder, "method": "bulk-rename",
            "output": ["Job {} -- Bulk rename -> {}".format(job_id, folder)], "pid": None, "current_idx": 0, "current_url": "", "current_cmd": "",
            "failed_urls": [], "progress": {}, "verified_files": [], "started": None, "finished": None, "created_at": created_at, "updated_at": created_at, "command": "bulk-rename",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_bulk_rename_job, args=(job_id, {"folder": folder, "find_text": find_text, "replace_text": replace_text, "recursive": recursive, "include_dirs": include_dirs}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Bulk rename job started -- {}".format(job_id)}, 200


def start_empty_folder_cleanup_job(data):
    folder = str(data.get("folder") or "").strip()
    recursive = bool(data.get("recursive", True))
    if not folder:
        return legacy_error_payload("folder is required"), 400
    try:
        folder = safe_path(folder)
    except ValueError as exc:
        return legacy_error_payload("Invalid folder: {}".format(exc)), 400
    target = Path(folder)
    if not target.exists() or not target.is_dir():
        return legacy_error_payload("Folder not found"), 404
    dirs = [p for p in target.rglob("*") if p.is_dir()] if recursive else [p for p in target.iterdir() if p.is_dir()]
    if not dirs:
        return legacy_error_payload("No folders found to check"), 404
    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "kind": "empty_folder_cleanup", "label": "Empty folder cleanup",
            "rerun_data": {"type": "empty_folder_cleanup", "folder": folder, "recursive": recursive},
            "status": "queued", "urls": [folder], "total": len(dirs), "dest": folder, "method": "empty-folder-cleanup",
            "output": ["Job {} -- Empty folder cleanup -> {}".format(job_id, folder)], "pid": None, "current_idx": 0, "current_url": "", "current_cmd": "",
            "failed_urls": [], "progress": {}, "verified_files": [], "started": None, "finished": None, "created_at": created_at, "updated_at": created_at, "command": "empty-folder-cleanup",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_empty_folder_cleanup_job, args=(job_id, {"folder": folder, "recursive": recursive}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Empty folder cleanup job started -- {}".format(job_id)}, 200


def start_duplicate_scan_job(data):
    folder = str(data.get("folder") or "").strip()
    recursive = bool(data.get("recursive", True))
    if not folder:
        return legacy_error_payload("folder is required"), 400
    try:
        folder = safe_path(folder)
    except ValueError as exc:
        return legacy_error_payload("Invalid folder: {}".format(exc)), 400
    target = Path(folder)
    if not target.exists() or not target.is_dir():
        return legacy_error_payload("Folder not found"), 404
    files = list(_iter_files(folder, recursive=recursive))
    if not files:
        return legacy_error_payload("No files found"), 404
    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "kind": "duplicate_scan", "label": "Duplicate file scan",
            "rerun_data": {"type": "duplicate_scan", "folder": folder, "recursive": recursive},
            "status": "queued", "urls": [folder], "total": len(files), "dest": folder, "method": "duplicate-scan",
            "output": ["Job {} -- Duplicate scan -> {}".format(job_id, folder)], "pid": None, "current_idx": 0, "current_url": "", "current_cmd": "",
            "failed_urls": [], "progress": {}, "verified_files": [], "started": None, "finished": None, "created_at": created_at, "updated_at": created_at, "command": "duplicate-scan",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_duplicate_scan_job, args=(job_id, {"folder": folder, "recursive": recursive}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Duplicate scan job started -- {}".format(job_id)}, 200


def start_largest_files_job(data):
    folder = str(data.get("folder") or "").strip()
    recursive = bool(data.get("recursive", True))
    limit = int(data.get("limit", 20) or 20)
    if not folder:
        return legacy_error_payload("folder is required"), 400
    try:
        folder = safe_path(folder)
    except ValueError as exc:
        return legacy_error_payload("Invalid folder: {}".format(exc)), 400
    target = Path(folder)
    if not target.exists() or not target.is_dir():
        return legacy_error_payload("Folder not found"), 404
    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "kind": "largest_files", "label": "Largest files report",
            "rerun_data": {"type": "largest_files", "folder": folder, "recursive": recursive, "limit": limit},
            "status": "queued", "urls": [folder], "total": limit, "dest": folder, "method": "largest-files",
            "output": ["Job {} -- Largest files -> {}".format(job_id, folder)], "pid": None, "current_idx": 0, "current_url": "", "current_cmd": "",
            "failed_urls": [], "progress": {}, "verified_files": [], "started": None, "finished": None, "created_at": created_at, "updated_at": created_at, "command": "largest-files",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_largest_files_job, args=(job_id, {"folder": folder, "recursive": recursive, "limit": limit}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Largest files job started -- {}".format(job_id)}, 200


def start_folder_size_job(data):
    folder = str(data.get("folder") or "").strip()
    recursive = bool(data.get("recursive", True))
    limit = int(data.get("limit", 20) or 20)
    if not folder:
        return legacy_error_payload("folder is required"), 400
    try:
        folder = safe_path(folder)
    except ValueError as exc:
        return legacy_error_payload("Invalid folder: {}".format(exc)), 400
    target = Path(folder)
    if not target.exists() or not target.is_dir():
        return legacy_error_payload("Folder not found"), 404
    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "kind": "folder_size", "label": "Folder size analysis",
            "rerun_data": {"type": "folder_size", "folder": folder, "recursive": recursive, "limit": limit},
            "status": "queued", "urls": [folder], "total": limit, "dest": folder, "method": "folder-size",
            "output": ["Job {} -- Folder size analysis -> {}".format(job_id, folder)], "pid": None, "current_idx": 0, "current_url": "", "current_cmd": "",
            "failed_urls": [], "progress": {}, "verified_files": [], "started": None, "finished": None, "created_at": created_at, "updated_at": created_at, "command": "folder-size",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_folder_size_job, args=(job_id, {"folder": folder, "recursive": recursive, "limit": limit}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Folder size analysis job started -- {}".format(job_id)}, 200


def start_archive_extract_job(data):
    mode = str(data.get("mode") or "archive").strip()
    source = str(data.get("source") or "").strip()
    destination = str(data.get("destination") or "").strip()
    archive_format = str(data.get("format") or "zip").strip()
    if not source or not destination:
        return legacy_error_payload("source and destination are required"), 400
    if mode not in {"archive", "extract"}:
        return legacy_error_payload("mode must be archive or extract"), 400
    if archive_format not in ARCHIVE_FORMATS:
        archive_format = "zip"
    try:
        source = safe_path(source)
        destination = safe_path(destination)
    except ValueError as exc:
        return legacy_error_payload("Invalid path: {}".format(exc)), 400
    source_path = Path(source)
    if not source_path.exists():
        return legacy_error_payload("Source not found"), 404
    if mode == "archive" and not source_path.is_dir():
        return legacy_error_payload("Archive mode expects a folder source"), 400
    if mode == "extract" and not source_path.is_file():
        return legacy_error_payload("Extract mode expects an archive file"), 400
    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "kind": "archive_extract", "label": "Archive / extract",
            "rerun_data": {"type": "archive_extract", "mode": mode, "source": source, "destination": destination, "format": archive_format},
            "status": "queued", "urls": [source], "total": 1, "dest": destination, "method": "archive-extract",
            "output": ["Job {} -- Archive / extract -> {}".format(job_id, source)], "pid": None, "current_idx": 0, "current_url": "", "current_cmd": "",
            "failed_urls": [], "progress": {}, "verified_files": [], "started": None, "finished": None, "created_at": created_at, "updated_at": created_at, "command": "archive-extract",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_archive_extract_job, args=(job_id, {"mode": mode, "source": source, "destination": destination, "format": archive_format}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Archive/extract job started -- {}".format(job_id)}, 200


def start_backup_folder_job(data):
    source = str(data.get("source") or "").strip()
    destination = str(data.get("destination") or "").strip()
    archive_format = str(data.get("format") or "zip").strip()
    if not source or not destination:
        return legacy_error_payload("source and destination are required"), 400
    if archive_format not in ARCHIVE_FORMATS:
        archive_format = "zip"
    try:
        source = safe_path(source)
        destination = safe_path(destination)
    except ValueError as exc:
        return legacy_error_payload("Invalid path: {}".format(exc)), 400
    source_path = Path(source)
    if not source_path.exists() or not source_path.is_dir():
        return legacy_error_payload("Source folder not found"), 404
    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "kind": "backup_folder", "label": "Folder backup",
            "rerun_data": {"type": "backup_folder", "source": source, "destination": destination, "format": archive_format},
            "status": "queued", "urls": [source], "total": 1, "dest": destination, "method": "backup-folder",
            "output": ["Job {} -- Folder backup -> {}".format(job_id, source)], "pid": None, "current_idx": 0, "current_url": "", "current_cmd": "",
            "failed_urls": [], "progress": {}, "verified_files": [], "started": None, "finished": None, "created_at": created_at, "updated_at": created_at, "command": "backup-folder",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_backup_folder_job, args=(job_id, {"source": source, "destination": destination, "format": archive_format}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Folder backup job started -- {}".format(job_id)}, 200


def start_orphan_subtitle_job(data):
    folder = str(data.get("folder") or "").strip()
    recursive = bool(data.get("recursive", True))
    if not folder:
        return legacy_error_payload("folder is required"), 400
    try:
        folder = safe_path(folder)
    except ValueError as exc:
        return legacy_error_payload("Invalid folder: {}".format(exc)), 400
    target = Path(folder)
    if not target.exists() or not target.is_dir():
        return legacy_error_payload("Folder not found"), 404
    files = list(_iter_files(folder, recursive=recursive))
    if not files:
        return legacy_error_payload("No files found"), 404
    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "kind": "orphan_subtitle", "label": "Orphan subtitle scan",
            "rerun_data": {"type": "orphan_subtitle", "folder": folder, "recursive": recursive},
            "status": "queued", "urls": [folder], "total": len(files), "dest": folder, "method": "orphan-subtitle",
            "output": ["Job {} -- Orphan subtitle scan -> {}".format(job_id, folder)], "pid": None, "current_idx": 0, "current_url": "", "current_cmd": "",
            "failed_urls": [], "progress": {}, "verified_files": [], "started": None, "finished": None, "created_at": created_at, "updated_at": created_at, "command": "orphan-subtitle",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_orphan_subtitle_job, args=(job_id, {"folder": folder, "recursive": recursive}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Orphan subtitle scan job started -- {}".format(job_id)}, 200


def start_temp_cleanup_job(data):
    folder = str(data.get("folder") or "").strip()
    recursive = bool(data.get("recursive", True))
    if not folder:
        return legacy_error_payload("folder is required"), 400
    try:
        folder = safe_path(folder)
    except ValueError as exc:
        return legacy_error_payload("Invalid folder: {}".format(exc)), 400
    target = Path(folder)
    if not target.exists() or not target.is_dir():
        return legacy_error_payload("Folder not found"), 404
    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "kind": "temp_cleanup", "label": "Temp & cache cleanup",
            "rerun_data": {"type": "temp_cleanup", "folder": folder, "recursive": recursive},
            "status": "queued", "urls": [folder], "total": 1, "dest": folder, "method": "temp-cleanup",
            "output": ["Job {} -- Temp cleanup -> {}".format(job_id, folder)], "pid": None, "current_idx": 0, "current_url": "", "current_cmd": "",
            "failed_urls": [], "progress": {}, "verified_files": [], "started": None, "finished": None, "created_at": created_at, "updated_at": created_at, "command": "temp-cleanup",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_temp_cleanup_job, args=(job_id, {"folder": folder, "recursive": recursive}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Temp cleanup job started -- {}".format(job_id)}, 200


def start_log_cleanup_job(data):
    folder = str(data.get("folder") or "").strip()
    recursive = bool(data.get("recursive", True))
    older_than_days = int(data.get("older_than_days", 7) or 7)
    if not folder:
        return legacy_error_payload("folder is required"), 400
    try:
        folder = safe_path(folder)
    except ValueError as exc:
        return legacy_error_payload("Invalid folder: {}".format(exc)), 400
    target = Path(folder)
    if not target.exists() or not target.is_dir():
        return legacy_error_payload("Folder not found"), 404
    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "kind": "log_cleanup", "label": "Log cleanup",
            "rerun_data": {"type": "log_cleanup", "folder": folder, "recursive": recursive, "older_than_days": older_than_days},
            "status": "queued", "urls": [folder], "total": 1, "dest": folder, "method": "log-cleanup",
            "output": ["Job {} -- Log cleanup -> {}".format(job_id, folder)], "pid": None, "current_idx": 0, "current_url": "", "current_cmd": "",
            "failed_urls": [], "progress": {}, "verified_files": [], "started": None, "finished": None, "created_at": created_at, "updated_at": created_at, "command": "log-cleanup",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_log_cleanup_job, args=(job_id, {"folder": folder, "recursive": recursive, "older_than_days": older_than_days}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Log cleanup job started -- {}".format(job_id)}, 200


def start_system_report_job(data):
    destination = str(data.get("destination") or "").strip()
    period = str(data.get("period") or "snapshot").strip() or "snapshot"
    if not destination:
        return legacy_error_payload("destination is required"), 400
    try:
        destination = safe_path(destination)
    except ValueError as exc:
        return legacy_error_payload("Invalid destination: {}".format(exc)), 400
    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "kind": "system_report", "label": "System report",
            "rerun_data": {"type": "system_report", "destination": destination, "period": period},
            "status": "queued", "urls": [destination], "total": 1, "dest": destination, "method": "system-report",
            "output": ["Job {} -- System report -> {}".format(job_id, destination)], "pid": None, "current_idx": 0, "current_url": "", "current_cmd": "",
            "failed_urls": [], "progress": {}, "verified_files": [], "started": None, "finished": None, "created_at": created_at, "updated_at": created_at, "command": "system-report",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_system_report_job, args=(job_id, {"destination": destination, "period": period}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "System report job started -- {}".format(job_id)}, 200


def start_disk_snapshot_job(data):
    destination = str(data.get("destination") or "").strip()
    period = str(data.get("period") or "snapshot").strip() or "snapshot"
    if not destination:
        return legacy_error_payload("destination is required"), 400
    try:
        destination = safe_path(destination)
    except ValueError as exc:
        return legacy_error_payload("Invalid destination: {}".format(exc)), 400
    job_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "kind": "disk_snapshot", "label": "Disk snapshot report",
            "rerun_data": {"type": "disk_snapshot", "destination": destination, "period": period},
            "status": "queued", "urls": [destination], "total": 1, "dest": destination, "method": "disk-snapshot",
            "output": ["Job {} -- Disk snapshot -> {}".format(job_id, destination)], "pid": None, "current_idx": 0, "current_url": "", "current_cmd": "",
            "failed_urls": [], "progress": {}, "verified_files": [], "started": None, "finished": None, "created_at": created_at, "updated_at": created_at, "command": "disk-snapshot",
        }
    _persist_job(job_id)
    threading.Thread(target=_run_disk_snapshot_job, args=(job_id, {"destination": destination, "period": period}), daemon=True).start()
    return {"ok": True, "job_id": job_id, "msg": "Disk snapshot job started -- {}".format(job_id)}, 200


def list_jobs():
    with _jobs_lock:
        return [_public_job(job) for job in _jobs.values()]


def get_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return None
    return _public_job(job)


def stream_job_output(job_id):
    def generate():
        sent = 0
        while True:
            with _jobs_lock:
                job = _jobs.get(job_id)
            if not job:
                yield "data: {}\n\n".format(json.dumps("__DONE__"))
                return
            lines = job["output"]
            while sent < len(lines):
                yield "data: {}\n\n".format(json.dumps(lines[sent]))
                sent += 1
            if job["status"] in ("done", "failed", "partial", "cancelled", "interrupted", "unknown"):
                yield "data: {}\n\n".format(json.dumps("__DONE__"))
                return
            time.sleep(0.5)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def cancel_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return legacy_error_payload("Not found"), 404
    pid = job.get("pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        except ProcessLookupError:
            pass
    _set_job_fields(job_id, status="cancelled", finished=time.strftime("%H:%M:%S"), pid=None)
    return {"ok": True, "msg": "Cancelled"}, 200


def rerun_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return legacy_error_payload("Job not found"), 404

    status = job.get("status")
    if status in {"queued", "running"}:
        return legacy_error_payload("Job is still in progress"), 400

    rerun_data = job.get("rerun_data") or {}
    rerun_type = rerun_data.get("type")
    if rerun_type == "download":
        payload = {
            "urls": rerun_data.get("urls") or job.get("urls") or [],
            "dest": rerun_data.get("dest") or job.get("dest") or MOUNT_POINT_BASE,
            "method": rerun_data.get("method") or job.get("method") or "auto",
        }
        return start_download(payload)

    if rerun_type == "viki":
        payload = {
            "first_id": rerun_data.get("first_id", ""),
            "ep_count": rerun_data.get("ep_count", 0),
            "base_name": rerun_data.get("base_name", ""),
            "special_eps": rerun_data.get("special_eps", ""),
            "dest": rerun_data.get("dest") or job.get("dest") or MOUNT_POINT_BASE,
            "stream_id": rerun_data.get("stream_id", ""),
            "ext": rerun_data.get("ext", ""),
            "lang": rerun_data.get("lang", ""),
            "app_param": rerun_data.get("app_param", ""),
            "token": rerun_data.get("token", ""),
            "base_url": rerun_data.get("base_url", ""),
        }
        return start_viki_subtitles(payload)

    if rerun_type == "subtitle_shift":
        payload = {
            "folder": rerun_data.get("folder", "") or job.get("dest") or MOUNT_POINT_BASE,
            "seconds": rerun_data.get("seconds", 0),
        }
        return start_subtitle_shift_job(payload)

    if rerun_type == "subtitle_convert":
        return start_subtitle_convert_job({"folder": rerun_data.get("folder", "") or job.get("dest") or MOUNT_POINT_BASE})

    if rerun_type == "subtitle_match":
        return start_subtitle_match_job({"folder": rerun_data.get("folder", "") or job.get("dest") or MOUNT_POINT_BASE})

    if rerun_type == "media_rename":
        return start_media_rename_job(
            {
                "folder": rerun_data.get("folder", "") or job.get("dest") or MOUNT_POINT_BASE,
                "include_dirs": rerun_data.get("include_dirs", False),
            }
        )

    if rerun_type == "media_organize":
        return start_media_organize_job(
            {
                "source": rerun_data.get("source", "") or MOUNT_POINT_BASE,
                "destination": rerun_data.get("destination", "") or MOUNT_POINT_BASE,
                "mode": rerun_data.get("mode", "folder_per_video"),
            }
        )

    if rerun_type == "jellyfin_rescan":
        return start_jellyfin_rescan_job(
            {
                "url": rerun_data.get("url", "") or JELLYFIN_URL,
                "api_key": rerun_data.get("api_key", "") or JELLYFIN_API_KEY,
            }
        )

    if rerun_type == "bulk_rename":
        return start_bulk_rename_job(
            {
                "folder": rerun_data.get("folder", "") or job.get("dest") or MOUNT_POINT_BASE,
                "find_text": rerun_data.get("find_text", ""),
                "replace_text": rerun_data.get("replace_text", ""),
                "recursive": rerun_data.get("recursive", True),
                "include_dirs": rerun_data.get("include_dirs", False),
            }
        )

    if rerun_type == "empty_folder_cleanup":
        return start_empty_folder_cleanup_job(
            {
                "folder": rerun_data.get("folder", "") or job.get("dest") or MOUNT_POINT_BASE,
                "recursive": rerun_data.get("recursive", True),
            }
        )

    if rerun_type == "duplicate_scan":
        return start_duplicate_scan_job(
            {
                "folder": rerun_data.get("folder", "") or job.get("dest") or MOUNT_POINT_BASE,
                "recursive": rerun_data.get("recursive", True),
            }
        )

    if rerun_type == "largest_files":
        return start_largest_files_job(
            {
                "folder": rerun_data.get("folder", "") or job.get("dest") or MOUNT_POINT_BASE,
                "limit": rerun_data.get("limit", 20),
                "recursive": rerun_data.get("recursive", True),
            }
        )

    if rerun_type == "folder_size":
        return start_folder_size_job(
            {
                "folder": rerun_data.get("folder", "") or job.get("dest") or MOUNT_POINT_BASE,
                "limit": rerun_data.get("limit", 12),
            }
        )

    if rerun_type == "archive_extract":
        return start_archive_extract_job(
            {
                "source": rerun_data.get("source", "") or job.get("dest") or MOUNT_POINT_BASE,
                "destination": rerun_data.get("destination", "") or job.get("dest") or MOUNT_POINT_BASE,
                "mode": rerun_data.get("mode", "archive"),
                "format": rerun_data.get("format", "zip"),
            }
        )

    if rerun_type == "backup_folder":
        return start_backup_folder_job(
            {
                "source": rerun_data.get("source", "") or job.get("dest") or MOUNT_POINT_BASE,
                "destination": rerun_data.get("destination", "") or job.get("dest") or MOUNT_POINT_BASE,
                "format": rerun_data.get("format", "zip"),
            }
        )

    if rerun_type == "orphan_subtitle":
        return start_orphan_subtitle_job(
            {
                "folder": rerun_data.get("folder", "") or job.get("dest") or MOUNT_POINT_BASE,
                "recursive": rerun_data.get("recursive", True),
            }
        )

    if rerun_type == "temp_cleanup":
        return start_temp_cleanup_job(
            {
                "folder": rerun_data.get("folder", "") or job.get("dest") or MOUNT_POINT_BASE,
                "recursive": rerun_data.get("recursive", True),
            }
        )

    if rerun_type == "log_cleanup":
        return start_log_cleanup_job(
            {
                "folder": rerun_data.get("folder", "") or job.get("dest") or MOUNT_POINT_BASE,
                "recursive": rerun_data.get("recursive", True),
                "older_than_days": rerun_data.get("older_than_days", 7),
            }
        )

    if rerun_type == "system_report":
        return start_system_report_job(
            {
                "destination": rerun_data.get("destination", "") or job.get("dest") or MOUNT_POINT_BASE,
                "period": rerun_data.get("period", "snapshot"),
            }
        )

    if rerun_type == "disk_snapshot":
        return start_disk_snapshot_job(
            {
                "destination": rerun_data.get("destination", "") or job.get("dest") or MOUNT_POINT_BASE,
                "period": rerun_data.get("period", "snapshot"),
            }
        )

    return legacy_error_payload("This job cannot be rerun"), 400


def edit_job_payload(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return legacy_error_payload("Job not found"), 404

    rerun_data = dict(job.get("rerun_data") or {})
    if not rerun_data:
        return legacy_error_payload("This job cannot be edited"), 400

    return {
        "ok": True,
        "job_id": job_id,
        "kind": job.get("kind", "download"),
        "label": job.get("label", ""),
        "data": rerun_data,
    }, 200


def delete_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return legacy_error_payload("Job not found"), 404
        if job.get("status") in {"queued", "running"}:
            return legacy_error_payload("Cancel the running job before removing it"), 400
        del _jobs[job_id]

    with _db_lock:
        conn = _db()
        try:
            conn.execute("DELETE FROM download_jobs WHERE id = ?", (job_id,))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "msg": "Removed job {}".format(job_id)}, 200


def clear_jobs():
    with _jobs_lock:
        done_ids = [
            job_id
            for job_id, job in _jobs.items()
            if job["status"] in ("done", "failed", "cancelled", "partial", "interrupted", "unknown")
        ]
        for job_id in done_ids:
            del _jobs[job_id]
    with _db_lock:
        conn = _db()
        try:
            conn.executemany("DELETE FROM download_jobs WHERE id = ?", [(job_id,) for job_id in done_ids])
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "msg": "Cleared {} job(s)".format(len(done_ids))}, 200


def browse_download_path(path):
    try:
        base = safe_path(path or MOUNT_POINT_BASE)
        entries = []
        current = Path(base)
        if current.parent != current:
            entries.append({"name": "..", "path": str(current.parent), "type": "dir"})
        for entry in sorted(current.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                entries.append({"name": entry.name, "path": str(entry), "type": "dir"})
        return {"path": base, "entries": entries}, 200
    except Exception as exc:
        return {"error": str(exc)}, 400


def tools_status():
    return {tool: has_tool(tool) for tool in ["yt-dlp", "aria2c", "gallery-dl", "wget", "curl", "ffmpeg"]}
