from flask import Blueprint, jsonify, request

from services import docker_service

action_bp = Blueprint("action_bp", __name__)


@action_bp.route("/api/action", methods=["POST"])
def api_action():
    payload, status = docker_service.handle_action(request.json)
    return jsonify(payload), status
