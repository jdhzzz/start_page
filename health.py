"""Overall health light, fed by an Uptime Kuma status page.

Set KUMA_URL (e.g. http://192.168.5.25:3001) and optionally KUMA_SLUG (the
status page's slug, default "home"). KUMA_LINK overrides where the light links
to (default: KUMA_URL/status/KUMA_SLUG). Without KUMA_URL the light is hidden.

KUMA_LINK_TAILNET, if set, is the link given to devices that reach the page over
Tailscale, where LAN names and addresses don't work. "{host}" in it becomes the
name or address the device used for the start page, e.g.
http://{host}:3001/status/home with deploy/kuma-tailnet.socket relaying that port.

This is a fixed, admin-configured URL (not user input), so it may point at the
LAN, unlike the feed widget's fetches.
"""
import ipaddress
import os
import threading
import time

import requests
from flask import Blueprint, jsonify, request

bp = Blueprint("health", __name__)

TTL = 30  # seconds
DOWN, UP, PENDING, MAINTENANCE = 0, 1, 2, 3

_cache = {"at": 0.0, "value": None}
_lock = threading.Lock()


def _config():
    base = os.environ.get("KUMA_URL", "").strip().rstrip("/")
    slug = os.environ.get("KUMA_SLUG", "home").strip() or "home"
    link = os.environ.get("KUMA_LINK", "").strip() or (f"{base}/status/{slug}" if base else "")
    return base, slug, link


TAILNETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def _link_for_client(link):
    """The Kuma link this client can open: the Tailscale one for Tailscale devices."""
    remote = os.environ.get("KUMA_LINK_TAILNET", "").strip()
    if not remote:
        return link
    try:
        ip = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return link
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if not any(ip in net for net in TAILNETS):
        return link
    host = request.host.rsplit(":", 1)[0] if not request.host.endswith("]") else request.host
    return remote.replace("{host}", host)


def summarise(page, beats):
    """Turn Uptime Kuma's status-page JSON into one traffic-light state."""
    names = {}
    for group in page.get("publicGroupList") or []:
        for m in group.get("monitorList") or []:
            names[str(m.get("id"))] = m.get("name") or f"Monitor {m.get('id')}"
    counts = {"up": 0, "down": 0, "pending": 0, "maintenance": 0}
    problems = []
    for mid, name in names.items():
        hist = (beats.get("heartbeatList") or {}).get(mid) or []
        status = hist[-1].get("status") if hist else PENDING
        if status == UP:
            counts["up"] += 1
        elif status == DOWN:
            counts["down"] += 1
            problems.append(f"{name}: down")
        elif status == MAINTENANCE:
            counts["maintenance"] += 1
        else:
            counts["pending"] += 1
            problems.append(f"{name}: {'no data yet' if not hist else 'pending'}")
    if counts["down"]:
        state = "down"
    elif counts["pending"]:
        state = "warn"
    elif names:
        state = "ok"
    else:
        state = "warn"
        problems.append("The status page has no monitors on it")
    return {"state": state, "total": len(names), "problems": problems, **counts}


def fetch(base, slug):
    s = requests.Session()
    page = s.get(f"{base}/api/status-page/{slug}", timeout=5)
    page.raise_for_status()
    beats = s.get(f"{base}/api/status-page/heartbeat/{slug}", timeout=5)
    beats.raise_for_status()
    return summarise(page.json(), beats.json())


@bp.get("/api/health")
def api_health():
    base, slug, link = _config()
    if not base:
        return jsonify(state="off")
    now = time.time()
    with _lock:
        if _cache["value"] and now - _cache["at"] < TTL:
            return jsonify(**_cache["value"], link=_link_for_client(link))
    try:
        value = fetch(base, slug)
    except (requests.RequestException, ValueError):
        # If monitoring itself is unreachable, that's a problem worth showing.
        value = {"state": "down", "problems": ["Can't reach Uptime Kuma"], "total": 0}
    with _lock:
        _cache.update(at=now, value=value)
    return jsonify(**value, link=_link_for_client(link))
