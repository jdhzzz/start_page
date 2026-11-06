"""Hacker News by day: the stories behind the "Top 10 / Top 20 / Top 50%" view.

Uses Hacker News's public search API (Algolia), no key needed. For a time range it
returns the stories that reached a minimum score, sorted by points. The page does
the Top N / Top 50% cut and the per-day grouping.
"""
import threading
import time

import requests
from flask import Blueprint, jsonify, request

bp = Blueprint("hn", __name__)

API = "https://hn.algolia.com/api/v1/search"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
MAX_RANGE = 26 * 3600     # a day, allowing for daylight-saving changes

_cache = {}
_lock = threading.Lock()


@bp.get("/api/hn")
def api_hn():
    try:
        start = int(request.args["start"])
        end = int(request.args["end"])
        min_pts = max(0, min(int(request.args.get("min", 20)), 500))
    except (KeyError, ValueError):
        return jsonify(error="Bad request"), 400
    if end <= start or end - start > MAX_RANGE:
        return jsonify(error="Bad time range"), 400

    key = (start, end, min_pts)
    ttl = 300 if end > time.time() else 3600   # finished days barely change
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return jsonify(stories=hit[1])
    try:
        r = requests.get(API, params={
            "tags": "story",
            "numericFilters": f"created_at_i>={start},created_at_i<{end},points>={min_pts}",
            "hitsPerPage": 1000,
        }, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        hits = r.json().get("hits", [])
    except (requests.RequestException, ValueError) as exc:
        return jsonify(error=f"Hacker News search failed: {exc}"), 502

    stories = [{
        "id": h.get("objectID"),
        "title": h.get("title") or "(untitled)",
        "url": h.get("url") or "",
        "points": h.get("points") or 0,
        "comments": h.get("num_comments") or 0,
        "time": h.get("created_at_i"),
    } for h in hits]
    stories.sort(key=lambda s: -s["points"])
    with _lock:
        _cache[key] = (time.time(), stories)
    return jsonify(stories=stories)
