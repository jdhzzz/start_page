"""Weather widget backend: reads your station from the Ambient Weather Network API.

Keys (free, from https://ambientweather.net/account, "API Keys"):
  an API Key and an Application Key. Provide them either as environment variables
  AMBIENT_API_KEY and AMBIENT_APP_KEY, or in data/ambient.json:
      {"api_key": "...", "app_key": "..."}
Keep them private; anyone with both keys can read your station data.
"""
import json
import os
import threading
import time
from pathlib import Path

import requests
from flask import Blueprint, jsonify

bp = Blueprint("weather", __name__)

API = "https://api.ambientweather.net/v1/devices"
KEY_FILE = Path(__file__).parent / "data" / "ambient.json"
CACHE_TTL = 60   # the API allows about one request per second; stations report every 1-5 min

_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()

# lastData field -> our name
FIELDS = {
    "tempf": "temp", "feelsLike": "feels", "humidity": "humidity", "dewPoint": "dew",
    "windspeedmph": "wind", "windgustmph": "gust", "winddir": "winddir",
    "baromrelin": "pressure", "dailyrainin": "rain_today", "hourlyrainin": "rain_hour",
    "uv": "uv", "solarradiation": "solar", "tempinf": "temp_in", "humidityin": "humidity_in",
}


def load_keys():
    api_key = os.environ.get("AMBIENT_API_KEY", "").strip()
    app_key = os.environ.get("AMBIENT_APP_KEY", "").strip()
    if (not api_key or not app_key) and KEY_FILE.exists():
        try:
            cfg = json.loads(KEY_FILE.read_text("utf-8"))
            api_key = api_key or str(cfg.get("api_key", "")).strip()
            app_key = app_key or str(cfg.get("app_key", "")).strip()
        except (json.JSONDecodeError, OSError):
            pass
    return api_key, app_key


def fetch():
    api_key, app_key = load_keys()
    if not api_key or not app_key:
        return None, ("not_configured", 503,
                      "Add your Ambient API key and application key (see README), then restart the app.")
    try:
        r = requests.get(API, params={"apiKey": api_key, "applicationKey": app_key}, timeout=10)
    except requests.RequestException:
        return None, ("unreachable", 502, "Couldn’t reach ambientweather.net.")
    if r.status_code in (401, 403):
        return None, ("bad_keys", 502, "Ambient rejected the keys. Check both values.")
    if r.status_code == 429:
        return None, ("rate_limited", 502, "Ambient is rate limiting requests; try again shortly.")
    if not r.ok:
        return None, ("http_error", 502, f"Ambient returned HTTP {r.status_code}.")
    devices = r.json()
    if not devices:
        return None, ("no_devices", 404, "No weather stations found on this account.")
    dev = devices[0]
    last = dev.get("lastData") or {}
    out = {"name": (dev.get("info") or {}).get("name") or "My station",
           "updated_ms": last.get("dateutc")}
    for src, dst in FIELDS.items():
        if last.get(src) is not None:
            out[dst] = last[src]
    return out, None


@bp.get("/api/weather")
def api_weather():
    with _lock:
        if _cache["data"] is not None and time.time() - _cache["at"] < CACHE_TTL:
            return jsonify(_cache["data"])
        data, err = fetch()
        if err:
            code, status, msg = err
            return jsonify(error=msg, code=code), status
        _cache.update(at=time.time(), data=data)
        return jsonify(data)
