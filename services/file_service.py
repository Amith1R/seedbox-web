import base64
import mimetypes
import re
import shutil
from pathlib import Path

from werkzeug.utils import secure_filename

from services.system_service import human_size, legacy_error_payload, safe_path

TEXT_PREVIEW_LIMIT = 100_000
IMAGE_PREVIEW_LIMIT = 2_000_000
SRT_TS_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})(.*)$")


def list_files(path):
    try:
        path = safe_path(path)
        current = Path(path)
        entries = []
        file_count = 0
        dir_count = 0
        total_size = 0
        for entry in sorted(current.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            try:
                stat = entry.stat()
                is_dir = entry.is_dir()
                size = stat.st_size
                if is_dir:
                    dir_count += 1
                else:
                    file_count += 1
                    total_size += size
                entries.append(
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "type": "dir" if is_dir else "file",
                        "size": size,
                        "size_str": "" if is_dir else human_size(size),
                        "mtime": stat.st_mtime,
                        "mtime_str": __import__("datetime").datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                        "ext": entry.suffix.lower() if entry.is_file() else "",
                    }
                )
            except Exception:
                pass
        parent = str(current.parent) if current.parent != current else None
        return {
            "path": path,
            "parent": parent,
            "entries": entries,
            "summary": {"dirs": dir_count, "files": file_count, "total_size": total_size, "total_size_str": human_size(total_size)},
        }, 200
    except Exception as exc:
        return legacy_error_payload(str(exc)), 400


def mkdir(base_path, name):
    if not base_path or not name:
        return legacy_error_payload("Path and folder name required"), 400
    if "/" in name or "\\" in name or "\x00" in name:
        return legacy_error_payload("Invalid folder name"), 400
    try:
        base_path = safe_path(base_path)
    except ValueError as exc:
        return legacy_error_payload(str(exc)), 400
    base = Path(base_path)
    if not base.exists() or not base.is_dir():
        return legacy_error_payload("Base folder not found"), 404
    new_dir = base / name
    if new_dir.exists():
        return legacy_error_payload("Folder already exists"), 409
    try:
        new_dir.mkdir(parents=False, exist_ok=False)
        return {"ok": True, "msg": "Created folder '{}'".format(name), "path": str(new_dir)}, 200
    except Exception as exc:
        return legacy_error_payload(str(exc)), 500


def rename_path(old_path, new_name):
    if not old_path or not new_name:
        return legacy_error_payload("Path and name required"), 400
    if "/" in new_name or "\\" in new_name or "\x00" in new_name:
        return legacy_error_payload("Invalid name"), 400
    try:
        old_path = safe_path(old_path)
    except ValueError as exc:
        return legacy_error_payload(str(exc)), 400
    old = Path(old_path)
    if not old.exists():
        return legacy_error_payload("Not found"), 404
    new = old.parent / new_name
    if new.exists():
        return legacy_error_payload("Already exists"), 409
    try:
        old.rename(new)
        return {"ok": True, "msg": "Renamed to '{}'".format(new_name), "new_path": str(new)}, 200
    except Exception as exc:
        return legacy_error_payload(str(exc)), 500


def delete_path(path):
    if not path:
        return legacy_error_payload("Path required"), 400
    try:
        path = safe_path(path)
    except ValueError as exc:
        return legacy_error_payload(str(exc)), 400
    target = Path(path)
    if not target.exists():
        return legacy_error_payload("Not found"), 404
    try:
        if target.is_dir():
            target.rmdir()
            return {"ok": True, "msg": "Removed folder '{}'".format(target.name)}, 200
        target.unlink()
        return {"ok": True, "msg": "Deleted '{}'".format(target.name)}, 200
    except OSError:
        if target.is_dir():
            return legacy_error_payload("Folder is not empty"), 400
        return legacy_error_payload("Delete failed"), 500
    except Exception as exc:
        return legacy_error_payload(str(exc)), 500


def bulk_delete(paths):
    if not isinstance(paths, list) or not paths:
        return legacy_error_payload("paths must be a non-empty list"), 400
    removed = 0
    errors = []
    for raw_path in paths:
        payload, status = delete_path(raw_path)
        if status == 200 and payload.get("ok"):
            removed += 1
        else:
            errors.append({"path": raw_path, "error": payload.get("msg") or payload.get("error")})
    return {"ok": not errors, "msg": "Deleted {} item(s)".format(removed), "removed": removed, "errors": errors}, 200


def move_paths(paths, destination):
    if not isinstance(paths, list) or not paths:
        return legacy_error_payload("paths must be a non-empty list"), 400
    if not destination:
        return legacy_error_payload("Destination required"), 400

    try:
        destination = safe_path(destination)
    except ValueError as exc:
        return legacy_error_payload(str(exc)), 400

    dest_dir = Path(destination)
    if not dest_dir.exists() or not dest_dir.is_dir():
        return legacy_error_payload("Destination folder not found"), 404

    validated_sources = []
    seen_sources = set()
    seen_targets = set()
    errors = []

    for raw_path in paths:
        try:
            source_path = safe_path(raw_path)
        except ValueError as exc:
            errors.append({"path": raw_path, "error": str(exc)})
            continue

        if source_path in seen_sources:
            errors.append({"path": source_path, "error": "Duplicate source path"})
            continue
        seen_sources.add(source_path)

        source = Path(source_path)
        if not source.exists():
            errors.append({"path": source_path, "error": "Not found"})
            continue

        target = dest_dir / source.name
        if target == source:
            errors.append({"path": source_path, "error": "Already in destination folder"})
            continue
        if target.exists():
            errors.append({"path": source_path, "error": "Destination already has '{}'".format(source.name)})
            continue
        if str(target) in seen_targets:
            errors.append({"path": source_path, "error": "Multiple items would become '{}'".format(source.name)})
            continue

        try:
            if source.is_dir() and dest_dir.resolve().is_relative_to(source.resolve()):
                errors.append({"path": source_path, "error": "Cannot move a folder into itself"})
                continue
        except Exception:
            pass

        seen_targets.add(str(target))
        validated_sources.append((source, target))

    if errors:
        return {
            "ok": False,
            "msg": "Move blocked for {} item(s)".format(len(errors)),
            "moved": 0,
            "errors": errors,
        }, 400

    moved = 0
    move_errors = []
    moved_items = []
    for source, target in validated_sources:
        try:
            shutil.move(str(source), str(target))
            moved += 1
            moved_items.append({"from": str(source), "to": str(target)})
        except Exception as exc:
            move_errors.append({"path": str(source), "error": str(exc)})

    return {
        "ok": not move_errors,
        "msg": "Moved {} item(s) to '{}'".format(moved, str(dest_dir)),
        "moved": moved,
        "destination": str(dest_dir),
        "items": moved_items,
        "errors": move_errors,
    }, 200


def upload_file(file_storage, target_path):
    if file_storage is None:
        return legacy_error_payload("No file provided"), 400
    if not target_path:
        return legacy_error_payload("Path required"), 400
    try:
        target_path = safe_path(target_path)
    except ValueError as exc:
        return legacy_error_payload(str(exc)), 400
    target_dir = Path(target_path)
    if not target_dir.exists() or not target_dir.is_dir():
        return legacy_error_payload("Target folder not found"), 404
    filename = secure_filename(file_storage.filename or "")
    if not filename:
        return legacy_error_payload("Invalid filename"), 400
    destination = target_dir / filename
    if destination.exists():
        return legacy_error_payload("File already exists"), 409
    try:
        file_storage.save(str(destination))
        return {"ok": True, "msg": "Uploaded '{}'".format(filename), "path": str(destination)}, 200
    except Exception as exc:
        return legacy_error_payload(str(exc)), 500


def preview_file(path):
    if not path:
        return legacy_error_payload("Path required"), 400
    try:
        path = safe_path(path)
    except ValueError as exc:
        return legacy_error_payload(str(exc)), 400
    target = Path(path)
    if not target.exists() or not target.is_file():
        return legacy_error_payload("File not found"), 404
    mime, _ = mimetypes.guess_type(str(target))
    mime = mime or "application/octet-stream"
    try:
        if mime.startswith("text/") or target.suffix.lower() in {".log", ".txt", ".json", ".nfo", ".srt", ".ass", ".md", ".py", ".sh", ".toml", ".yaml", ".yml"}:
            content = target.read_text(errors="replace")[:TEXT_PREVIEW_LIMIT]
            return {
                "ok": True,
                "preview_type": "text",
                "mime": mime,
                "path": str(target),
                "content": content,
                "truncated": target.stat().st_size > TEXT_PREVIEW_LIMIT,
            }, 200
        if mime.startswith("image/") and target.stat().st_size <= IMAGE_PREVIEW_LIMIT:
            encoded = base64.b64encode(target.read_bytes()).decode("ascii")
            return {
                "ok": True,
                "preview_type": "image",
                "mime": mime,
                "path": str(target),
                "data_url": "data:{};base64,{}".format(mime, encoded),
            }, 200
        return {
            "ok": True,
            "preview_type": "binary",
            "mime": mime,
            "path": str(target),
            "size": target.stat().st_size,
            "size_str": human_size(target.stat().st_size),
        }, 200
    except Exception as exc:
        return legacy_error_payload(str(exc)), 500


def _format_srt_timestamp(total_ms):
    total_ms = max(0, int(total_ms))
    hours = total_ms // 3_600_000
    minutes = (total_ms % 3_600_000) // 60_000
    seconds = (total_ms % 60_000) // 1_000
    millis = total_ms % 1_000
    return "{:02d}:{:02d}:{:02d},{:03d}".format(hours, minutes, seconds, millis)


def _shift_srt_line(line, shift_ms):
    ending = ""
    body = line
    if body.endswith("\r\n"):
        body = body[:-2]
        ending = "\r\n"
    elif body.endswith("\n"):
        body = body[:-1]
        ending = "\n"

    match = SRT_TS_RE.match(body)
    if not match:
        return line, False

    h1, m1, s1, ms1, h2, m2, s2, ms2, tail = match.groups()
    start_ms = int(h1) * 3_600_000 + int(m1) * 60_000 + int(s1) * 1_000 + int(ms1)
    end_ms = int(h2) * 3_600_000 + int(m2) * 60_000 + int(s2) * 1_000 + int(ms2)

    start_ms += shift_ms
    end_ms += shift_ms

    if start_ms < 0:
        start_ms = 0
    if end_ms <= start_ms:
        end_ms = start_ms + 1_000

    shifted = "{} --> {}{}{}".format(
        _format_srt_timestamp(start_ms),
        _format_srt_timestamp(end_ms),
        tail or "",
        ending,
    )
    return shifted, shifted != line


def _shift_subtitle_target(target, shift_seconds):
    try:
        shift_seconds = float(str(shift_seconds).strip())
    except Exception:
        return legacy_error_payload("Invalid shift value"), 400

    shift_ms = int(round(shift_seconds * 1000))
    try:
        original_text = target.read_text(errors="replace")
    except Exception as exc:
        return legacy_error_payload(str(exc)), 500

    original_lines = original_text.splitlines()
    changed_lines = 0
    shifted_any = False
    new_lines = []
    for line in original_text.splitlines(keepends=True):
        shifted_line, changed = _shift_srt_line(line, shift_ms)
        new_lines.append(shifted_line)
        if changed:
            shifted_any = True
            changed_lines += 1
    new_text = "".join(new_lines)

    if len(original_lines) != len(new_text.splitlines()):
        return legacy_error_payload(
            "Line count mismatch. Original: {}, New: {}".format(len(original_lines), len(new_text.splitlines()))
        ), 500

    if not shifted_any or new_text == original_text:
        return {
            "ok": True,
            "changed": False,
            "msg": "No timing changes were needed for '{}'".format(target.name),
            "path": str(target),
            "status": "unchanged",
        }, 200

    backup = Path(str(target) + ".bak")
    try:
        backup.write_text(original_text)
        target.write_text(new_text)
    except Exception as exc:
        return legacy_error_payload(str(exc)), 500

    return {
        "ok": True,
        "changed": True,
        "msg": "Shifted '{}' by {} seconds".format(target.name, shift_seconds),
        "path": str(target),
        "backup_path": str(backup),
        "changed_lines": changed_lines,
        "status": "updated",
    }, 200


def shift_subtitle(path, shift_seconds):
    if not path:
        return legacy_error_payload("Path required"), 400
    try:
        path = safe_path(path)
    except ValueError as exc:
        return legacy_error_payload(str(exc)), 400

    target = Path(path)
    if not target.exists() or not target.is_file():
        return legacy_error_payload("File not found"), 404
    if target.suffix.lower() != ".srt":
        return legacy_error_payload("Only .srt subtitle files are supported"), 400

    return _shift_subtitle_target(target, shift_seconds)


def shift_subtitles_in_folder(path, shift_seconds):
    if not path:
        return legacy_error_payload("Folder path required"), 400
    try:
        path = safe_path(path)
    except ValueError as exc:
        return legacy_error_payload(str(exc)), 400

    folder = Path(path)
    if not folder.exists() or not folder.is_dir():
        return legacy_error_payload("Folder not found"), 404

    subtitle_files = sorted(
        entry for entry in folder.iterdir()
        if entry.is_file() and entry.suffix.lower() == ".srt"
    )
    if not subtitle_files:
        return legacy_error_payload("No .srt files found in this folder"), 404

    results = []
    updated = 0
    unchanged = 0
    failed = 0
    for subtitle_file in subtitle_files:
        payload, status = _shift_subtitle_target(subtitle_file, shift_seconds)
        payload["file"] = subtitle_file.name
        results.append(payload)
        if status != 200 or not payload.get("ok"):
            failed += 1
        elif payload.get("status") == "updated":
            updated += 1
        else:
            unchanged += 1

    return {
        "ok": failed == 0,
        "msg": "Processed {} subtitle file(s)".format(len(subtitle_files)),
        "path": str(folder),
        "results": results,
        "summary": {
            "total": len(subtitle_files),
            "updated": updated,
            "unchanged": unchanged,
            "failed": failed,
        },
    }, 200
