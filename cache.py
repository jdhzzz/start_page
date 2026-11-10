"""A small in-memory cache shared by the feed, quote and Hacker News endpoints."""
import threading
import time

CACHE_MAX = 500      # entries; stops a client filling memory with unique keys
PURGE_EVERY = 60     # seconds between sweeps for expired entries

_cache = {}          # key -> (expires_at, value)
_lock = threading.Lock()
_last_purge = 0.0


def cached(key, ttl, fn):
    """Return a cached value for key, or compute it with fn(). Errors are not cached."""
    global _last_purge
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now < hit[0]:
            return hit[1]
    value = fn()
    now = time.time()
    with _lock:
        if now - _last_purge > PURGE_EVERY or len(_cache) >= CACHE_MAX:
            _last_purge = now
            for k in [k for k, (expires, _) in _cache.items() if expires <= now]:
                del _cache[k]
            if len(_cache) >= CACHE_MAX:   # still full of live entries: drop the oldest half
                for k in list(_cache)[:CACHE_MAX // 2]:
                    del _cache[k]
        _cache[key] = (now + ttl, value)
    return value
