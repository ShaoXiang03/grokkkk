"""Loopback HTTP server for the read-only dashboard. GET only.

A non-loopback bind is refused unless the process was started with
``--allow-remote`` and ``DASHBOARD_TOKEN`` is set. The token is never read
from the URL or from settings.yaml.
"""

from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from desk.dashboard.read import build_snapshot
from desk.dashboard.views import filter_journal, is_loopback

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/app.css": "app.css",
}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class DashboardBindError(ValueError):
    """The requested listen address is not allowed."""


def resolve_listen(spec: str, *, allow_remote: bool, token: str | None) -> tuple[str, int, str | None]:
    """Return host, port, and the token the server must require.

    Loopback is the default and needs no token. A token that is set is
    enforced even on loopback. Anything else needs ``allow_remote`` and a
    token of at least 16 characters.
    """
    host, port = _split_listen(spec)
    cleaned = (token or "").strip()
    if cleaned and len(cleaned) < 16:
        raise DashboardBindError("DASHBOARD_TOKEN must be at least 16 characters. Do not pass it on the command line.")
    enforced = cleaned or None
    if is_loopback(host):
        return host, port, enforced
    if not allow_remote:
        raise DashboardBindError(
            f"Refusing to bind {host}. The dashboard listens on loopback. "
            "Use an SSH tunnel, or pass --allow-remote with DASHBOARD_TOKEN set in the environment."
        )
    if not enforced:
        raise DashboardBindError(
            "DASHBOARD_TOKEN is required with --allow-remote. Set it in the environment, not on the command line."
        )
    return host, port, enforced


def _split_listen(spec: str) -> tuple[str, int]:
    text = (spec or "").strip()
    if text.startswith("["):
        end = text.find("]")
        if end < 0 or not text[end:].startswith("]:"):
            raise DashboardBindError("listen must be host:port")
        host = text[1:end]
        port_text = text[end + 2 :]
    else:
        host, sep, port_text = text.rpartition(":")
        if not sep or not host:
            raise DashboardBindError("listen must be host:port")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise DashboardBindError("listen port must be an integer") from exc
    if port < 1 or port > 65535:
        raise DashboardBindError("listen port is out of range")
    return host, port


def bearer_ok(header: str | None, token: str | None) -> bool:
    if not token:
        return True
    prefix = "Bearer "
    presented = header or ""
    if not presented.startswith(prefix):
        return False
    got = presented[len(prefix) :].strip().encode("utf-8")
    expected = token.encode("utf-8")
    if len(got) != len(expected):
        return False
    return hmac.compare_digest(got, expected)


class DashboardServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(host: str, port: int, context) -> DashboardServer:
    httpd = DashboardServer((host, port), DashboardHandler)
    httpd.context = context
    return httpd


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ChartAnalystDashboard/0.4"
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.request.settimeout(15)

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        if path in ("/health", "/api/snapshot", "/api/journal") and not bearer_ok(
            self.headers.get("Authorization"), getattr(self.server, "context").token
        ):
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if path == "/health":
            self._send_json(200, {"ok": True, "read_only": True, "orders": False})
            return
        if path == "/api/snapshot":
            self._snapshot()
            return
        if path == "/api/journal":
            self._journal(parse_qs(parsed.query))
            return
        self._static(path)

    def do_HEAD(self):
        if urlparse(self.path).path in STATIC_FILES:
            self._static(urlparse(self.path).path, body=False)
            return
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.end_headers()

    def do_POST(self):
        self._reject_write()

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST

    def _reject_write(self):
        self._send_json(
            405,
            {
                "ok": False,
                "error": "read_only",
                "detail": "The dashboard does not accept changes. It cannot place orders, review signals, or touch the kill switch.",
            },
        )

    def _snapshot(self):
        context = self.server.context
        try:
            now_ms = None if context.now_ms is None else context.now_ms()
            payload = build_snapshot(
                context.settings,
                now_ms=now_ms,
                secrets_env_path=context.secrets_env_path,
                environ=context.environ,
            )
        except FileNotFoundError:
            self._send_json(
                503,
                {
                    "ok": False,
                    "error": "database_unavailable",
                    "detail": "The sqlite database is not there. The dashboard does not create it.",
                },
            )
            return
        except sqlite3_error_type() as exc:
            _ = exc
            self._send_json(
                503,
                {"ok": False, "error": "database_unreadable", "detail": "The sqlite file could not be read."},
            )
            return
        except Exception:
            self._send_json(500, {"ok": False, "error": "read_failed"})
            return
        self._send_json(200, payload)

    def _journal(self, query: dict):
        context = self.server.context
        try:
            now_ms = None if context.now_ms is None else context.now_ms()
            payload = build_snapshot(
                context.settings,
                now_ms=now_ms,
                secrets_env_path=context.secrets_env_path,
                environ=context.environ,
            )
        except FileNotFoundError:
            self._send_json(503, {"ok": False, "error": "database_unavailable"})
            return
        except Exception:
            self._send_json(500, {"ok": False, "error": "read_failed"})
            return
        rows = filter_journal(
            payload.get("journal") or [],
            book=_first(query, "book"),
            symbol=_first(query, "symbol"),
            event=_first(query, "event"),
            text=_first(query, "q"),
        )
        self._send_json(200, {"read_only": True, "journal": rows, "journal_meta": payload.get("journal_meta")})

    def _static(self, path: str, body: bool = True):
        name = STATIC_FILES.get(path)
        if name is None:
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        target = STATIC_DIR / name
        if not target.is_file():
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        data = target.read_bytes() if body else b""
        content_type = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(target.stat().st_size if not body else len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if body:
            self.wfile.write(data)

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status == 405:
            self.send_header("Allow", "GET")
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'",
        )


def sqlite3_error_type():
    import sqlite3

    return sqlite3.Error


def _first(query: dict, name: str) -> str:
    values = query.get(name) or []
    if not values:
        return ""
    return str(values[0])
