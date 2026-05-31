#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Report generation and viewing routes."""

from flask import Blueprint, jsonify, request, render_template_string
from services import download_service, system_service
from services.report_service import ReportService

reports_bp = Blueprint("reports_bp", __name__)
report_service = ReportService()


@reports_bp.route("/api/report/system", methods=["GET"])
def api_report_system():
    """Generate a system report."""
    try:
        status = system_service.build_status_snapshot()
        result = report_service.generate_report("system", status)
        return jsonify(result), 200 if result.get("ok") else 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "msg": str(exc)}), 500


@reports_bp.route("/api/report/<report_id>", methods=["GET"])
def api_get_report(report_id):
    """Retrieve a report by ID."""
    report_format = request.args.get("format", "html")
    content = report_service.get_report(report_id, report_format)
    
    if not content:
        return jsonify({"ok": False, "error": "Report not found"}), 404
    
    if report_format.lower() in ["html"]:
        return content, 200, {"Content-Type": "text/html; charset=utf-8"}
    elif report_format.lower() in ["md", "markdown"]:
        return content, 200, {"Content-Type": "text/markdown; charset=utf-8"}
    elif report_format.lower() == "json":
        return content, 200, {"Content-Type": "application/json"}
    
    return content, 200, {"Content-Type": "text/plain; charset=utf-8"}


@reports_bp.route("/api/report/list", methods=["GET"])
def api_list_reports():
    """List all available reports."""
    try:
        reports = report_service.list_reports()
        return jsonify({"ok": True, "reports": reports}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@reports_bp.route("/api/report/<report_id>", methods=["DELETE"])
def api_delete_report(report_id):
    """Delete a report."""
    result = report_service.delete_report(report_id)
    return jsonify(result), 200 if result.get("ok") else 500


@reports_bp.route("/view/report/<report_id>", methods=["GET"])
def view_report(report_id):
    """Display report in browser."""
    content = report_service.get_report(report_id, "html")
    if not content:
        return "<h1>Report not found</h1>", 404, {"Content-Type": "text/html"}
    return content, 200, {"Content-Type": "text/html; charset=utf-8"}
