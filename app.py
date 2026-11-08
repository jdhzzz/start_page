"""Personal start page: a widget portal with RSS and stock widgets.

Run:  python app.py   then open http://localhost:5055
"""
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
import yfinance as yf
from flask import Flask, jsonify, request, send_from_directory

import netguard
from ribbon import bp as ribbon_bp
from weather import bp as weather_bp
from hn import bp as hn_bp

BASE = Path(__file__).parent
STATIC = BASE / "static"
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
LAYOUT_FILE = DATA / "layout.json"

#UA = "Mozilla/5.0 (compatible; StartPage/1.0)"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-^=]{1,15}$")

FEED_TTL = 300   # seconds a fetched feed is reused
QUOTE_TTL = 60   # seconds a fetched quote is reused

CACHE_MAX = 500  # entries; stops a client filling memory with unique feed URLs

app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024  # layout POSTs are a few KB
netguard.install(app)
app.register_blueprint(ribbon_bp)
app.register_blueprint(weather_bp)
app.register_blueprint(hn_bp)

_cache = {}
_lock = threading.Lock()
_layout_lock = threading.Lock()


def cached(key, ttl, fn):
    """Return a cached value for key, or compute it with fn(). Errors are not cached."""
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _lock:
        if len(_cache) >= CACHE_MAX:
            for k in [k for k, (t, _) in _cache.items() if now - t > FEED_TTL] or list(_cache)[:CACHE_MAX // 2]:
                _cache.pop(k, None)
        _cache[key] = (now, value)
    return value


# ---------- pages ----------

@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


# ---------- RSS / Atom ----------

def fetch_feed(url):
    _, body = netguard.safe_get(url, headers={"User-Agent": UA}, timeout=10)
    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        raise ValueError("That URL didn’t return a valid RSS or Atom feed")
    items = []
    for e in parsed.entries[:50]:
        ts = e.get("published_parsed") or e.get("updated_parsed")
        published = None
        if ts:
            published = datetime(*ts[:6], tzinfo=timezone.utc).isoformat()
        summary = e.get("summary", "")
        pts = re.search(r"Points:\s*(\d+)", summary)
        ncom = re.search(r"#\s*Comments:\s*(\d+)", summary)
        items.append({
            "title": (e.get("title") or "(untitled)").strip(),
            "link": e.get("link", ""),
            "published": published,
            "comments": e.get("comments", ""),
            "points": int(pts.group(1)) if pts else None,
            "num_comments": int(ncom.group(1)) if ncom else None,
        })
    return {"title": parsed.feed.get("title", ""), "items": items}


@app.get("/api/feed")
def api_feed():
    url = request.args.get("url", "").strip()
    if len(url) > 2048 or urlparse(url).scheme not in ("http", "https"):
        return jsonify(error="Feed URL must start with http:// or https://"), 400
    try:
        n = max(1, min(int(request.args.get("n", 8)), 50))
    except ValueError:
        n = 8
    try:
        data = cached(f"feed:{url}", FEED_TTL, lambda: fetch_feed(url))
    except ValueError as exc:  # our own checks (incl. BlockedURL): safe to show
        return jsonify(error=str(exc)), 400
    except requests.HTTPError as exc:
        return jsonify(error=f"Feed returned HTTP {exc.response.status_code}"), 502
    except Exception:  # network errors etc.; don't echo internals to the client
        app.logger.exception("feed fetch failed: %s", url)
        return jsonify(error="Couldn't fetch that feed"), 502
    return jsonify(title=data["title"], items=data["items"][:n])


# ---------- stock quotes ----------

def fetch_quote(symbol):
    info = yf.Ticker(symbol).fast_info
    price = float(info["last_price"])
    prev = float(info["previous_close"])
    change = price - prev
    pct = (change / prev * 100) if prev else 0.0
    return {"symbol": symbol, "price": price, "change": change, "pct": pct}


def safe_quote(symbol):
    try:
        return cached(f"quote:{symbol}", QUOTE_TTL, lambda: fetch_quote(symbol))
    except Exception:
        return {"symbol": symbol, "error": "No data"}


@app.get("/api/quotes")
def api_quotes():
    raw = request.args.get("symbols", "")
    symbols = []
    for s in raw.split(","):
        s = s.strip().upper()
        if s and SYMBOL_RE.match(s) and s not in symbols:
            symbols.append(s)
    symbols = symbols[:30]
    if not symbols:
        return jsonify(error="Add at least one ticker symbol"), 400
    with ThreadPoolExecutor(max_workers=8) as pool:
        quotes = list(pool.map(safe_quote, symbols))
    return jsonify(quotes=quotes)


# ---------- layout persistence ----------

@app.get("/api/layout")
def get_layout():
    with _layout_lock:
        if LAYOUT_FILE.exists():
            try:
                return jsonify(json.loads(LAYOUT_FILE.read_text("utf-8")))
            except json.JSONDecodeError:
                pass
    return jsonify(None)


@app.post("/api/layout")
def save_layout():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("columns"), list):
        return jsonify(error="Invalid layout"), 400
    tmp = LAYOUT_FILE.with_suffix(".tmp")
    with _layout_lock:
        tmp.write_text(json.dumps(payload, indent=2), "utf-8")
        os.replace(tmp, LAYOUT_FILE)
    return jsonify(ok=True)


if __name__ == "__main__":
    # HOST=0.0.0.0 to serve the LAN; also list LAN names/IPs in START_PAGE_HOSTS.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5055))
    print(f"Start page running at http://{host}:{port}")
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8, ident="start-page")
    except ImportError:
        app.run(host=host, port=port, debug=False)
