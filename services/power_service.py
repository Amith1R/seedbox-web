import json
import threading
import time
from datetime import date, datetime
from pathlib import Path

from services.system_service import STATE_DIR, get_system_uptime_seconds, log

ELEC_FILE = STATE_DIR / "electricity.json"
ELEC_VERSION = 3
_elec_lock = threading.Lock()
_ticker_started = False


def _default_cfg():
    return {"idle_watt": 8.0, "max_watt": 8.0, "rate": 8.0, "currency": "Rs"}


def _seconds_since_midnight(ts=None):
    now_dt = datetime.now() if ts is None else datetime.fromtimestamp(ts)
    midnight = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((now_dt - midnight).total_seconds())


def _normalize_config(cfg):
    cfg = cfg or {}
    defaults = _default_cfg()
    watts = cfg.get("watts")
    idle = cfg.get("idle_watt", watts if watts is not None else defaults["idle_watt"])
    max_watt = cfg.get("max_watt", watts if watts is not None else defaults["max_watt"])
    try:
        idle = float(idle)
        max_watt = float(max_watt)
        rate = float(cfg.get("rate", defaults["rate"]))
    except Exception:
        idle = defaults["idle_watt"]
        max_watt = defaults["max_watt"]
        rate = defaults["rate"]
    idle = max(0.1, idle)
    max_watt = max(idle, max_watt)
    currency = str(cfg.get("currency", defaults["currency"])).strip() or defaults["currency"]
    return {
        "idle_watt": idle,
        "max_watt": max_watt,
        "rate": max(0.1, rate),
        "currency": currency,
        "watts": idle if idle == max_watt else None,
    }


def _normalize(data):
    if not isinstance(data, dict):
        data = {}
    data.setdefault("days", {})
    data["cfg"] = _normalize_config(data.get("cfg"))

    version = int(data.get("version", 1))
    if version < 2:
        normalized_days = {}
        for day, value in data.get("days", {}).items():
            try:
                normalized_days[day] = int(round(float(value) * 60))
            except Exception:
                normalized_days[day] = 0
        data["days"] = normalized_days
    for day, value in list(data["days"].items()):
        try:
            data["days"][day] = max(0, int(round(float(value))))
        except Exception:
            data["days"][day] = 0

    if "since" not in data and data["days"]:
        data["since"] = min(data["days"].keys())
    data["version"] = ELEC_VERSION
    return data


def load_data():
    try:
        return _normalize(json.loads(ELEC_FILE.read_text()))
    except Exception:
        return _normalize({"days": {}, "cfg": _default_cfg()})


def save_data(data):
    try:
        ELEC_FILE.write_text(json.dumps(data))
    except Exception as exc:
        log.warning("Could not save electricity data: %s", exc)


def _add_runtime(data, start_ts, seconds):
    seconds = int(max(0, round(seconds)))
    if seconds <= 0:
        return
    remaining = seconds
    cursor = float(start_ts)
    while remaining > 0:
        day = datetime.fromtimestamp(cursor).date().isoformat()
        next_midnight = datetime.fromtimestamp(cursor).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + 86400
        chunk = min(remaining, max(1, int(next_midnight - cursor)))
        data["days"][day] = data["days"].get(day, 0) + chunk
        remaining -= chunk
        cursor += chunk


def _bootstrap(data, now_ts, uptime_sec):
    if data.get("bootstrapped_at"):
        return False
    if data.get("days"):
        data["bootstrapped_at"] = int(now_ts)
        data["last_uptime_sec"] = float(uptime_sec)
        data["last_seen_ts"] = float(now_ts)
        return True
    boot_runtime = min(int(max(0, uptime_sec)), _seconds_since_midnight(now_ts))
    if boot_runtime > 0:
        _add_runtime(data, now_ts - boot_runtime, boot_runtime)
        data.setdefault("since", datetime.fromtimestamp(now_ts - boot_runtime).date().isoformat())
    data["bootstrapped_at"] = int(now_ts)
    data["last_uptime_sec"] = float(uptime_sec)
    data["last_seen_ts"] = float(now_ts)
    return True


def tick():
    with _elec_lock:
        data = load_data()
        now_ts = time.time()
        uptime_sec = get_system_uptime_seconds()
        changed = _bootstrap(data, now_ts, uptime_sec)

        last_seen_ts = float(data.get("last_seen_ts", now_ts))
        last_uptime_sec = float(data.get("last_uptime_sec", uptime_sec))
        elapsed_wall = max(0.0, now_ts - last_seen_ts)
        elapsed_uptime = uptime_sec - last_uptime_sec

        runtime_delta = min(elapsed_wall, elapsed_uptime) if elapsed_uptime >= 0 else min(uptime_sec, elapsed_wall)
        if runtime_delta > 0:
            _add_runtime(data, now_ts - runtime_delta, runtime_delta)
            changed = True

        if "since" not in data and data["days"]:
            data["since"] = min(data["days"].keys())
        data["last_seen_ts"] = float(now_ts)
        data["last_uptime_sec"] = float(uptime_sec)
        data["version"] = ELEC_VERSION

        if changed:
            save_data(data)


def _ticker_loop():
    while True:
        time.sleep(60)
        try:
            tick()
        except Exception as exc:
            log.warning("Elec tick error: %s", exc)


def init_power_tracking():
    global _ticker_started
    if _ticker_started:
        return
    _ticker_started = True
    threading.Thread(target=_ticker_loop, daemon=True).start()
    time.sleep(1)
    try:
        tick()
    except Exception as exc:
        log.warning("Elec tick startup error: %s", exc)


def _cost(seconds, effective_watt, rate):
    hours = seconds / 3600.0
    kwh = (effective_watt * hours) / 1000.0
    return round(kwh * rate, 2), round(kwh, 4)


def get_effective_watt(cfg, cpu_usage):
    idle = float(cfg.get("idle_watt", 8))
    max_watt = float(cfg.get("max_watt", idle))
    ratio = min(max(float(cpu_usage or 0), 0.0), 100.0) / 100.0
    return round(idle + (ratio * (max_watt - idle)), 2)


def calc_payload(data, cpu_usage=0, live_watts=None):
    cfg = _normalize_config(data.get("cfg"))
    currency = cfg["currency"]
    rate = float(cfg["rate"])
    effective_watt = get_effective_watt(cfg, cpu_usage)
    days = data.get("days", {})
    today = date.today().isoformat()
    month = today[:7]

    today_sec = int(days.get(today, 0))
    month_sec = int(sum(value for key, value in days.items() if key.startswith(month)))
    total_sec = int(sum(days.values()))

    today_cost, today_kwh = _cost(today_sec, effective_watt, rate)
    month_cost, month_kwh = _cost(month_sec, effective_watt, rate)
    total_cost, total_kwh = _cost(total_sec, effective_watt, rate)
    monthly_24x7_cost, monthly_24x7_kwh = _cost(30 * 24 * 3600, effective_watt, rate)

    daily = []
    for day in sorted(days.keys(), reverse=True)[:30]:
        day_cost, day_kwh = _cost(days[day], effective_watt, rate)
        day_seconds = int(days[day])
        hours, rem = divmod(day_seconds, 3600)
        minutes, _ = divmod(rem, 60)
        daily.append(
            {
                "date": day,
                "seconds": day_seconds,
                "hours_str": "{}h {}m".format(hours, minutes),
                "kwh": day_kwh,
                "cost": day_cost,
                "today": day == today,
            }
        )

    today_hours, rem = divmod(today_sec, 3600)
    today_minutes, _ = divmod(rem, 60)
    return {
        "today_cost": today_cost,
        "month_cost": month_cost,
        "total_cost": total_cost,
        "today_kwh": today_kwh,
        "month_kwh": month_kwh,
        "total_kwh": total_kwh,
        "monthly_24x7_cost": monthly_24x7_cost,
        "monthly_24x7_kwh": monthly_24x7_kwh,
        "today_seconds": today_sec,
        "month_seconds": month_sec,
        "today_hours": "{}h {}m".format(today_hours, today_minutes),
        "currency": currency,
        "watts": effective_watt,
        "idle_watt": cfg["idle_watt"],
        "max_watt": cfg["max_watt"],
        "effective_watt": effective_watt,
        "rate": rate,
        "since": data.get("since", today),
        "daily": daily,
        "live_watts": live_watts,
    }


def get_electricity_payload(cpu_usage=0, live_watts=None):
    with _elec_lock:
        data = load_data()
    return calc_payload(data, cpu_usage=cpu_usage, live_watts=live_watts)


def get_config():
    with _elec_lock:
        data = load_data()
    return data.get("cfg", _default_cfg())


def update_config(body):
    cfg = body or {}
    watts = cfg.get("watts")
    idle_watt = cfg.get("idle_watt", watts)
    max_watt = cfg.get("max_watt", watts if watts is not None else idle_watt)
    rate = cfg.get("rate")
    currency = (cfg.get("currency", "Rs") or "Rs").strip()
    if idle_watt is None:
        idle_watt = max_watt
    if max_watt is None:
        max_watt = idle_watt
    if idle_watt is None or max_watt is None or rate is None:
        return {"ok": False, "msg": "watts/rate or idle_watt/max_watt/rate required"}, 400
    try:
        idle_watt = float(idle_watt)
        max_watt = float(max_watt)
        rate = float(rate)
        if idle_watt <= 0 or max_watt <= 0 or rate <= 0:
            raise ValueError("must be positive")
        if max_watt < idle_watt:
            max_watt = idle_watt
    except Exception:
        return {"ok": False, "msg": "Invalid values"}, 400

    with _elec_lock:
        data = load_data()
        data["cfg"] = _normalize_config(
            {"idle_watt": idle_watt, "max_watt": max_watt, "rate": rate, "currency": currency}
        )
        save_data(data)
    return {"ok": True, "msg": "Settings saved"}, 200


def reset_tracking():
    with _elec_lock:
        data = load_data()
        cfg = data.get("cfg", _default_cfg())
        now_ts = time.time()
        save_data(
            _normalize(
                {
                    "days": {},
                    "cfg": cfg,
                    "version": ELEC_VERSION,
                    "bootstrapped_at": int(now_ts),
                    "last_seen_ts": float(now_ts),
                    "last_uptime_sec": float(get_system_uptime_seconds()),
                }
            )
        )
    return {"ok": True, "msg": "Electricity tracking reset"}, 200
