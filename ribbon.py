"""Market ribbon: index tiles with sparklines, like the strip atop Yahoo Finance.

Each equity-index tile shows, in order of preference:
  1. the index itself, while the regular US session is open;
  2. the matching futures contract, if the index is closed but futures are trading;
  3. the index at its last close, if both are closed.
Commodity, yield and volatility tiles simply show their latest data.
"""
import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
from flask import Blueprint, jsonify

bp = Blueprint("ribbon", __name__)

# name/symbol = the index; fut/fut_name = the futures shown when the index is closed.
TILES = [
    {"name": "S&P 500", "symbol": "^GSPC", "fut": "ES=F", "fut_name": "S&P futures"},
    {"name": "Dow", "symbol": "^DJI", "fut": "YM=F", "fut_name": "Dow futures"},
    {"name": "Nasdaq", "symbol": "^IXIC", "fut": "NQ=F", "fut_name": "Nasdaq futures"},
    {"name": "Russell 2000", "symbol": "^RUT", "fut": "RTY=F", "fut_name": "Russell futures"},
    {"name": "VIX", "symbol": "^VIX"},
    # kind "yield": 3-month daily trend from Yahoo, shown as a % with changes in basis points.
    {"row": 2, "name": "10-yr yield", "symbol": "^TNX", "kind": "yield"},
    # kind "fred": daily series from FRED (no API key needed). Swap the symbol to change it:
    #   T10Y2Y        10-year minus 2-year Treasury yield (yield-curve spread)
    #   BAMLH0A0HYM2  ICE BofA US High Yield option-adjusted spread (broad junk-bond credit spread)
    #   BAMLH0A3HYC   ICE BofA CCC & lower spread (the distressed end; jumpier, an early-warning gauge)
    {"row": 2, "name": "10y–2y spread", "symbol": "T10Y2Y", "kind": "fred"},
    {"row": 2, "name": "High-yield spread", "symbol": "BAMLH0A0HYM2", "kind": "fred"},
    # kind "trend": daily Yahoo series shown as plain points with a 3-month sparkline.
    {"row": 2, "name": "MOVE (rate vol)", "symbol": "^MOVE", "kind": "trend"},
    {"row": 2, "name": "Gold", "symbol": "GC=F"},
    {"row": 2, "name": "Crude oil", "symbol": "CL=F"},
    {"row": 1, "name": "Bitcoin", "symbol": "BTC-USD"},
]

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
TREND_DAYS = 65         # about three months of trading days
FRED_TTL = 3600

CACHE_TTL = 60
FRESH_MINUTES = 30      # a latest bar older than this means the market isn't trading
MAX_POINTS = 60

_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()


def regular_session_now():
    now = datetime.now(ZoneInfo("America/New_York"))
    return now.weekday() < 5 and dtime(9, 30) <= now.time() < dtime(16, 0)


def intraday(symbol, window):
    """Recent 5-minute closes. window='day' keeps the last trading date,
    window='24h' keeps the last 24 hours (futures sessions span midnight)."""
    df = yf.Ticker(symbol).history(period="5d", interval="5m")
    if df is None or df.empty or "Close" not in df:
        return pd.Series(dtype=float)
    s = df["Close"].dropna()
    if s.empty:
        return s
    if window == "all":
        return s
    if window == "day":
        dates = s.index.date
        return s[dates == dates[-1]]
    return s[s.index >= s.index[-1] - timedelta(hours=24)]


ET = ZoneInfo("America/New_York")


def close_ref_position(s, now=None):
    """Position of the last bar at or before the most recent 4 PM ET cash close
    (the bar starting 3:55 PM, whose closing price is the 4:00 PM price).
    Futures changes are measured from here, so they show the move since the close."""
    now = now or datetime.now(ET)
    et = s.index.tz_convert(ET)
    for d in sorted({t.date() for t in et}, reverse=True):
        close = datetime.combine(d, dtime(16, 0), tzinfo=ET)
        if d.weekday() >= 5 or close > now:
            continue
        last_start = close - timedelta(minutes=5)
        pos = [i for i, t in enumerate(et) if t.date() == d and t <= last_start]
        if pos and close - et[pos[-1]] <= timedelta(hours=4):
            return pos[-1]
    return None


def is_fresh(s):
    if s.empty:
        return False
    age = datetime.now(timezone.utc) - s.index[-1].to_pydatetime()
    return age.total_seconds() < FRESH_MINUTES * 60


def make_tile(name, symbol, s, mode, prev=None, note=None):
    if s.empty:
        raise ValueError("no data")
    price = float(s.iloc[-1])
    if prev is None:
        try:
            prev = float(yf.Ticker(symbol).fast_info["previous_close"])
        except Exception:
            pass
    if not prev:
        prev = float(s.iloc[0])
    change = price - prev
    pct = change / prev * 100 if prev else 0.0

    pts = [round(float(v), 4) for v in s.tolist()]
    step = max(1, len(pts) // MAX_POINTS)
    sampled = pts[::step]
    if (len(pts) - 1) % step:
        sampled.append(pts[-1])
    return {
        "name": name, "symbol": symbol, "mode": mode, "note": note,
        "price": round(price, 4), "change": round(change, 4), "pct": round(pct, 4),
        "prev": round(prev, 4), "base": round(prev, 4), "points": sampled,
    }


def series_tile(item, s, note, link=None, unit="pct"):
    """Tile for a daily series: latest value, change vs the prior observation,
    and a three-month sparkline. Yields and spreads are neutral-colored."""
    if len(s) < 2:
        raise ValueError("no data")
    price, prev = float(s.iloc[-1]), float(s.iloc[-2])
    pct = (price - prev) / prev * 100 if prev else 0.0
    return {
        "name": item["name"], "symbol": item["symbol"], "mode": "trend", "note": note,
        "unit": unit, "neutral": True, "link": link,
        "price": round(price, 4), "change": round(price - prev, 4), "pct": round(pct, 4),
        "prev": round(prev, 4), "base": round(float(s.iloc[0]), 4),
        "points": [round(float(v), 4) for v in s.tolist()],
    }


_fred_cache = {}


def fetch_fred(series_id):
    """Daily series from FRED. Retries once, and falls back to the last good copy
    if FRED is slow or down."""
    hit = _fred_cache.get(series_id)
    if hit and time.time() - hit[0] < FRED_TTL:
        return hit[1]
    last_exc = None
    for attempt in range(2):
        try:
            start = (datetime.now(timezone.utc) - timedelta(days=110)).strftime("%Y-%m-%d")
            r = requests.get(
                "https://fred.stlouisfed.org/graph/fredgraph.csv",
                params={"id": series_id, "cosd": start},
                headers={"User-Agent": UA}, timeout=12,
            )
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            s = pd.to_numeric(df.iloc[:, 1], errors="coerce").dropna().tail(TREND_DAYS).reset_index(drop=True)
            if len(s) < 2:
                raise ValueError("FRED returned no recent values")
            _fred_cache[series_id] = (time.time(), s)
            return s
        except Exception as exc:
            last_exc = exc
            time.sleep(1)
    if hit:
        return hit[1]
    raise last_exc


def daily_closes(symbol):
    df = yf.Ticker(symbol).history(period="4mo", interval="1d")
    if df is None or df.empty or "Close" not in df:
        return pd.Series(dtype=float)
    return df["Close"].dropna().tail(TREND_DAYS).reset_index(drop=True)


def build_tile(item):
    sym, fut = item["symbol"], item.get("fut")
    try:
        if item.get("kind") == "fred":
            return series_tile(item, fetch_fred(sym), "3-month trend, daily",
                               f"https://fred.stlouisfed.org/series/{sym}")
        if item.get("kind") == "trend":
            return series_tile(item, daily_closes(sym), "3-month trend", unit=None)
        if item.get("kind") == "yield":
            return series_tile(item, daily_closes(sym), "3-month trend")
        if fut:
            idx = intraday(sym, "day")
            if regular_session_now() and is_fresh(idx):
                return make_tile(item["name"], sym, idx, "live")
            fs = intraday(fut, "all")
            if is_fresh(fs):
                pos = close_ref_position(fs)
                if pos is not None:
                    return make_tile(item["fut_name"], fut, fs.iloc[pos:], "futures",
                                     prev=float(fs.iloc[pos]), note="Since 4 PM close")
                return make_tile(item["fut_name"], fut, fs[fs.index >= fs.index[-1] - timedelta(hours=24)], "futures")
            return make_tile(item["name"], sym, idx, "close")
        window = "day" if sym.startswith("^") else "24h"
        s = intraday(sym, window)
        return make_tile(item["name"], sym, s, "live" if is_fresh(s) else "close")
    except Exception as exc:
        return {"name": item["name"], "symbol": sym, "error": (str(exc) or type(exc).__name__)[:120]}


_last_good = {}   # tile name -> its last good tile, shown if a later fetch is slow
SLOW_RETRY = 10   # seconds a response with slow tiles is reused before trying again


@bp.get("/api/ribbon")
def api_ribbon():
    with _lock:
        if _cache["data"] is not None and time.time() - _cache["at"] < CACHE_TTL:
            return jsonify(tiles=_cache["data"])

        def build_row(item):
            tile = build_tile(item)
            tile["row"] = item.get("row", 1)
            if not tile.get("error"):
                _last_good[item["name"]] = tile
            return tile

        pool = ThreadPoolExecutor(max_workers=8)
        futures = [pool.submit(build_row, item) for item in TILES]
        deadline = time.time() + 15
        tiles = []
        slow = False
        for item, fut in zip(TILES, futures):
            try:
                tiles.append(fut.result(timeout=max(0.1, deadline - time.time())))
            except Exception:
                slow = True
                tiles.append(_last_good.get(item["name"]) or {
                    "name": item["name"], "symbol": item["symbol"],
                    "row": item.get("row", 1), "error": "Still loading, try again shortly"})
        # Slow tiles keep working in the background and land in _last_good, so a
        # response that had to stand in for them is only reused briefly.
        pool.shutdown(wait=False)
        if any(not t.get("error") for t in tiles):
            _cache.update(at=time.time() - (CACHE_TTL - SLOW_RETRY if slow else 0), data=tiles)
        return jsonify(tiles=tiles)
