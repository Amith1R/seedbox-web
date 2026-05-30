import json

from flask import Blueprint, Response, jsonify, request, stream_with_context

from services import docker_service, system_service

logs_bp = Blueprint("logs_bp", __name__)


@logs_bp.route("/api/logs/<service>")
def api_logs(service):
    command = docker_service.get_log_command(service)
    if command is None and service != "system":
        return jsonify({"success": False, "error": "Unknown service", "ok": False, "msg": "Unknown service"}), 400
    if service != "system" and not command:
        return Response(
            stream_with_context(iter(["data: {}\n\n".format(json.dumps(system_service.PRIV_HINT)), 'data: "__DONE__"\n\n'])),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    filter_text = request.args.get("filter", "").strip() or None
    search_text = request.args.get("search", "").strip() or None
    if service == "system":
        return Response(
            stream_with_context(system_service.run_stream(command, filter_text=filter_text, search_text=search_text)),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    cmd = system_service._privileged_cmd(*command)
    if not cmd:
        return Response(
            stream_with_context(iter(["data: {}\n\n".format(json.dumps(system_service.PRIV_HINT)), 'data: "__DONE__"\n\n'])),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return Response(
        stream_with_context(system_service.run_stream(cmd, filter_text=filter_text, search_text=search_text)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
