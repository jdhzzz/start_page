"""Personal start page: a widget portal with RSS and stock widgets.

Run:  python app.py   then open http://localhost:5055
"""
import json
import os
import re
import shutil
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
from cache import cached
from ribbon import bp as ribbon_bp
from weather import bp as weather_bp
from hn import bp as hn_bp
from health import bp as health_bp

BASE = Path(__file__).parent
STATIC = BASE / "static"
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
LAYOUT_FILE = DATA / "layout.json"
LAYOUT_BAK = DATA / "layout.json.bak"
BACKUP_EVERY = 24 * 3600   # seconds between refreshes of the backup copy

#UA = "Mozilla/5.0 (compatible; StartPage/1.0)"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-^=]{1,15}$")

FEED_TTL = 300   # seconds a fetched feed is reused
QUOTE_TTL = 60   # seconds a fetched quote is reused

app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024  # layout POSTs are a few KB
netguard.install(app)
app.register_blueprint(ribbon_bp)
app.register_blueprint(weather_bp)
app.register_blueprint(hn_bp)
app.register_blueprint(health_bp)

_layout_lock = threading.Lock()


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

def _read_layout(path):
    """The layout stored in path, or None if there is no file.
    Raises ValueError if the file is there but isn't a usable layout."""
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        return None
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("columns"), list):
        raise ValueError("not a layout")
    return data


def _version(layout):
    v = layout.get("version") if layout else 0
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


@app.get("/api/layout")
def get_layout():
    with _layout_lock:
        try:
            return jsonify(_read_layout(LAYOUT_FILE))
        except (ValueError, OSError):
            app.logger.exception("layout.json is unreadable; trying the backup")
        try:
            backup = _read_layout(LAYOUT_BAK)
        except (ValueError, OSError):
            backup = None
    if backup is not None:
        return jsonify(backup)
    # An error, not "no layout": the page must not save defaults over whatever is there.
    return jsonify(error="The saved layout file is damaged"), 500


@app.post("/api/layout")
def save_layout():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("columns"), list):
        return jsonify(error="Invalid layout"), 400
    tmp = LAYOUT_FILE.with_suffix(".tmp")
    with _layout_lock:
        try:
            current = _read_layout(LAYOUT_FILE)
        except (ValueError, OSError):
            current = None   # damaged: let this save replace it
        if current is not None:
            # Each save carries the version it was based on. A mismatch means another
            # tab or device saved in between, so refuse rather than overwrite its work.
            if _version(payload) != _version(current):
                return jsonify(error="The layout was changed somewhere else", layout=current), 409
            try:
                if (not LAYOUT_BAK.exists()
                        or time.time() - LAYOUT_BAK.stat().st_mtime > BACKUP_EVERY):
                    shutil.copyfile(LAYOUT_FILE, LAYOUT_BAK)
            except OSError:
                app.logger.exception("couldn't refresh layout.json.bak")
        payload["version"] = _version(current) + 1
        tmp.write_text(json.dumps(payload, indent=2), "utf-8")
        os.replace(tmp, LAYOUT_FILE)
    return jsonify(ok=True, version=payload["version"])


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
