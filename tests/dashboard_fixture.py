"""A small paper database with the shapes the live desk writes.

The clock start matches the desk's paper start, 2026-11-11 05:13:38 UTC.
Nothing here places an order. The outbox body contains a marker the dashboard
must not render.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from desk.data.store import Store
from desk.paper.books import BookPosition, dump_book_position
from desk.paper.clock import myt_week_key
from desk.timeutils import DAY_MS, HOUR_MS, last_closed_daily_open, ms_to_utc_iso, myt_date

CLOCK_START = datetime(2026, 11, 11, 5, 13, 38, tzinfo=timezone.utc)
BODY_MARKER = "BODY_SECRET_MARKER"


def clock_start_ms() -> int:
    return int(CLOCK_START.timestamp() * 1000)


def write_fixture(directory: str | Path, now_ms: int | None = None) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    db_path = directory / "desk.sqlite"
    for suffix in ("", "-wal", "-shm"):
        leftover = Path(str(db_path) + suffix) if suffix else db_path
        if leftover.exists():
            leftover.unlink()
    now = int(time.time() * 1000 if now_ms is None else now_ms)
    start = clock_start_ms()
    store = Store(db_path)
    try:
        _seed(store, directory, start, now)
    finally:
        store.close()
    return db_path


def _seed(store: Store, directory: Path, start: int, now: int) -> None:
    expected_close = last_closed_daily_open(now) + DAY_MS
    daily_open = expected_close - DAY_MS
    daily_close = expected_close - 1
    marks = {
        "BTCUSDT": 95500.0,
        "ETHUSDT": 3460.0,
        "SOLUSDT": 176.0,
        "BNBUSDT": 608.0,
        "AVAXUSDT": 36.2,
    }
    symbols = []
    for name in marks:
        symbols.append(_symbol(name, "TRADING", start))
    symbols.append(_symbol("OLDUSDT", "DELISTED", start))
    store.upsert_symbols(symbols, now)
    for name, mark in marks.items():
        store.upsert_klines(name, "1d", [_bar(daily_open, daily_close, mark)])
    old_open = daily_open - 3 * DAY_MS
    store.upsert_klines("OLDUSDT", "1d", [_bar(old_open, old_open + DAY_MS - 1, 1.0)])
    _btc_hours(store, start, now)
    for name, day in (("BTCUSDT", "2026-11-10"), ("ETHUSDT", "2026-11-10"), ("SOLUSDT", "2026-11-09")):
        store.mark_vision_day(name, "1d", day, 1)

    _book(store, "e2", "BTCUSDT", 1, 0.004, 94800.0, 93000.0, marks["BTCUSDT"], 7.2, "btc", "e2-rebalance-pending", "e2_residual_momentum", "rebalance", None, daily_open, start, now)
    _book(store, "e2", "ETHUSDT", -1, -0.05, 3500.0, 3620.0, marks["ETHUSDT"], 6.0, "eth", "e2-rebalance-pending", "e2_residual_momentum", "rebalance", None, daily_open, start, now)
    _book(store, "e3", "SOLUSDT", 1, 1.5, 180.0, 172.0, marks["SOLUSDT"], 12.0, "alts", "e3-sol-open", "e3_slow_trend", "trail", 3.0, daily_open, start, now)
    _book(store, "e3", "BNBUSDT", 1, 0.25, 600.0, 584.0, marks["BNBUSDT"], 4.0, "l1", "e3-bnb-skip", "e3_slow_trend", "trail", 3.0, daily_open, start, now)

    exit_ms = start + min(2 * HOUR_MS, max(1_000, (now - start) // 2))
    store.save_paper_trade(
        {
            "signal_id": "e3-avax-exit",
            "symbol": "AVAXUSDT",
            "book": "e3",
            "setup": "e3_slow_trend",
            "direction": "long",
            "pnl": 6.4,
            "r_multiple": 0.45,
            "fees": 0.18,
            "funding": 0.04,
            "slippage": 0.22,
            "turnover": 90.0,
            "exit_reason": "trail",
            "exit_price": 36.4,
            "exit_time": exit_ms,
            "entry_time": start,
        }
    )
    span = max(1, now - start)
    for ts, equity in (
        (start, 800.0),
        (start + span // 3, 796.0),
        (start + (2 * span) // 3, 801.0),
        (now, 807.2),
    ):
        store.append_equity_point(ts, equity)
    store.save_paper_account(
        {
            "initial_equity": 800.0,
            "equity_usdt": 807.2,
            "day_start_equity": 800.0,
            "week_start_equity": 800.0,
            "day_key": str(myt_date(now)),
            "week_key": myt_week_key(now),
            "as_of_ms": now - 20_000,
            "started_at_ms": start,
            "last_block": None,
            "updated_ms": now - 20_000,
        }
    )
    _signals(store, now)
    _journal(store, now, exit_ms)
    (directory / "refresh_status.json").write_text(
        json.dumps(
            {
                "v": 1,
                "updated_ms": now - 4_000,
                "paused": False,
                "pause_reason": None,
                "last_slice": {
                    "vision_files": 3,
                    "vision_rows": 3,
                    "fapi_requests": 1,
                    "fapi_rows": 1,
                    "funding_files": 0,
                    "errors": 0,
                    "pending": 2,
                    "inactive": 0,
                    "http": 4,
                    "paused": False,
                    "pause_reason": None,
                },
                "totals": {
                    "vision_files": 48,
                    "vision_rows": 48,
                    "fapi_requests": 6,
                    "fapi_rows": 5,
                    "funding_requests": 1,
                    "errors": 0,
                    "pauses": 2,
                    "http_429": 1,
                    "http_418": 0,
                    "slices": 40,
                },
            }
        ),
        encoding="utf-8",
    )


def _symbol(name: str, status: str, start: int) -> dict:
    base = name[: -len("USDT")] if name.endswith("USDT") else name
    return {
        "symbol": name,
        "status": status,
        "contract_type": "PERPETUAL",
        "base_asset": base,
        "quote_asset": "USDT",
        "margin_asset": "USDT",
        "onboard_date_ms": start - 400 * DAY_MS,
    }


def _bar(open_time: int, close_time: int, price: float) -> dict:
    return {
        "open_time": int(open_time),
        "close_time": int(close_time),
        "open": price,
        "high": price * 1.01,
        "low": price * 0.99,
        "close": price,
        "volume": 1000.0,
        "quote_volume": 50_000_000.0,
    }


def _btc_hours(store: Store, start: int, now: int) -> None:
    first = (start - HOUR_MS) - ((start - HOUR_MS) % HOUR_MS)
    prices = [95100.0, 95500.0, 95800.0, 96100.0, 96400.0, 96600.0, 96800.0, 97000.0]
    hour = first
    index = 0
    while hour < now and index < len(prices):
        close_time = hour + HOUR_MS - 1
        if close_time <= now:
            store.upsert_klines("BTCUSDT", "1h", [_bar(hour, close_time, prices[index])])
            index += 1
        hour += HOUR_MS


def _book(store, book, symbol, side, qty, entry, stop, mark, risk, cluster, signal_id, setup, exit_mode, trail, daily_open, start, now) -> None:
    pos = BookPosition(
        book=book,
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=entry,
        stop_price=stop,
        trail_mult=trail,
        exit_mode=exit_mode,
        cluster=cluster,
        risk_usdt=risk,
        stop_pct=abs(entry - stop) / entry,
        decision_atr=abs(entry - stop),
        last_mark=mark,
        entry_time=start,
        opened_day_ms=daily_open,
        cash_impact=-(qty * entry),
        fees=0.0,
        slippage=0.35,
        funding=0.0,
        turnover=abs(qty) * entry,
        notional=abs(qty) * entry,
        initial_risk=risk,
        signal_id=signal_id,
        sticky=False,
        setup=setup,
        last_day_ms=daily_open,
    )
    store.save_book_position(book, symbol, dump_book_position(pos), now)


def _packet(signal_id: str, **extra) -> dict:
    packet = {
        "signal_id": signal_id,
        "dedupe_key": signal_id,
        "symbol": "BTCUSDT",
        "setup": "e2_residual_momentum",
        "direction": "long",
        "ts_ms": 0,
        "status": "proposed",
        "master_decision": {"decision": None, "reasoning": None, "ts": None},
        "hard_gates": {"news": {"result": "pass", "reason": "News Watcher ALLOW"}},
        "decision_path": "pending_master",
        "risk_usdt": 8.0,
        "notional_usdt": 240.0,
        "targets": [],
        "diffs": {},
    }
    packet.update(extra)
    return packet


def _signals(store: Store, now: int) -> None:
    pending = _packet(
        "e2-rebalance-pending",
        symbol="BTCUSDT",
        book="e2",
        packet_kind="rebalance",
        variant="e2_r15_b60_v30_top25",
        ts_ms=now - 15 * 60 * 1000,
        notional_usdt=560.0,
        risk_usdt=13.2,
        targets=[
            {"symbol": "BTCUSDT", "side": "long", "notional": 382.0, "risk_usdt": 7.2},
            {"symbol": "ETHUSDT", "side": "short", "notional": 173.0, "risk_usdt": 6.0},
        ],
        diffs={"open": [{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}], "close": [{"symbol": "AVAXUSDT"}]},
    )
    opened = _packet(
        "e3-sol-open",
        symbol="SOLUSDT",
        setup="e3_slow_trend",
        book="e3",
        packet_kind="book_entry",
        variant="e3a_sma50_trail3_top40",
        ts_ms=now - 30 * 60 * 1000,
        direction="long",
        risk_usdt=12.0,
        notional_usdt=264.0,
        targets=[{"symbol": "SOLUSDT", "side": "long", "notional": 264.0, "risk_usdt": 12.0}],
    )
    skipped = _packet(
        "e3-bnb-skip",
        symbol="BNBUSDT",
        setup="e3_slow_trend",
        book="e3",
        packet_kind="book_entry",
        variant="e3a_sma50_trail3_top40",
        ts_ms=now - 40 * 60 * 1000,
        direction="long",
        risk_usdt=4.0,
    )
    timed = _packet(
        "e3-old-timeout",
        symbol="ADAUSDT",
        setup="e3_slow_trend",
        book="e3",
        packet_kind="book_entry",
        variant="e3a_sma50_trail3_top40",
        ts_ms=now - 2 * HOUR_MS,
        direction="long",
    )
    rejected = _packet(
        "e2-rejected-news",
        book="e2",
        packet_kind="rebalance",
        variant="e2_r15_b60_v30_top25",
        ts_ms=now - 50 * 60 * 1000,
        status="rejected",
        decision_path="hard_gate_reject",
        hard_gates={"news": {"result": "fail", "reason": "News Watcher status is missing"}},
    )
    for packet in (pending, opened, skipped, timed, rejected):
        store.save_signal(packet)
    _review(store, "e3-sol-open", "open", "open", "reviewer_open", "daily close held above the 50-day", now - 20 * 60 * 1000)
    _review(store, "e3-bnb-skip", "skip", "skip", "reviewer_skip", "cluster already holds the risk I want", now - 25 * 60 * 1000)
    _review(store, "e3-old-timeout", "timeout", "skip", "decision_timeout", "decision timeout; fail closed", now - 30 * 60 * 1000)
    store.insert_outbox(_outbox("e3-sol-open", "pending", now, None))
    store.insert_outbox(_outbox("e3-bnb-skip", "dead", now, "webhook disabled"))
    store.insert_outbox(_outbox("e3-old-timeout", "delivered", now, None, delivered=now - 10 * 60 * 1000))


def _review(store, signal_id, requested, effective, reason, reasoning, ts) -> None:
    store.append_review(
        {
            "signal_id": signal_id,
            "ts_ms": ts,
            "market_context": {
                "summary": None,
                "strengtheners": [],
                "weakeners": [],
                "onchain_checks": {},
                "sources": [],
                "ts": None,
            },
            "requested_decision": requested,
            "effective_decision": effective,
            "reasoning": reasoning,
            "decision_ts": ms_to_utc_iso(ts),
            "override_refused": requested == "open" and effective != "open",
            "reason": reason,
        }
    )


def _outbox(signal_id, status, now, error, delivered=None) -> dict:
    return {
        "signal_id": signal_id,
        "idempotency_key": signal_id,
        "body": json.dumps({"signal_id": signal_id, "marker": BODY_MARKER}),
        "status": status,
        "attempts": 0 if status == "pending" else 1,
        "next_attempt_ms": now,
        "last_error": error,
        "created_ms": now - HOUR_MS,
        "updated_ms": now - 5 * 60 * 1000,
        "delivered_ms": delivered,
    }


def _journal(store: Store, now: int, exit_ms: int) -> None:
    store.append_decision_journal(
        now - 15 * 60 * 1000,
        "e2-rebalance-pending",
        "signal",
        {"packet": {"symbol": "BTCUSDT", "setup": "e2_residual_momentum", "direction": "long", "status": "proposed", "book": "e2"}},
    )
    store.append_decision_journal(
        now - 20 * 60 * 1000,
        "e3-sol-open",
        "decision",
        {"effective": "open", "requested": "open", "reason": "reviewer_open", "reasoning": "daily close held above the 50-day", "book": "e3", "symbol": "SOLUSDT"},
    )
    store.append_decision_journal(
        now - 18 * 60 * 1000,
        "e3-sol-open",
        "fill",
        {"fill_price": 180.2, "size": 1.5, "fee": 0.14, "stop": 172.0, "book": "e3", "symbol": "SOLUSDT"},
    )
    store.append_decision_journal(
        now - 25 * 60 * 1000,
        "e3-bnb-skip",
        "decision",
        {"effective": "skip", "requested": "skip", "reason": "reviewer_skip", "reasoning": "cluster already holds the risk I want", "book": "e3", "symbol": "BNBUSDT"},
    )
    store.append_decision_journal(
        exit_ms,
        "e3-avax-exit",
        "exit",
        {"reason": "trail", "pnl": 6.4, "r_multiple": 0.45, "book": "e3", "symbol": "AVAXUSDT"},
    )
