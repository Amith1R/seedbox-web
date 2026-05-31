#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Enhanced system monitoring and information service."""

import os
import json
import psutil
import socket
import subprocess
from pathlib import Path
from datetime import datetime
import logging

log = logging.getLogger("seedbox.enhanced_system")


class EnhancedSystemService:
    """Comprehensive system monitoring and information service."""

    @staticmethod
    def get_system_info():
        """Get detailed system information."""
        try:
            boot_time = datetime.fromtimestamp(psutil.boot_time())
            return {
                "hostname": socket.gethostname(),
                "kernel": os.popen("uname -r").read().strip(),
                "os": os.popen("lsb_release -ds").read().strip() or "Linux",
                "arch": os.popen("uname -m").read().strip(),
                "boot_time": boot_time.isoformat(),
                "uptime_seconds": int(datetime.now().timestamp() - psutil.boot_time()),
            }
        except Exception as exc:
            log.warning("Could not fetch system info: %s", exc)
            return {}

    @staticmethod
    def get_cpu_info():
        """Get detailed CPU information."""
        try:
            return {
                "physical_cores": psutil.cpu_count(logical=False),
                "logical_cores": psutil.cpu_count(logical=True),
                "frequency_current": psutil.cpu_freq().current,
                "frequency_max": psutil.cpu_freq().max,
                "usage_percent": psutil.cpu_percent(interval=1),
                "usage_per_core": psutil.cpu_percent(interval=1, percpu=True),
                "load_average": os.getloadavg(),
            }
        except Exception as exc:
            log.warning("Could not fetch CPU info: %s", exc)
            return {}

    @staticmethod
    def get_memory_info():
        """Get detailed memory information."""
        try:
            virtual = psutil.virtual_memory()
            swap = psutil.swap_memory()
            return {
                "total": virtual.total,
                "available": virtual.available,
                "used": virtual.used,
                "free": virtual.free,
                "percent": virtual.percent,
                "swap_total": swap.total,
                "swap_used": swap.used,
                "swap_free": swap.free,
                "swap_percent": swap.percent,
            }
        except Exception as exc:
            log.warning("Could not fetch memory info: %s", exc)
            return {}

    @staticmethod
    def get_disk_info():
        """Get detailed disk information."""
        try:
            disks = {}
            for partition in psutil.disk_partitions(all=True):\n                if partition.fstype:\n                    try:\n                        usage = psutil.disk_usage(partition.mountpoint)\n                        disks[partition.mountpoint] = {\n                            "device": partition.device,\n                            "fstype": partition.fstype,\n                            "total": usage.total,\n                            "used": usage.used,\n                            "free": usage.free,\n                            "percent": usage.percent,\n                        }\n                    except (OSError, PermissionError):\n                        pass\n            return disks\n        except Exception as exc:\n            log.warning("Could not fetch disk info: %s", exc)\n            return {}\n\n    @staticmethod\n    def get_network_info():\n        """Get detailed network information.\"\"\"\n        try:\n            interfaces = {}\n            for name, addrs in psutil.net_if_addrs().items():\n                interfaces[name] = []\n                for addr in addrs:\n                    interfaces[name].append({\n                        "family": addr.family.name if hasattr(addr.family, 'name') else str(addr.family),\n                        "address": addr.address,\n                        "netmask": addr.netmask,\n                        "broadcast": addr.broadcast,\n                    })\n            \n            stats = psutil.net_if_stats()\n            iface_stats = {}\n            for name, stat in stats.items():\n                iface_stats[name] = {\n                    \"is_up\": stat.isup,\n                    \"speed\": stat.speed,\n                    \"mtu\": stat.mtu,\n                    \"packets_sent\": stat.packets_sent,\n                    \"packets_recv\": stat.packets_recv,\n                    \"errin\": stat.errin,\n                    \"errout\": stat.errout,\n                    \"dropin\": stat.dropin,\n                    \"dropout\": stat.dropout,\n                }\n            \n            # Get overall network I/O\n            net_io = psutil.net_io_counters()\n            \n            return {\n                \"interfaces\": interfaces,\n                \"stats\": iface_stats,\n                \"io\": {\n                    \"bytes_sent\": net_io.bytes_sent,\n                    \"bytes_recv\": net_io.bytes_recv,\n                    \"packets_sent\": net_io.packets_sent,\n                    \"packets_recv\": net_io.packets_recv,\n                    \"errin\": net_io.errin,\n                    \"errout\": net_io.errout,\n                    \"dropin\": net_io.dropin,\n                    \"dropout\": net_io.dropout,\n                },\n            }\n        except Exception as exc:\n            log.warning("Could not fetch network info: %s", exc)\n            return {}\n\n    @staticmethod\n    def get_temperature_info():\n        """Get system temperature information.\"\"\"\n        try:\n            temps = {}\n            try:\n                temps = psutil.sensors_temperatures()\n            except (AttributeError, OSError):\n                pass\n            \n            return {name: [{\n                "label": str(t.label),\n                "current": t.current,\n                "high": t.high,\n                "critical": t.critical,\n            } for t in values] for name, values in temps.items()}\n        except Exception as exc:\n            log.warning("Could not fetch temperature info: %s", exc)\n            return {}\n\n    @staticmethod\n    def get_process_list(limit=20):\n        \"\"\"Get list of top processes by CPU/Memory.\"\"\"\n        try:\n            processes = []\n            for proc in sorted(psutil.process_iter(\n                ['pid', 'name', 'cpu_percent', 'memory_percent']\n            ), key=lambda p: p.info.get('memory_percent', 0), reverse=True)[:limit]:\n                try:\n                    processes.append({\n                        \"pid\": proc.info['pid'],\n                        \"name\": proc.info['name'],\n                        \"cpu_percent\": proc.info.get('cpu_percent', 0),\n                        \"memory_percent\": proc.info.get('memory_percent', 0),\n                    })\n                except (psutil.NoSuchProcess, psutil.AccessDenied):\n                    pass\n            return processes\n        except Exception as exc:\n            log.warning("Could not fetch process list: %s", exc)\n            return []\n\n    @staticmethod\n    def get_systemd_services():\n        \"\"\"Get systemd services status.\"\"\"\n        try:\n            services = {}\n            result = subprocess.run(\n                ['systemctl', 'list-units', '--all', '--output=json'],\n                capture_output=True,\n                text=True,\n                timeout=5\n            )\n            if result.returncode == 0:\n                units = json.loads(result.stdout)\n                for unit in units:\n                    if unit['type'] == 'service':\n                        services[unit['unit']] = {\n                            \"state\": unit.get('state', 'unknown'),\n                            \"active\": unit.get('active', 'unknown'),\n                            \"sub\": unit.get('sub', 'unknown'),\n                        }\n            return services\n        except Exception as exc:\n            log.warning("Could not fetch systemd services: %s", exc)\n            return {}\n\n    @staticmethod\n    def get_installed_packages_count():\n        \"\"\"Get count of installed packages.\"\"\"\n        try:\n            result = subprocess.run(\n                ['dpkg', '-l'],\n                capture_output=True,\n                text=True,\n                timeout=5\n            )\n            if result.returncode == 0:\n                return len([l for l in result.stdout.split('\\n') if l.startswith('ii')])\n        except Exception:\n            pass\n        \n        try:\n            result = subprocess.run(\n                ['rpm', '-qa'],\n                capture_output=True,\n                text=True,\n                timeout=5\n            )\n            if result.returncode == 0:\n                return len(result.stdout.strip().split('\\n'))\n        except Exception:\n            pass\n        \n        return 0\n\n    @staticmethod\n    def get_docker_stats():\n        \"\"\"Get Docker container statistics.\"\"\"\n        try:\n            result = subprocess.run(\n                ['docker', 'ps', '-a', '--format={{json .}}'],\n                capture_output=True,\n                text=True,\n                timeout=5\n            )\n            if result.returncode == 0:\n                containers = []\n                for line in result.stdout.strip().split('\\n'):\n                    if line:\n                        containers.append(json.loads(line))\n                return {\n                    \"total\": len(containers),\n                    \"running\": sum(1 for c in containers if c['State'] == 'running'),\n                    \"stopped\": sum(1 for c in containers if c['State'] != 'running'),\n                    \"containers\": containers,\n                }\n        except Exception as exc:\n            log.warning("Could not fetch Docker stats: %s", exc)\n        \n        return {\"total\": 0, \"running\": 0, \"stopped\": 0, \"containers\": []}\n\n    @classmethod\n    def get_complete_dashboard(cls):\n        \"\"\"Get complete system dashboard data.\"\"\"\n        return {\n            \"timestamp\": datetime.now().isoformat(),\n            \"system\": cls.get_system_info(),\n            \"cpu\": cls.get_cpu_info(),\n            \"memory\": cls.get_memory_info(),\n            \"disk\": cls.get_disk_info(),\n            \"network\": cls.get_network_info(),\n            \"temperature\": cls.get_temperature_info(),\n            \"processes\": cls.get_process_list(limit=15),\n            \"services\": cls.get_systemd_services(),\n            \"packages\": cls.get_installed_packages_count(),\n            \"docker\": cls.get_docker_stats(),\n        }\n\n\n# Singleton instance\nenhanced_system = EnhancedSystemService()
