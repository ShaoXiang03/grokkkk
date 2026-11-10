"""POST a signal packet to a configurable webhook. Off unless settings say otherwise.

The intended target is Market Bot's webhook. This module does not know how to
trade. The outbox is the durable path; ``send_packet`` is one in-process
attempt loop used by tests and by callers that already hold the body.
"""

from __future__ import annotations

import time

from desk.delivery.signing import (
    IDEMPOTENCY_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign_payload,
    webhook_secret,
    webhook_url,
)
from desk.scanner.signals import packet_json


class WebhookError(RuntimeError):
    pass


def signature_for(body: str, secret: str, timestamp: str | None = None) -> str:
    if timestamp is None:
        import hashlib
        import hmac

        return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return sign_payload(body, secret, timestamp)


def canonical_body(packet: dict) -> str:
    return packet_json(packet)


def build_headers(packet: dict, body: str, settings: dict, now_ms: int) -> tuple[dict, str | None]:
    signal_id = str(packet.get("signal_id") or "")
    timestamp = str(int(now_ms))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "chart-analyst/0.4",
        "X-Signal-Id": signal_id,
        IDEMPOTENCY_HEADER: signal_id,
        TIMESTAMP_HEADER: timestamp,
    }
    secret = webhook_secret(settings)
    if secret:
        headers[SIGNATURE_HEADER] = sign_payload(body, secret, timestamp)
        return headers, None
    if settings["webhook"].get("require_signature", True):
        return headers, "missing_secret"
    return headers, None


def post_once(url: str, body: str, headers: dict, timeout: float, session) -> dict:
    try:
        response = session.post(url, data=body, headers=headers, timeout=timeout)
    except Exception as exc:  # network errors only; no fallback order path
        return {"sent": False, "reason": str(exc), "retryable": True}
    status = int(getattr(response, "status_code", 0) or 0)
    if 200 <= status < 300 or status == 409:
        return {"sent": True, "status_code": status, "retryable": False}
    retryable = status == 429 or status >= 500
    return {"sent": False, "reason": f"HTTP {status}", "status_code": status, "retryable": retryable}


def send_packet(packet: dict, settings: dict, session=None, sleeper=time.sleep, now_ms: int | None = None) -> dict:
    hook = settings["webhook"]
    if not hook.get("enabled"):
        return {"sent": False, "reason": "disabled"}
    url = webhook_url(settings)
    if not url:
        return {"sent": False, "reason": "no_url"}
    if packet.get("status") != "proposed" and not hook.get("send_rejects"):
        return {"sent": False, "reason": "reject_suppressed"}

    stamped = int(now_ms if now_ms is not None else time.time() * 1000)
    body = packet_json(packet)
    headers, hold = build_headers(packet, body, settings, stamped)
    if hold:
        return {"sent": False, "reason": hold}

    client = session
    close_client = False
    if client is None:
        import requests

        client = requests.Session()
        close_client = True
    attempts = int(hook.get("max_retries") or 1)
    timeout = float(hook.get("timeout_seconds") or 5)
    last_error = "not sent"
    try:
        for attempt in range(1, attempts + 1):
            result = post_once(url, body, headers, timeout, client)
            if result.get("sent"):
                return {"sent": True, "status_code": result.get("status_code"), "attempts": attempt}
            last_error = str(result.get("reason") or "not sent")
            if not result.get("retryable") or attempt == attempts:
                break
            sleeper(min(2 ** (attempt - 1), 8))
            stamped += 1
            headers, hold = build_headers(packet, body, settings, stamped)
            if hold:
                return {"sent": False, "reason": hold, "attempts": attempt}
    finally:
        if close_client:
            client.close()
    return {"sent": False, "reason": last_error}
