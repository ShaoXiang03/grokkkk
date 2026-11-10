"""POST a signal packet to a configurable webhook. Off unless settings say otherwise.

The intended target is Market Bot's webhook. This module does not know how to
trade. Retries cover transport failures only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any

from desk.scanner.signals import packet_json


class WebhookError(RuntimeError):
    pass


def send_packet(packet: dict, settings: dict, session=None, sleeper=time.sleep) -> dict:
    hook = settings["webhook"]
    if not hook.get("enabled"):
        return {"sent": False, "reason": "disabled"}
    url = (hook.get("url") or "").strip()
    if not url:
        return {"sent": False, "reason": "no_url"}
    if packet.get("status") != "proposed" and not hook.get("send_rejects"):
        return {"sent": False, "reason": "reject_suppressed"}

    body = packet_json(packet)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "chart-analyst-scanner/0.3",
        "X-Signal-Id": str(packet.get("signal_id", "")),
    }
    secret = os.environ.get(hook.get("secret_env") or "", "")
    if secret:
        headers["X-Desk-Signature"] = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()

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
            try:
                response = client.post(url, data=body, headers=headers, timeout=timeout)
            except Exception as exc:  # network errors only; no fallback order path
                last_error = str(exc)
                if attempt < attempts:
                    sleeper(min(2 ** (attempt - 1), 8))
                continue
            status = getattr(response, "status_code", 0)
            if 200 <= status < 300:
                return {"sent": True, "status_code": status, "attempts": attempt}
            last_error = f"HTTP {status}"
            if status < 500 or attempt == attempts:
                break
            sleeper(min(2 ** (attempt - 1), 8))
    finally:
        if close_client:
            client.close()
    return {"sent": False, "reason": last_error}


def signature_for(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def canonical_body(packet: dict) -> str:
    return json.dumps(packet, sort_keys=True, separators=(",", ":"), default=str)
