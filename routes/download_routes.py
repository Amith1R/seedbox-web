from flask import Blueprint, jsonify, request

from services import download_service

download_bp = Blueprint("download_bp", __name__)


@download_bp.route("/api/download/dirs")
def dl_dirs():
    return jsonify(download_service.download_dirs())


@download_bp.route("/api/download/start", methods=["POST"])
def dl_start():
    payload, status = download_service.start_download(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/download/jobs")
def dl_jobs():
    return jsonify(download_service.list_jobs())


@download_bp.route("/api/download/job/<job_id>")
def dl_job(job_id):
    job = download_service.get_job(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found", "ok": False, "msg": "Job not found"}), 404
    return jsonify(job)


@download_bp.route("/api/download/edit/<job_id>")
def dl_edit(job_id):
    payload, status = download_service.edit_job_payload(job_id)
    return jsonify(payload), status


@download_bp.route("/api/download/output/<job_id>")
def dl_output(job_id):
    return download_service.stream_job_output(job_id)


@download_bp.route("/api/download/cancel/<job_id>", methods=["POST"])
def dl_cancel(job_id):
    payload, status = download_service.cancel_job(job_id)
    return jsonify(payload), status


@download_bp.route("/api/download/rerun/<job_id>", methods=["POST"])
def dl_rerun(job_id):
    payload, status = download_service.rerun_job(job_id)
    return jsonify(payload), status


@download_bp.route("/api/download/delete/<job_id>", methods=["POST"])
def dl_delete(job_id):
    payload, status = download_service.delete_job(job_id)
    return jsonify(payload), status


@download_bp.route("/api/download/clear", methods=["POST"])
def dl_clear():
    payload, status = download_service.clear_jobs()
    return jsonify(payload), status


@download_bp.route("/api/download/browse")
def dl_browse():
    payload, status = download_service.browse_download_path(request.args.get("path"))
    return jsonify(payload), status


@download_bp.route("/api/download/tools")
def dl_tools():
    return jsonify(download_service.tools_status())


@download_bp.route("/api/viki/config")
def viki_config():
    return jsonify(download_service.get_viki_defaults())


@download_bp.route("/api/viki/start", methods=["POST"])
def viki_start():
    payload, status = download_service.start_viki_subtitles(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/subtitles/shift/start", methods=["POST"])
def subtitle_shift_start():
    payload, status = download_service.start_subtitle_shift_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/subtitles/convert/start", methods=["POST"])
def subtitle_convert_start():
    payload, status = download_service.start_subtitle_convert_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/subtitles/match/start", methods=["POST"])
def subtitle_match_start():
    payload, status = download_service.start_subtitle_match_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/media/rename/start", methods=["POST"])
def media_rename_start():
    payload, status = download_service.start_media_rename_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/media/organize/start", methods=["POST"])
def media_organize_start():
    payload, status = download_service.start_media_organize_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/jellyfin/rescan/start", methods=["POST"])
def jellyfin_rescan_start():
    payload, status = download_service.start_jellyfin_rescan_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/storage/bulk_rename/start", methods=["POST"])
def storage_bulk_rename_start():
    payload, status = download_service.start_bulk_rename_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/storage/empty_folders/start", methods=["POST"])
def storage_empty_folders_start():
    payload, status = download_service.start_empty_folder_cleanup_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/storage/duplicates/start", methods=["POST"])
def storage_duplicates_start():
    payload, status = download_service.start_duplicate_scan_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/storage/largest_files/start", methods=["POST"])
def storage_largest_files_start():
    payload, status = download_service.start_largest_files_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/storage/folder_size/start", methods=["POST"])
def storage_folder_size_start():
    payload, status = download_service.start_folder_size_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/storage/archive/start", methods=["POST"])
def storage_archive_start():
    payload, status = download_service.start_archive_extract_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/storage/backup/start", methods=["POST"])
def storage_backup_start():
    payload, status = download_service.start_backup_folder_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/storage/orphan_subtitles/start", methods=["POST"])
def storage_orphan_subtitles_start():
    payload, status = download_service.start_orphan_subtitle_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/system/temp_cleanup/start", methods=["POST"])
def system_temp_cleanup_start():
    payload, status = download_service.start_temp_cleanup_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/system/log_cleanup/start", methods=["POST"])
def system_log_cleanup_start():
    payload, status = download_service.start_log_cleanup_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/system/report/start", methods=["POST"])
def system_report_start():
    payload, status = download_service.start_system_report_job(request.json or {})
    return jsonify(payload), status


@download_bp.route("/api/system/disk_snapshot/start", methods=["POST"])
def system_disk_snapshot_start():
    payload, status = download_service.start_disk_snapshot_job(request.json or {})
    return jsonify(payload), status
