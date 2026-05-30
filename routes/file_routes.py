from flask import Blueprint, jsonify, request

from services import file_service

file_bp = Blueprint("file_bp", __name__)


@file_bp.route("/api/files/list")
def files_list():
    payload, status = file_service.list_files(request.args.get("path", ""))
    return jsonify(payload), status


@file_bp.route("/api/files/mkdir", methods=["POST"])
def files_mkdir():
    data = request.json or {}
    payload, status = file_service.mkdir(data.get("path", "").strip(), data.get("name", "").strip())
    return jsonify(payload), status


@file_bp.route("/api/files/rename", methods=["POST"])
def files_rename():
    data = request.json or {}
    payload, status = file_service.rename_path(data.get("path", "").strip(), data.get("name", "").strip())
    return jsonify(payload), status


@file_bp.route("/api/files/delete", methods=["POST"])
def files_delete():
    data = request.json or {}
    payload, status = file_service.delete_path(data.get("path", "").strip())
    return jsonify(payload), status


@file_bp.route("/api/files/bulk_delete", methods=["POST"])
def files_bulk_delete():
    data = request.json or {}
    payload, status = file_service.bulk_delete(data.get("paths", []))
    return jsonify(payload), status


@file_bp.route("/api/files/move", methods=["POST"])
def files_move():
    data = request.json or {}
    paths = data.get("paths")
    if not isinstance(paths, list):
        single_path = (data.get("path") or "").strip()
        paths = [single_path] if single_path else []
    payload, status = file_service.move_paths(paths, data.get("destination", "").strip())
    return jsonify(payload), status


@file_bp.route("/api/files/upload", methods=["POST"])
def files_upload():
    payload, status = file_service.upload_file(request.files.get("file"), request.form.get("path", "").strip())
    return jsonify(payload), status


@file_bp.route("/api/files/preview")
def files_preview():
    payload, status = file_service.preview_file(request.args.get("path", "").strip())
    return jsonify(payload), status


@file_bp.route("/api/files/shift_subtitle", methods=["POST"])
def files_shift_subtitle():
    data = request.json or {}
    payload, status = file_service.shift_subtitle(data.get("path", "").strip(), data.get("seconds", ""))
    return jsonify(payload), status
