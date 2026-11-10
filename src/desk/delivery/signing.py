"""HMAC-SHA256 for outbound signal posts and inbound review posts.

The signed payload is ``{timestamp}.{body}`` where ``body`` is the exact
request bytes and ``timestamp`` is the ``X-Desk-Timestamp`` header in
milliseconds. The secret is read from the environment, never from YAML.
"""

from __future__ import annotations

import hashlib
import hmac
import os

SIGNATURE_HEADER = "X-Desk-Signature"
TIMESTAMP_HEADER = "X-Desk-Timestamp"
IDEMPOTENCY_HEADER = "Idempotency-Key"


def webhook_secret(settings: dict) -> str:
    name = (settings.get("webhook") or {}).get("secret_env") or "WEBHOOK_SECRET"
    return os.environ.get(str(name), "")


def webhook_url(settings: dict) -> str:
    override = os.environ.get("WEBHOOK_URL", "")
    if override.strip():
        return override.strip()
    return str((settings.get("webhook") or {}).get("url") or "").strip()


def review_secret(settings: dict) -> str:
    name = (settings.get("review") or {}).get("secret_env") or "REVIEW_SECRET"
    secret = os.environ.get(str(name), "")
    if secret:
        return secret
    return webhook_secret(settings)


def sign_payload(body: str, secret: str, timestamp: str) -> str:
    if not secret:
        raise ValueError("refusing to sign without a secret")
    message = f"{timestamp}.{body}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_signature(
    body: str,
    secret: str,
    timestamp: str,
    signature: str,
    *,
    now_ms: int,
    max_age_ms: int,
) -> bool:
    """Return True only for a matching signature inside the age window."""
    if not secret or not signature or not timestamp:
        return False
    try:
        stamped = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(now_ms) - stamped) > int(max_age_ms):
        return False
    try:
        expected = sign_payload(body, secret, str(timestamp))
    except ValueError:
        return False
    try:
        return hmac.compare_digest(expected, str(signature).strip())
    except (TypeError, ValueError):
        return False
