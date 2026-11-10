"""Local review endpoint. Stdlib only. It does not place orders.

Bind it to loopback. A request without a valid HMAC is refused and is not
written to the journal.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from desk.delivery.eligibility import ReviewError, public_review, submit_review
from desk.delivery.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, review_secret, verify_signature
from desk.journal.decision import DecisionJournal


def signature_max_age_ms(settings: dict) -> int:
    seconds = float(settings["webhook"].get("signature_max_age_seconds") or 300)
    return int(seconds * 1000)


def apply_http_review(
    store,
    settings: dict,
    signal_id: str,
    body: bytes,
    headers: dict,
    *,
    now_ms: int,
    account: dict | None = None,
    open_positions: list[dict] | None = None,
) -> tuple[int, dict]:
    secret = review_secret(settings)
    if not secret:
        return 503, {"ok": False, "error": "review_secret_missing"}
    with store.lock:
        return _apply_locked(store, settings, signal_id, body, headers, now_ms, account, open_positions, secret)


def _apply_locked(store, settings, signal_id, body, headers, now_ms, account, open_positions, secret):
    text = body.decode("utf-8")
    lowered = {str(key).lower(): value for key, value in headers.items()}
    timestamp = str(lowered.get(TIMESTAMP_HEADER.lower(), "") or "")
    signature = str(lowered.get(SIGNATURE_HEADER.lower(), "") or "")
    if not verify_signature(
        text,
        secret,
        timestamp,
        signature,
        now_ms=now_ms,
        max_age_ms=signature_max_age_ms(settings),
    ):
        return 401, {"ok": False, "error": "bad_signature"}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return 400, {"ok": False, "error": "invalid_json"}
    if not isinstance(payload, dict):
        return 400, {"ok": False, "error": "body must be an object"}
    try:
        result = submit_review(
            store,
            settings,
            signal_id,
            payload.get("market_context"),
            payload.get("master_decision"),
            now_ms=now_ms,
            journal=DecisionJournal(store, settings),
            account=account,
            open_positions=open_positions,
        )
    except ReviewError as exc:
        return exc.status, {"ok": False, "error": str(exc)}
    return 200, public_review(result)


def serve_review(store, settings: dict, *, account_provider=None) -> ThreadingHTTPServer:
    listen = str(settings["review"].get("listen") or "").strip()
    if not listen:
        raise RuntimeError("review.listen is empty; the review endpoint is off")
    host, _, port_text = listen.rpartition(":")
    if not host or not port_text:
        raise RuntimeError("review.listen must be host:port")
    port = int(port_text)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            if self.path.split("?", 1)[0] != "/health":
                self._send(404, {"ok": False, "error": "not_found"})
                return
            self._send(200, {"ok": True, "mode": "paper", "orders": False})

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            prefix = "/signals/"
            suffix = "/review"
            if not (path.startswith(prefix) and path.endswith(suffix)):
                self._send(404, {"ok": False, "error": "not_found"})
                return
            signal_id = path[len(prefix) : -len(suffix)]
            if not signal_id or "/" in signal_id:
                self._send(404, {"ok": False, "error": "not_found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 1_000_000:
                self._send(400, {"ok": False, "error": "invalid_length"})
                return
            body = self.rfile.read(length)
            with store.lock:
                account = None
                positions = None
                if account_provider is not None:
                    provided = account_provider()
                    if provided is not None:
                        account = provided.get("account")
                        positions = provided.get("open_positions")
                status, payload = apply_http_review(
                    store,
                    settings,
                    signal_id,
                    body,
                    dict(self.headers.items()),
                    now_ms=_now_ms(),
                    account=account,
                    open_positions=positions,
                )
            self._send(status, payload)

        def log_message(self, fmt, *args):
            return

        def _send(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer((host, port), Handler)
    return server


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
