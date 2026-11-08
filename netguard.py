"""Guards for a start page that is reachable from the LAN.

- safe_get(): fetch a user-supplied URL without letting it reach private,
  loopback or link-local addresses (blocks SSRF into the router, NAS, etc.),
  re-checking every redirect hop and capping the response size.
- install(app): Host-header allowlist (defeats DNS rebinding), Origin check
  on state-changing requests (defeats CSRF), and security headers.
"""
import ipaddress
import os
import socket
from urllib.parse import urljoin, urlparse

import requests
from flask import abort, request

MAX_FETCH_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5


class BlockedURL(ValueError):
    pass


def _check_host(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise BlockedURL("Feed URL must start with http:// or https://")
    if p.username or p.password:
        raise BlockedURL("Feed URLs with embedded credentials aren't allowed")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise BlockedURL(f"Couldn't resolve {p.hostname}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise BlockedURL("Feeds on private or local network addresses aren't allowed")


def safe_get(url, headers=None, timeout=10):
    """GET a public URL; returns (final_url, body_bytes)."""
    with requests.Session() as s:
        for _ in range(MAX_REDIRECTS + 1):
            _check_host(url)
            r = s.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=False)
            if r.is_redirect:
                url = urljoin(url, r.headers.get("Location", ""))
                r.close()
                continue
            r.raise_for_status()
            body = bytearray()
            for chunk in r.iter_content(65536):
                body += chunk
                if len(body) > MAX_FETCH_BYTES:
                    r.close()
                    raise ValueError("Feed is too large")
            return url, bytes(body)
    raise ValueError("Too many redirects")


def _allowed_hosts():
    hosts = {"localhost", "127.0.0.1", "::1", socket.gethostname().lower(),
             f"{socket.gethostname().lower()}.local"}
    extra = os.environ.get("START_PAGE_HOSTS", "")
    hosts.update(h.strip().lower() for h in extra.split(",") if h.strip())
    return hosts


def _hostname(netloc):
    netloc = netloc.strip().lower()
    if netloc.startswith("["):
        return netloc[1:netloc.find("]")]
    return netloc.rsplit(":", 1)[0] if netloc.count(":") == 1 else netloc


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: https:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action https://www.google.com")


def install(app):
    allowed = _allowed_hosts()

    @app.before_request
    def _guard():
        host = _hostname(request.host or "")
        # IP literals can't be DNS-rebinding attacks (those always arrive with
        # the attacker's hostname), so any IP is fine; names must be listed.
        if host not in allowed and not _is_ip(host):
            abort(421)  # Misdirected Request: unknown Host, likely DNS rebinding
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if origin and _hostname(urlparse(origin).netloc) != host:
                abort(403)
            if not request.is_json:
                abort(415)

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        resp.headers["Server"] = "start-page"
        return resp
