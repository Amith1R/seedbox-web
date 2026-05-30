#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Seedbox Web Dashboard - modular Flask entrypoint."""

from flask import Flask, request
from werkzeug.exceptions import HTTPException

from routes.action_routes import action_bp
from routes.download_routes import download_bp
from routes.file_routes import file_bp
from routes.logs_routes import logs_bp
from routes.status_routes import status_bp
from services import docker_service, download_service, power_service, system_service


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB upload ceiling

    app.register_blueprint(status_bp)
    app.register_blueprint(action_bp)
    app.register_blueprint(download_bp)
    app.register_blueprint(file_bp)
    app.register_blueprint(logs_bp)

    @app.errorhandler(Exception)
    def handle_error(exc):
        if isinstance(exc, HTTPException):
            status = exc.code or 500
            message = exc.description
        else:
            status = 500
            message = str(exc) or "Internal server error"

        wants_sse = "text/event-stream" in (request.headers.get("Accept") or "")
        if request.path.startswith("/api") and not wants_sse:
            return system_service.json_error(message, status)
        return message, status

    download_service.init_download_store()
    power_service.init_power_tracking()
    docker_service.ensure_neko_created()
    return app


app = create_app()


if __name__ == "__main__":
    app.run(host=system_service.APP_HOST, port=system_service.APP_PORT, debug=False, threaded=True)
