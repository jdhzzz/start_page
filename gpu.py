"""GPU rental prices: what an hour of an AI chip costs to rent.

Read like a credit spread, but upside down: the worrying move is prices FALLING,
which means demand is weakening or supply has overshot, and the GPU fleets being
bought with borrowed money earn less than planned.

Two free sources, neither needs a key:
  - Ornn's daily settled index (three months of history), for the ribbon tile.
  - GetDeploying's weekly median on-demand list price across about 40 providers
    (a year of history, CC BY 4.0), for the widget's chart.
The two measure differently (traded rates vs list prices), so their levels differ.
"""
import csv
import io
import time
from urllib.parse import quote

import pandas as pd
import requests
from flask import Blueprint, jsonify

bp = Blueprint("gpu", __name__)

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
ORNN_URL = "https://data.ornn.com/api/public-index/gpu/{}/index-history"
ORNN_PAGE = "https://data.ornn.com/markets/h100-sxm"
WEEKLY_URL = "https://getdeploying.com/dataset/gpu-prices/weekly.csv"
WEEKLY_PAGE = "https://getdeploying.com/gpu-price-trends"

# Chips on the widget's chart, in a fixed order (the order sets each one's color).
CHIPS = [
    {"key": "h100", "name": "H100", "slug": "nvidia-h100"},
    {"key": "h200", "name": "H200", "slug": "nvidia-h200"},
    {"key": "b200", "name": "B200", "slug": "nvidia-b200"},
]

ORNN_TTL = 3 * 3600      # the index settles once a day
WEEKLY_TTL = 6 * 3600    # the file changes once a week
WEEKS = 53               # a year of weekly points
SMOOTH_DAYS = 7          # the daily index swings 10% day to day, so drops are judged on a 7-day average
WARN_DROP = 10.0         # % below the high that turns a flag amber
ALERT_DROP = 20.0        # % below the high that turns it red

_last = {}   # key -> (fetched_at, value): reused while fresh, and kept as a fallback if a fetch fails


def _keep(key, ttl, fn):
    hit = _last.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        value = fn()
    except Exception:
        if hit:
            return hit[1]
        raise
    _last[key] = (time.time(), value)
    return value


def flag_for(off_high):
    """off_high is a percentage at or below zero: how far under the high."""
    if off_high <= -ALERT_DROP:
        return "alert"
    if off_high <= -WARN_DROP:
        return "warn"
    return None


# ---------- Ornn daily index (ribbon tile) ----------

def _fetch_ornn(gpu_type):
    r = requests.get(ORNN_URL.format(quote(gpu_type)), headers={"User-Agent": UA}, timeout=12)
    r.raise_for_status()
    rows = r.json().get("data") or []
    values = [float(x["index_value"]) for x in rows if x.get("index_value") is not None]
    if len(values) < SMOOTH_DAYS + 1:
        raise ValueError("Ornn returned no recent values")
    return pd.Series(values)


def ornn_daily(gpu_type):
    return _keep(f"ornn:{gpu_type}", ORNN_TTL, lambda: _fetch_ornn(gpu_type))


def off_high(s):
    """How far the 7-day average sits below its highest level in the series, in %."""
    smooth = s.rolling(SMOOTH_DAYS, min_periods=SMOOTH_DAYS).mean().dropna()
    return round((float(smooth.iloc[-1]) / float(smooth.max()) - 1) * 100, 1)


# ---------- weekly list prices (widget) ----------

def _fetch_weekly():
    r = requests.get(WEEKLY_URL, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    want = {c["slug"]: [] for c in CHIPS}
    for row in csv.DictReader(io.StringIO(r.text)):
        if row.get("billing_type") != "ON_DEMAND" or row.get("gpu_slug") not in want:
            continue
        try:
            want[row["gpu_slug"]].append((row["date"], float(row["median_price"]), int(row["provider_count"])))
        except (KeyError, ValueError):
            continue
    chips = []
    for c in CHIPS:
        rows = sorted(want[c["slug"]])[-WEEKS:]
        if len(rows) < 8:
            continue
        prices = [p for _, p, _ in rows]
        latest = prices[-1]

        def change(weeks_back):
            if len(prices) <= weeks_back:
                return None
            return round((latest / prices[-1 - weeks_back] - 1) * 100, 1)

        below = round((latest / max(prices) - 1) * 100, 1)
        chips.append({
            "key": c["key"], "name": c["name"],
            "points": [[d, round(p, 3)] for d, p, _ in rows],
            "latest": round(latest, 2), "chg4w": change(4), "chg1y": change(len(prices) - 1),
            "off_high": below, "flag": flag_for(below), "providers": rows[-1][2],
        })
    if not chips:
        raise ValueError("The weekly price file had no rows for these chips")
    return {"chips": chips, "week": max(c["points"][-1][0] for c in chips),
            "source": "GetDeploying", "source_link": WEEKLY_PAGE, "license": "CC BY 4.0"}


@bp.get("/api/gpu")
def api_gpu():
    try:
        return jsonify(_keep("weekly", WEEKLY_TTL, _fetch_weekly))
    except requests.HTTPError as exc:
        return jsonify(error=f"The price source returned HTTP {exc.response.status_code}"), 502
    except ValueError as exc:
        return jsonify(error=str(exc)), 502
    except Exception:
        return jsonify(error="Couldn't fetch GPU rental prices"), 502
