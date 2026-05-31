#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Report generation and viewing service for Seedbox."""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

import markdown
from flask import jsonify, Response


class ReportService:
    """Service for generating and managing system reports."""

    def __init__(self, base_path: str = None):
        """Initialize report service.
        
        Args:
            base_path: Base directory for storing reports
        """
        self.base_path = Path(base_path or "/tmp/seedbox_reports")
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _sanitize_filename(self, name: str) -> str:
        """Sanitize filename."""
        import re
        return re.sub(r'[^a-zA-Z0-9._-]', '_', name)

    def generate_report(self, report_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a system report.
        
        Args:
            report_type: Type of report (system, disk, etc.)
            data: Report data
            
        Returns:
            Dictionary with report metadata and content
        """
        try:
            timestamp = datetime.now().isoformat()
            report_id = f"{report_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            
            content = self._generate_report_content(report_type, data)
            html_content = self._convert_to_html(content)
            
            # Store report files
            md_file = self.base_path / f"{report_id}.md"
            html_file = self.base_path / f"{report_id}.html"
            json_file = self.base_path / f"{report_id}.json"
            
            md_file.write_text(content, encoding='utf-8')
            html_file.write_text(html_content, encoding='utf-8')
            json_file.write_text(json.dumps(data), encoding='utf-8')
            
            return {
                "ok": True,
                "report_id": report_id,
                "report_type": report_type,
                "timestamp": timestamp,
                "md_file": str(md_file),
                "html_file": str(html_file),
                "json_file": str(json_file),
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "msg": str(exc),
            }

    def get_report(self, report_id: str, format: str = "html") -> Optional[str]:
        """Retrieve a report by ID.
        
        Args:
            report_id: Report identifier
            format: Output format (html, md, json)
            
        Returns:
            Report content or None if not found
        """
        ext = {
            "html": ".html",
            "markdown": ".md",
            "md": ".md",
            "json": ".json",
        }.get(format.lower(), ".html")
        
        report_file = self.base_path / f"{report_id}{ext}"
        
        if not report_file.exists():
            return None
            
        return report_file.read_text(encoding='utf-8')

    def list_reports(self) -> list:
        """List all available reports."""
        reports = []
        for md_file in sorted(self.base_path.glob("*.md"), reverse=True):
            report_id = md_file.stem
            json_file = self.base_path / f"{report_id}.json"
            
            try:
                stat = md_file.stat()
                reports.append({
                    "report_id": report_id,
                    "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "size": stat.st_size,
                    "has_html": (self.base_path / f"{report_id}.html").exists(),
                })
            except Exception:
                pass
        
        return reports

    def delete_report(self, report_id: str) -> Dict[str, Any]:
        """Delete a report."""
        try:
            for ext in ['.md', '.html', '.json']:
                report_file = self.base_path / f"{report_id}{ext}"
                if report_file.exists():
                    report_file.unlink()
            return {"ok": True, "msg": "Report deleted"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _generate_report_content(report_type: str, data: Dict[str, Any]) -> str:
        """Generate markdown report content."""
        lines = [
            f"# System Report - {report_type.title()}",
            f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        
        if report_type == "system":
            lines.extend(ReportService._format_system_report(data))
        elif report_type == "disk":
            lines.extend(ReportService._format_disk_report(data))
        else:
            lines.append(f"```json\n{json.dumps(data, indent=2)}\n```")
        
        return "\n".join(lines)

    @staticmethod
    def _format_system_report(data: Dict[str, Any]) -> list:
        """Format system report lines."""
        lines = ["## System Status", ""]
        
        if "hostname" in data:
            lines.append(f"- **Hostname:** `{data.get('hostname', 'N/A')}`")
        if "kernel" in data:
            lines.append(f"- **Kernel:** `{data.get('kernel', 'N/A')}`")
        if "uptime" in data:
            lines.append(f"- **Uptime:** {data.get('uptime', 'N/A')}")
        
        lines.append("\n## Resources")
        
        if "cpu" in data:
            lines.append(f"- **CPU Usage:** {data.get('cpu', 0)}%")
        if "ram" in data:
            ram = data.get('ram', {})
            lines.append(f"- **Memory:** {ram.get('used', 0)}MB / {ram.get('total', 0)}MB ({ram.get('pct', 0)}%)")
        if "disk" in data:
            disk = data.get('disk', {})
            lines.append(f"- **Disk:** {disk.get('used', 'N/A')} / {disk.get('size', 'N/A')} ({disk.get('pct', 'N/A')})")
        
        return lines

    @staticmethod
    def _format_disk_report(data: Dict[str, Any]) -> list:
        """Format disk report lines."""
        lines = ["## Disk Analysis", ""]
        
        if isinstance(data, dict):
            for key, value in data.items():
                lines.append(f"- **{key.title()}:** {value}")
        
        return lines

    @staticmethod
    def _convert_to_html(markdown_content: str) -> str:
        """Convert markdown to HTML with styling."""
        html_body = markdown.markdown(markdown_content, extensions=['tables', 'fenced_code'])
        
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Seedbox Report</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Oxygen', 'Ubuntu', sans-serif;
            background: linear-gradient(135deg, #1b2128 0%, #232a31 100%);
            color: #edf2f7;
            line-height: 1.6;
            padding: 2rem;
        }}
        .container {{
            max-width: 1000px;
            margin: 0 auto;
            background: #222930;
            border: 1px solid #36414b;
            border-radius: 14px;
            padding: 2rem;
            box-shadow: 0 18px 40px rgba(0,0,0,.22);
        }}
        h1 {{
            color: #1ea7d7;
            margin-bottom: 1rem;
            border-bottom: 2px solid #1ea7d7;
            padding-bottom: 0.5rem;
        }}
        h2 {{
            color: #27c2c7;
            margin-top: 1.5rem;
            margin-bottom: 0.75rem;
        }}
        p, li {{
            margin-bottom: 0.5rem;
            color: #b8c3ce;
        }}
        ul, ol {{
            margin-left: 1.5rem;
            margin-bottom: 1rem;
        }}
        code {{
            background: #1b2128;
            padding: 0.2rem 0.4rem;
            border-radius: 4px;
            font-family: 'JetBrains Mono', monospace;
            color: #22c55e;
        }}
        pre {{
            background: #1b2128;
            padding: 1rem;
            border-radius: 8px;
            overflow-x: auto;
            margin: 1rem 0;
            border-left: 3px solid #22c55e;
        }}
        pre code {{
            background: transparent;
            padding: 0;
            color: #22c55e;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 1rem 0;
        }}
        th, td {{
            padding: 0.75rem;
            text-align: left;
            border-bottom: 1px solid #36414b;
        }}
        th {{
            background: #2b333c;
            color: #1ea7d7;
            font-weight: 600;
        }}
        tr:hover {{
            background: #2b333c;
        }}
        .timestamp {{
            color: #7b8b9b;
            font-size: 0.9rem;
            margin-bottom: 1rem;
        }}
    </style>
</head>
<body>
    <div class="container">
        {html_body}
        <div class="timestamp" style="margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #36414b;">
            Generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        </div>
    </div>
</body>
</html>"""
        
        return html
