"""Durable webhook outbox.

A packet is stored before the first POST. Retries keep the same body and the
same idempotency key. A missing secret or URL does not drop the row and does
not burn a retry. Dead-letter rows keep the body.
"""

from __future__ import annotations

from desk.delivery.signing import (
    IDEMPOTENCY_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign_payload,
    webhook_secret,
    webhook_url,
)
from desk.scanner.signals import packet_json
from desk.scanner.webhook import post_once


def backoff_ms(attempt: int, settings: dict) -> int:
    """Milliseconds to wait after ``attempt`` failures. Attempt is 1-based."""
    hook = settings["webhook"]
    base = float(hook.get("retry_backoff_seconds") or 1)
    cap = float(hook.get("retry_backoff_max_seconds") or 60)
    delay = min(base * (2 ** (max(attempt, 1) - 1)), cap)
    return int(delay * 1000)


def idempotency_key(packet: dict) -> str:
    key = str(packet.get("signal_id") or "").strip()
    if not key:
        raise ValueError("signal_id is required for an idempotency key")
    return key


def enqueue_packet(store, packet: dict, settings: dict, now_ms: int) -> dict:
    hook = settings["webhook"]
    if not hook.get("enabled"):
        return {"queued": False, "reason": "disabled"}
    if packet.get("status") != "proposed" and not hook.get("send_rejects"):
        return {"queued": False, "reason": "reject_suppressed"}
    key = idempotency_key(packet)
    existing = store.outbox_by_key(key)
    if existing is not None:
        return {"queued": False, "reason": "duplicate", "idempotency_key": key, "status": existing["status"]}
    body = packet_json(packet)
    store.insert_outbox(
        {
            "signal_id": packet["signal_id"],
            "idempotency_key": key,
            "body": body,
            "status": "pending",
            "attempts": 0,
            "next_attempt_ms": int(now_ms),
            "last_error": None,
            "created_ms": int(now_ms),
            "updated_ms": int(now_ms),
            "delivered_ms": None,
        }
    )
    return {"queued": True, "idempotency_key": key, "status": "pending"}


def _headers(body: str, signal_id: str, settings: dict, now_ms: int) -> tuple[dict, str | None]:
    timestamp = str(int(now_ms))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "chart-analyst/0.4",
        "X-Signal-Id": signal_id,
        IDEMPOTENCY_HEADER: signal_id,
        TIMESTAMP_HEADER: timestamp,
    }
    secret = webhook_secret(settings)
    require = bool(settings["webhook"].get("require_signature", True))
    if secret:
        headers[SIGNATURE_HEADER] = sign_payload(body, secret, timestamp)
        return headers, None
    if require:
        return headers, "missing_secret"
    return headers, None


def drain_outbox(store, settings: dict, now_ms: int, session=None) -> list[dict]:
    """POST each due row once. Backoff is persisted on the row."""
    url = webhook_url(settings)
    timeout = float(settings["webhook"].get("timeout_seconds") or 5)
    max_retries = int(settings["webhook"].get("max_retries") or 1)
    outcomes = []
    own_session = False
    client = session
    if client is None and url:
        import requests

        client = requests.Session()
        own_session = True
    try:
        for row in store.due_outbox(now_ms):
            outcomes.append(_attempt(store, settings, row, url, timeout, max_retries, now_ms, client))
    finally:
        if own_session and client is not None:
            client.close()
    return outcomes


def _attempt(store, settings, row: dict, url: str, timeout: float, max_retries: int, now_ms: int, client) -> dict:
    key = row["idempotency_key"]
    body = row["body"]
    base = {
        "signal_id": row["signal_id"],
        "idempotency_key": key,
        "attempts": int(row["attempts"]),
    }
    if not url:
        store.update_outbox(key, last_error="no_url", updated_ms=now_ms, next_attempt_ms=now_ms + backoff_ms(1, settings))
        return {**base, "sent": False, "reason": "no_url", "status": "pending"}
    headers, hold = _headers(body, row["signal_id"], settings, now_ms)
    if hold:
        store.update_outbox(
            key,
            last_error=hold,
            updated_ms=now_ms,
            next_attempt_ms=now_ms + backoff_ms(1, settings),
        )
        return {**base, "sent": False, "reason": hold, "status": "pending"}
    if client is None:
        store.update_outbox(key, last_error="no_client", updated_ms=now_ms, next_attempt_ms=now_ms + backoff_ms(1, settings))
        return {**base, "sent": False, "reason": "no_client", "status": "pending"}
    result = post_once(url, body, headers, timeout, client)
    if result.get("sent"):
        store.update_outbox(
            key,
            status="delivered",
            last_error=None,
            updated_ms=now_ms,
            delivered_ms=now_ms,
            attempts=int(row["attempts"]) + 1,
        )
        return {**base, "sent": True, "status": "delivered", "status_code": result.get("status_code"), "attempts": int(row["attempts"]) + 1}
    attempts = int(row["attempts"]) + 1
    if (not result.get("retryable")) or attempts >= max_retries:
        store.update_outbox(
            key,
            status="dead",
            attempts=attempts,
            last_error=result.get("reason"),
            updated_ms=now_ms,
            next_attempt_ms=now_ms,
        )
        return {**base, "sent": False, "status": "dead", "reason": result.get("reason"), "attempts": attempts}
    store.update_outbox(
        key,
        status="pending",
        attempts=attempts,
        last_error=result.get("reason"),
        updated_ms=now_ms,
        next_attempt_ms=now_ms + backoff_ms(attempts, settings),
    )
    return {**base, "sent": False, "status": "pending", "reason": result.get("reason"), "attempts": attempts}
