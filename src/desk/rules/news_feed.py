"""Inbound News Watcher statuses. Off until account.news_feed_enabled is true.

The token is NEWS_FEED_TOKEN in the environment. It is never written into a
response, a journal row, or a log line. A missing, expired, or non-ALLOW
status fails closed the same way an empty account.news_status does.
"""

from __future__ import annotations

import hmac
import json
import os

GLOBAL_SCOPE = "*"
STATUSES = ("ALLOW", "CAUTION", "VETO")
TOKEN_ENV = "NEWS_FEED_TOKEN"
MAX_TTL_SECONDS = 7 * 24 * 3600
MAX_REASON = 500
MAX_SKEW_MS = 60_000
MAX_BODY = 64_000

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def news_feed_enabled(settings: dict) -> bool:
    return (settings.get("account") or {}).get("news_feed_enabled") is True


def news_token() -> str:
    return os.environ.get(TOKEN_ENV, "")


def loopback_host(host: str | None) -> bool:
    return str(host or "").strip("[]") in LOOPBACK_HOSTS


def _expired(row: dict, now_ms: int) -> bool:
    try:
        return int(now_ms) >= int(row["ts_ms"]) + int(row["ttl_ms"])
    except (TypeError, ValueError, KeyError):
        return True


def resolve_status(store, now_ms: int, symbol: str | None) -> tuple[str | None, str]:
    """Latest unexpired symbol row, otherwise the unexpired global row."""
    scopes = []
    if symbol and symbol != GLOBAL_SCOPE:
        scopes.append(str(symbol).upper())
    scopes.append(GLOBAL_SCOPE)
    for scope in scopes:
        row = store.get_news_feed(scope)
        if row is None or _expired(row, now_ms):
            continue
        status = str(row.get("status") or "").upper()
        reason = str(row.get("reason") or "")
        if status not in STATUSES:
            continue
        return status, reason
    return None, ""


def news_verdict(settings: dict, store, now_ms: int, symbols: list[str], fallback) -> tuple[bool, str]:
    """True only for ALLOW. The feed is ignored until the setting is turned on."""
    if not news_feed_enabled(settings):
        news = None if fallback is None else str(fallback).strip()
        if not news or news.upper() in {"NONE", "NULL"}:
            return False, "News Watcher status is missing"
        if news.upper() != "ALLOW":
            return False, "News Watcher status is missing"
        return True, "News Watcher ALLOW"
    if store is None:
        return False, "News Watcher status is missing"
    names = [str(symbol).upper() for symbol in symbols if symbol and str(symbol).upper() not in {"E2", "E3"}]
    if not names:
        names = [GLOBAL_SCOPE]
    for symbol in names:
        scope = None if symbol == GLOBAL_SCOPE else symbol
        status, reason = resolve_status(store, now_ms, scope)
        if status is None:
            return False, "News Watcher status is missing"
        if status != "ALLOW":
            text = reason or f"News Watcher status is {status}"
            return False, text
    return True, "News Watcher ALLOW"


def packet_symbols(packet: dict) -> list[str]:
    found: list[str] = []
    for row in packet.get("targets") or []:
        if isinstance(row, dict) and row.get("symbol"):
            found.append(str(row["symbol"]))
    kind = packet.get("packet_kind")
    symbol = packet.get("symbol")
    if symbol and kind != "rebalance":
        found.append(str(symbol))
    ordered: list[str] = []
    seen: set[str] = set()
    for symbol in found:
        if symbol in seen:
            continue
        seen.add(symbol)
        ordered.append(symbol)
    return ordered


def apply_news_post(
    store,
    settings: dict,
    body: bytes,
    headers: dict,
    *,
    now_ms: int,
    client_host: str,
    bind_host: str,
) -> tuple[int, dict]:
    """Store one post. Refuses a non-loopback peer and never echoes the token."""
    if not loopback_host(bind_host) or not loopback_host(client_host):
        return 403, {"ok": False, "error": "loopback_only"}
    token = news_token()
    if not token:
        return 503, {"ok": False, "error": "news_token_missing"}
    given = _presented_token(headers)
    if not given or not hmac.compare_digest(given, token):
        return 401, {"ok": False, "error": "unauthorized"}
    if len(body) <= 0 or len(body) > MAX_BODY:
        return 400, {"ok": False, "error": "invalid_length"}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return 400, {"ok": False, "error": "invalid_json"}
    items = _items(payload)
    if isinstance(items, str):
        return 400, {"ok": False, "error": items}
    try:
        for item in items:
            scope, status, reason, ts_ms, ttl_ms = _validate_item(item, now_ms)
            store.save_news_feed(scope, status, reason, ts_ms, ttl_ms, now_ms)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    return 200, {"ok": True, "stored": len(items)}


def _presented_token(headers: dict) -> str:
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    explicit = lowered.get("x-news-token", "").strip()
    if explicit:
        return explicit
    auth = lowered.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _items(payload) -> list[dict] | str:
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        rows = payload["items"]
        if not rows or len(rows) > 50:
            return "items must contain 1 to 50 statuses"
        if not all(isinstance(row, dict) for row in rows):
            return "items must be objects"
        return rows
    if isinstance(payload, dict):
        return [payload]
    return "body must be an object"


def _validate_item(item: dict, now_ms: int) -> tuple[str, str, str, int, int]:
    raw_symbol = item.get("symbol", GLOBAL_SCOPE)
    if raw_symbol is None or str(raw_symbol).strip() in {"", "*"}:
        scope = GLOBAL_SCOPE
    else:
        scope = str(raw_symbol).strip().upper()
        if len(scope) > 32 or not scope.isalnum():
            raise ValueError("bad_symbol")
    status = str(item.get("status") or "").strip().upper()
    if status not in STATUSES:
        raise ValueError("bad_status")
    reason = str(item.get("reason") or "").strip()
    if not reason or len(reason) > MAX_REASON:
        raise ValueError("bad_reason")
    try:
        ts_ms = int(item["ts_ms"])
        ttl_seconds = int(item["ttl_seconds"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("bad_time") from None
    if ttl_seconds < 1 or ttl_seconds > MAX_TTL_SECONDS:
        raise ValueError("bad_ttl")
    if ts_ms > int(now_ms) + MAX_SKEW_MS:
        raise ValueError("bad_time")
    ttl_ms = ttl_seconds * 1000
    if int(now_ms) >= ts_ms + ttl_ms:
        raise ValueError("expired")
    return scope, status, reason, ts_ms, ttl_ms
