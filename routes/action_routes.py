#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Action routes for system commands and service management."""

from flask import Blueprint, jsonify, request
from services import system_service, power_service

action_bp = Blueprint("action_bp", __name__)


@action_bp.route("/api/system/reboot", methods=["POST"])
def api_reboot():
    """Reboot the system."""
    payload, status = power_service.system_reboot()
    return jsonify(payload), status


@action_bp.route("/api/system/shutdown", methods=["POST"])
def api_shutdown():
    """Shutdown the system."""
    payload, status = power_service.system_shutdown()
    return jsonify(payload), status
