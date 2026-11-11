"""Plain-language views for the dashboard. No database and no secrets."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone

from desk.timeutils import DAY_MS, MYT, UTC, floor_day_ms, ms_to_dt

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_HOLD_RE = re.compile(
    r"Hold-out: CAGR (?P<cagr>[-0-9.]+)%, Sharpe (?P<sharpe>[-0-9.]+), "
    r"max DD (?P<max_dd>[-0-9.]+)%, total return (?P<total_return>[-0-9.]+)%, "
    r"turnover (?P<turnover>[-0-9.]+)[×x]/year, fees \$(?P<fees>[0-9,]+), "
    r"slippage \$(?P<slippage>[0-9,]+),.*?trades (?P<trades>[0-9,]+), "
    r"expectancy R (?P<expectancy_r>[-0-9.]+)"
)
_FROZEN_RE = re.compile(r"Frozen `([^`]+)`")

# Keys that are identifiers, not credentials, even though they end in "key".
_SAFE_KEYS = {"cluster_key", "idempotency_key", "dedupe_key", "week_key", "day_key"}


def format_myt(ms: int | None, *, seconds: bool = False) -> str | None:
    if ms is None:
        return None
    dt = ms_to_dt(int(ms), MYT)
    clock = dt.strftime("%H:%M:%S") if seconds else dt.strftime("%H:%M")
    return f"{_WEEKDAYS[dt.weekday()]} {dt.day:02d} {_MONTHS[dt.month - 1]} {dt.year}, {clock} MYT"


def format_myt_span(open_ms: int, timeout_ms: int) -> str:
    start = ms_to_dt(int(open_ms), MYT)
    end = ms_to_dt(int(open_ms) + int(timeout_ms), MYT)
    utc_start = ms_to_dt(int(open_ms), UTC)
    utc_end = ms_to_dt(int(open_ms) + int(timeout_ms), UTC)
    return (
        f"{_WEEKDAYS[start.weekday()]} {start.day:02d} {_MONTHS[start.month - 1]} {start.year}, "
        f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')} MYT "
        f"({utc_start.strftime('%H:%M')}–{utc_end.strftime('%H:%M')} UTC)"
    )


def age_phrase(seconds: float | None) -> str:
    if seconds is None:
        return "at an unknown time"
    whole = int(abs(seconds))
    if whole < 90:
        return f"{whole}s ago"
    minutes = whole // 60
    if minutes < 90:
        return f"{minutes} min ago"
    hours = minutes // 60
    return f"{hours} h ago"


def clock_view(clock: dict) -> dict:
    """Add the sentence and progress the page shows above the engine clock."""
    view = dict(clock)
    duration_ms = int(clock.get("duration_days") or 0) * DAY_MS
    elapsed = int(clock.get("elapsed_ms") or 0)
    view["fraction"] = 0.0 if duration_ms <= 0 else min(1.0, elapsed / duration_ms)
    if clock.get("started_at_ms") is None:
        view["plain"] = (
            f"The paper clock has not started. It starts the first time the paper process runs "
            f"and then runs for {clock.get('duration_days')} days. Restarting the service does not reset it."
        )
        return view
    start = format_myt(clock["started_at_ms"], seconds=True)
    text = f"Day {clock['calendar_day']} of {clock['duration_days']}. Started {start}."
    if clock.get("complete"):
        text += " The clock window has elapsed."
    view["plain"] = text
    view["started_label"] = start
    return view


def next_decision_windows(now_ms: int, timeout_seconds: int = 3600) -> list[dict]:
    """Next E2 Monday rebalance and E3 daily entry window, in Asia/Kuala_Lumpur.

    The bar closes at 00:00 UTC (08:00 MYT). The review hour ends at 01:00 UTC
    (09:00 MYT). A timeout is a skip. Silence on E2 keeps the current book.
    """
    timeout_ms = int(timeout_seconds) * 1000
    e2_open, e2_state = _monday_window(int(now_ms), timeout_ms)
    e3_open, e3_state = _daily_window(int(now_ms), timeout_ms)
    return [
        _window(
            "e2",
            "E2",
            "Weekly residual rebalance",
            e2_open,
            timeout_ms,
            e2_state,
            _e2_plain(e2_state, e2_open, timeout_ms),
        ),
        _window(
            "e3",
            "E3",
            "Daily 50-day trend entries",
            e3_open,
            timeout_ms,
            e3_state,
            _e3_plain(e3_state, e3_open, timeout_ms),
        ),
    ]


def _window(book, name, title, open_ms, timeout_ms, state, plain) -> dict:
    return {
        "book": book,
        "name": name,
        "title": title,
        "state": state,
        "opens_ms": int(open_ms),
        "deadline_ms": int(open_ms) + int(timeout_ms),
        "opens_label": format_myt(open_ms),
        "deadline_label": format_myt(int(open_ms) + int(timeout_ms)),
        "span_label": format_myt_span(open_ms, timeout_ms),
        "plain": plain,
    }


def _daily_window(now_ms: int, timeout_ms: int) -> tuple[int, str]:
    midnight = floor_day_ms(now_ms)
    if now_ms < midnight + timeout_ms:
        return midnight, "open"
    return midnight + DAY_MS, "upcoming"


def _monday_window(now_ms: int, timeout_ms: int) -> tuple[int, str]:
    midnight = floor_day_ms(now_ms)
    weekday = ms_to_dt(now_ms, UTC).weekday()
    monday = midnight + ((0 - weekday) % 7) * DAY_MS
    if now_ms >= monday + timeout_ms:
        monday += 7 * DAY_MS
    state = "open" if monday <= now_ms < monday + timeout_ms else "upcoming"
    return monday, state


def _e2_plain(state: str, open_ms: int, timeout_ms: int) -> str:
    span = format_myt_span(open_ms, timeout_ms)
    if state == "open":
        return (
            f"E2 decision window is open until {format_myt(open_ms + timeout_ms)}. "
            "Open applies the target book. Skip, or silence, keeps the book you already hold."
        )
    return (
        f"Next E2 rebalance is {span}. "
        "One packet lists the target book and the diff. Open applies it. Silence keeps the current book."
    )


def _e3_plain(state: str, open_ms: int, timeout_ms: int) -> str:
    span = format_myt_span(open_ms, timeout_ms)
    if state == "open":
        return (
            f"E3 entry window is open until {format_myt(open_ms + timeout_ms)}. "
            "New entries need a review. The 3× ATR trail and stops do not."
        )
    return (
        f"Next E3 entry window is {span}. "
        "One packet per new entry. The trail and a stop fill without a review."
    )


def news_view(status) -> dict:
    if status is None or (isinstance(status, str) and not status.strip()):
        return {
            "value": None,
            "blocking": True,
            "tone": "bad",
            "plain": (
                "Entries refused. account.news_status is empty, so the news gate fails closed. "
                "This host is not connected to News Watcher. Stops and other exits still run."
            ),
        }
    text = str(status)
    if text.upper() == "ALLOW":
        return {
            "value": text,
            "blocking": False,
            "tone": "ok",
            "plain": (
                "News gate is ALLOW. That is an assumption in config, not a News Watcher feed. "
                "Entries are not blocked by news."
            ),
        }
    return {
        "value": text,
        "blocking": True,
        "tone": "bad",
        "plain": f"Entries refused. account.news_status is {text}, not ALLOW.",
    }


def kill_view(active: bool, reason: str | None, path: str) -> dict:
    if not active:
        return {
            "active": False,
            "reason": None,
            "path": path,
            "tone": "ok",
            "plain": f"Kill switch is off. {path} is not present.",
        }
    shown = reason or "kill switch file is present"
    return {
        "active": True,
        "reason": shown,
        "path": path,
        "tone": "bad",
        "plain": (
            f"Kill switch is on ({shown}). New entries are blocked. Open paper positions flatten "
            "on the next fresh bar. Removing the file does not cancel an exchange order, because "
            "this process never sent one."
        ),
    }


def stale_view(blocking: bool, reason: str) -> dict:
    if not blocking:
        return {
            "blocking": False,
            "tone": "ok",
            "plain": "New entries are not blocked by stale data. " + reason,
        }
    return {
        "blocking": True,
        "tone": "bad",
        "plain": (
            "New entries are refused because stored data is stale. "
            + reason
            + " Stops still run on bars already in the store."
        ),
    }


def orders_view(mode: str | None) -> dict:
    paper = str(mode or "") == "paper"
    if paper:
        plain = (
            "Orders are off. There is no order client in this repository. "
            "This page cannot place, cancel, approve, or change config."
        )
        tone = "ok"
    else:
        plain = (
            f"Config mode is {mode}. The paper engine exits unless mode is paper. "
            "This page is still read-only and still cannot place an order."
        )
        tone = "bad"
    return {"orders": False, "mode": mode, "paper": paper, "tone": tone, "plain": plain}


def cycle_plain(last_block, news_blocking: bool, recent: bool, detail: str | None) -> str:
    if last_block == "kill_switch":
        text = "The last cycle was blocked by the kill switch."
    elif last_block == "stale_data":
        text = "The last cycle was blocked because stored bars were stale."
    elif last_block == "equity_unreadable":
        text = "The last cycle could not compute paper equity, so new entries were refused."
    elif last_block:
        text = f"The last cycle recorded a block: {last_block}."
    elif news_blocking:
        text = "The last cycle did not record a block. New entries are still refused because the news gate is closed."
    else:
        text = "The last cycle did not record a block."
    if detail and last_block:
        text += " " + str(detail)
    if not recent:
        text += " That cycle is older than three loop intervals."
    return text


def service_view(state: str | None, recent: bool, age_seconds: float | None) -> dict:
    age = age_phrase(age_seconds)
    if state == "active" and recent:
        return {
            "state": "active",
            "tone": "ok",
            "headline": "Paper service is up.",
            "detail": f"chart-analyst-paper.service is active. The last cycle was {age}.",
        }
    if state == "active" and not recent:
        return {
            "state": "active",
            "tone": "bad",
            "headline": "Service is active, but the last cycle is late.",
            "detail": "The unit is running and the database has not been updated inside three loop intervals.",
        }
    if recent:
        if state in (None, "", "unknown"):
            detail = f"systemd is not available to this process. The last cycle was {age}."
            tone = "ok"
        else:
            detail = f"chart-analyst-paper.service is {state}. The database was still updated {age}."
            tone = "warn"
        return {
            "state": state or "unknown",
            "tone": tone,
            "headline": "The last paper cycle is recent.",
            "detail": detail,
        }
    if state in (None, "", "unknown"):
        detail = "systemd is not available here, and the paper account has no recent cycle."
    else:
        detail = f"chart-analyst-paper.service is {state}, and the last cycle is not recent."
    return {
        "state": state or "unknown",
        "tone": "bad",
        "headline": "The paper loop does not look up.",
        "detail": detail,
    }


def interval_plain(interval: str, latest_ms: int | None, now_ms: int, fresh: bool, setups_on: bool) -> str:
    if latest_ms is None:
        extra = " Setups A–D are off, so a missing intraday bar does not block E2 or E3." if interval != "1d" and not setups_on else ""
        return f"No {interval} bars are stored.{extra}"
    age = age_phrase((int(now_ms) - int(latest_ms)) / 1000)
    when = format_myt(latest_ms)
    if interval == "1d":
        if fresh:
            return f"Latest daily close is {when} ({age}). The entry gate still checks every listed name, not only the newest bar."
        return f"Latest daily close is {when} ({age}). That is behind the last 00:00 UTC close."
    if fresh:
        tail = " E2 and E3 do not decide on this bar." if not setups_on else ""
        return f"Latest {interval} close is {when} ({age}).{tail}"
    return f"Latest {interval} close is {when} ({age}). That is older than the intraday freshness window."


def refresh_plain(process: dict | None, *, weight: int, pause_above: int) -> str:
    budget = f"Budget is {weight} weight per minute. fapi pauses when the shared IP is already above {pause_above}."
    if not process:
        return (
            "The paper loop has not written a refresh snapshot yet. Counters appear after it runs with --refresh. "
            + budget
        )
    if process.get("unreadable"):
        return "The refresh snapshot file is unreadable. " + budget
    totals = process.get("totals") or {}
    paused = bool(process.get("paused"))
    reason = process.get("pause_reason")
    if paused and reason == "http_429":
        state = "fapi is paused after HTTP 429. The loop does not retry in a tight loop. Stops still run on bars already stored."
    elif paused and reason == "http_418":
        state = "fapi is paused after HTTP 418. The loop waits out the ban. Stops still run on bars already stored."
    elif paused:
        state = f"fapi is paused ({reason or 'budget'}). Vision can still fill older days. New entries wait for a current daily bar."
    else:
        state = "Refresh is not paused."
    counts = (
        f" Since this file was created: {int(totals.get('vision_files') or 0)} vision files, "
        f"{int(totals.get('fapi_requests') or 0)} fapi requests, "
        f"{int(totals.get('pauses') or 0)} pauses, "
        f"{int(totals.get('http_429') or 0)} HTTP 429, "
        f"{int(totals.get('http_418') or 0)} HTTP 418."
    )
    stale = ""
    if process.get("stale"):
        stale = " The snapshot itself is old, so the refresh thread may be stuck."
    return state + counts + " " + budget + stale


def outbox_plain(enabled: bool, counts: dict) -> str:
    pending = int(counts.get("pending") or 0)
    dead = int(counts.get("dead") or 0)
    delivered = int(counts.get("delivered") or 0)
    if enabled:
        base = "Webhook delivery is on. This page does not post or retry."
    else:
        base = "Webhook delivery is off. Rows already stored stay in the outbox. This page does not drain them."
    return f"{base} Pending {pending}, delivered {delivered}, dead {dead}."


def signal_hint(review_status: str, packet_kind: str | None) -> str:
    if review_status == "pending" and packet_kind == "rebalance":
        return "Waiting for a review. Silence keeps the current book."
    if review_status == "pending":
        return "Waiting for a review. Silence becomes a skip when the hour ends."
    if review_status == "timeout":
        return "The hour passed with no open. Stored as skip."
    if review_status == "open":
        return "Opened. The paper loop may simulate the fill while the window is still open."
    if review_status == "skip":
        return "Skipped. The paper loop will not open this packet."
    if review_status == "rejected":
        return "Rejected by a hard gate before it could be opened."
    return ""


def review_status_of(packet: dict, review: dict | None, now_ms: int, timeout_ms: int) -> str:
    if review is not None:
        if review.get("reason") == "decision_timeout" or review.get("requested_decision") == "timeout":
            return "timeout"
        if review.get("effective_decision") == "open":
            return "open"
        if review.get("effective_decision") == "skip":
            return "skip"
        return "pending"
    if packet.get("status") == "rejected":
        return "rejected"
    ts = int(packet.get("ts_ms") or 0)
    if int(now_ms) - ts >= int(timeout_ms):
        return "timeout"
    return "pending"


def drift_plain(drift: dict, elapsed_days: float, bands: dict) -> str:
    min_days = float(bands.get("min_days") or 0)
    tail = " The paper loop journals a drift row when that set changes. This page does not."
    if elapsed_days < min_days:
        return (
            f"Drift alerts start after {min_days:.0f} days. "
            f"This clock has run {elapsed_days:.1f} days, so a gap is not an alert yet."
        )
    alerts = list(drift.get("alerts") or [])
    if not alerts:
        return (
            "Inside the hold-out band "
            f"(turnover within {float(bands.get('turnover_rel') or 0.5):.0%}, "
            f"slippage within {float(bands.get('slippage_rel') or 0.5):.0%}, "
            f"return within {float(bands.get('return_abs') or 0.05):.0%} absolute)."
            + tail
        )
    return "Outside the hold-out band on " + ", ".join(alerts) + "." + tail


def journal_summary(event: str, payload) -> str:
    if not isinstance(payload, dict):
        return event
    if event == "signal":
        packet = payload.get("packet") if isinstance(payload.get("packet"), dict) else {}
        return " ".join(
            part
            for part in (packet.get("symbol"), packet.get("setup"), packet.get("direction"), packet.get("status"))
            if part
        )
    if event == "decision":
        text = str(payload.get("effective") or "undecided")
        if payload.get("reason"):
            text += f" ({payload.get('reason')})"
        if payload.get("reasoning"):
            text += f": {payload.get('reasoning')}"
        return text
    if event == "fill":
        return f"fill {payload.get('fill_price')} size {payload.get('size')} {payload.get('book') or ''}".strip()
    if event == "exit":
        return f"{payload.get('reason') or 'exit'} pnl {payload.get('pnl')} R {payload.get('r_multiple')}"
    if event == "resolution":
        return f"{payload.get('outcome') or 'resolution'}: {payload.get('reason') or ''}".strip().rstrip(":")
    if event == "violation":
        return f"{payload.get('kind') or 'violation'}: {payload.get('detail') or ''}".strip().rstrip(":")
    if event == "gates":
        return " ".join(part for part in (payload.get("decision_path"), payload.get("status")) if part) or "gates"
    if event in ("drift", "book-drift"):
        alerts = payload.get("alerts") or []
        return "drift " + ", ".join(str(item) for item in alerts) if alerts else "drift cleared"
    if event == "context":
        context = payload.get("market_context") if isinstance(payload.get("market_context"), dict) else {}
        return context.get("summary") or "market context"
    return event


def journal_labels(payload) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    packet = payload.get("packet") if isinstance(payload.get("packet"), dict) else {}
    symbol = payload.get("symbol") or packet.get("symbol")
    book = payload.get("book") or packet.get("book")
    return (None if symbol is None else str(symbol), None if book is None else str(book))


def filter_journal(rows: list[dict], *, book: str = "", symbol: str = "", event: str = "", text: str = "") -> list[dict]:
    book = (book or "").strip().lower()
    symbol = (symbol or "").strip().upper()
    event = (event or "").strip().lower()
    text = (text or "").strip().lower()
    kept = []
    for row in rows:
        if book and book != str(row.get("book") or "").lower():
            continue
        if symbol and symbol != str(row.get("symbol") or "").upper():
            continue
        if event and event != str(row.get("event") or "").lower():
            continue
        if text:
            blob = " ".join(
                str(row.get(key) or "") for key in ("summary", "signal_id", "symbol", "book", "event")
            ).lower()
            if text not in blob:
                continue
        kept.append(row)
    return kept


def parse_strategy_report(text: str) -> dict:
    """Pull the hold-out sentence from a strategies report. Missing sections are omitted."""
    found = {}
    for section in re.split(r"\n### ", "\n" + text):
        match = re.match(r"(E[1-6])\b", section)
        if not match:
            continue
        family = match.group(1)
        hold = _HOLD_RE.search(section)
        frozen = _FROZEN_RE.search(section)
        if hold is None and frozen is None:
            continue
        row = {"family": family, "variant": frozen.group(1) if frozen else None}
        if hold is not None:
            row.update(
                {
                    "cagr": float(hold.group("cagr")) / 100.0,
                    "sharpe": float(hold.group("sharpe")),
                    "max_dd": float(hold.group("max_dd")) / 100.0,
                    "total_return": float(hold.group("total_return")) / 100.0,
                    "turnover_per_year": float(hold.group("turnover")),
                    "fees_usdt": float(hold.group("fees").replace(",", "")),
                    "slippage_usdt": float(hold.group("slippage").replace(",", "")),
                    "trades": int(hold.group("trades").replace(",", "")),
                    "expectancy_r": float(hold.group("expectancy_r")),
                }
            )
        found[family] = row
    return found


def sensitive_key(key: str) -> bool:
    lowered = str(key).lower()
    if lowered in _SAFE_KEYS:
        return False
    return any(part in lowered for part in ("secret", "password", "api_key", "token", "credential", "webhook_url"))


def scrub(value, denylist: set[str]):
    """Drop credential-shaped keys and replace known secret strings."""
    if isinstance(value, str):
        out = value
        for secret in denylist:
            if secret and secret in out:
                out = out.replace(secret, "[redacted]")
        return out
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if sensitive_key(str(key)):
                continue
            cleaned[str(key)] = scrub(item, denylist)
        return cleaned
    if isinstance(value, list):
        return [scrub(item, denylist) for item in value]
    return value


def is_loopback(host: str) -> bool:
    text = (host or "").strip().lower()
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def json_safe(value):
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return float(value)
    if isinstance(value, str):
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return json_safe(item())
        except Exception:
            return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)
