from flask import Blueprint, jsonify, render_template, request

from services import docker_service, power_service, system_service

status_bp = Blueprint("status_bp", __name__)


@status_bp.route("/")
def index():
    return render_template("index.html")


@status_bp.route("/api/status")
def api_status():
    return jsonify(system_service.build_status_snapshot())


@status_bp.route("/api/status/stream")
def api_status_stream():
    return system_service.status_stream_response()


@status_bp.route("/api/electricity/config", methods=["GET", "POST"])
def api_electricity_config():
    if request.method == "GET":
        return jsonify(power_service.get_config())
    payload, status = power_service.update_config(request.json or {})
    return jsonify(payload), status


@status_bp.route("/api/electricity/reset", methods=["POST"])
def api_electricity_reset():
    payload, status = power_service.reset_tracking()
    return jsonify(payload), status


@status_bp.route("/api/electricity/data")
def api_electricity_data():
    cpu_usage = request.args.get("cpu")
    live_watts = request.args.get("live_watts")
    try:
        cpu_usage = float(cpu_usage) if cpu_usage is not None else 0.0
    except Exception:
        cpu_usage = 0.0
    try:
        live_watts = float(live_watts) if live_watts is not None else None
    except Exception:
        live_watts = None
    return jsonify(power_service.get_electricity_payload(cpu_usage=cpu_usage, live_watts=live_watts))


@status_bp.route("/api/processes")
def api_processes():
    return jsonify(system_service.get_processes())


@status_bp.route("/api/kill_process", methods=["POST"])
def api_kill_process():
    data = request.json or {}
    payload, status = system_service.kill_process(data.get("pid", ""), data.get("signal", "TERM"))
    return jsonify(payload), status


@status_bp.route("/api/run_command", methods=["POST"])
def api_run_command():
    data = request.json or {}
    return system_service.run_command_response(data.get("cmd", ""))


@status_bp.route("/api/neko/status")
def api_neko_status():
    return jsonify(docker_service.build_neko_status())
