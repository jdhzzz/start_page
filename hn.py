"""Hacker News by day: the stories behind the "Top 10 / Top 20 / Top 50%" view.

Uses Hacker News's public search API (Algolia), no key needed. For a time range it
returns the stories that reached a minimum score, sorted by points. The page does
the Top N / Top 50% cut and the per-day grouping.
"""
import time

import requests
from flask import Blueprint, jsonify, request

from cache import cached

bp = Blueprint("hn", __name__)

API = "https://hn.algolia.com/api/v1/search"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
MAX_RANGE = 26 * 3600     # a day, allowing for daylight-saving changes


def fetch(start, end, min_pts):
    r = requests.get(API, params={
        "tags": "story",
        "numericFilters": f"created_at_i>={start},created_at_i<{end},points>={min_pts}",
        "hitsPerPage": 1000,
    }, headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    stories = [{
        "id": h.get("objectID"),
        "title": h.get("title") or "(untitled)",
        "url": h.get("url") or "",
        "points": h.get("points") or 0,
        "comments": h.get("num_comments") or 0,
        "time": h.get("created_at_i"),
    } for h in r.json().get("hits", [])]
    stories.sort(key=lambda s: -s["points"])
    return stories


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

    ttl = 300 if end > time.time() else 3600   # finished days barely change
    try:
        stories = cached(("hn", start, end, min_pts), ttl, lambda: fetch(start, end, min_pts))
    except (requests.RequestException, ValueError) as exc:
        return jsonify(error=f"Hacker News search failed: {exc}"), 502
    return jsonify(stories=stories)
