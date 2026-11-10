"""One paper cycle: closed bars, simulated fills, fail closed.

The scanner decides whether a setup fired. This module does not name setups.
Fills use the backtester's fee, slippage, and funding. There is no order
client here. ``mode: live`` is refused before any of that runs.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from desk.backtest.costs import directional_pnl, taker_fee
from desk.backtest.replay import (
    apply_funding_between,
    force_exit,
    manage_bar,
    position_risk_usdt,
    simulate_entry,
    trade_record,
)
from desk.config import assert_no_live_orders
from desk.delivery.eligibility import entry_blocks, expire_pending
from desk.delivery.outbox import drain_outbox
from desk.indicators.core import add_indicators, ema_column
from desk.journal.decision import DecisionJournal
from desk.paper.books import book_contribution, fill_books, propose_books, reduce_books
from desk.paper.clock import myt_week_key
from desk.paper.scorecard import build_scorecard, note_drift
from desk.paper.state import dump_position, load_position
from desk.rules.risk_gates import _as_float
from desk.scanner.service import scan_once
from desk.timeutils import DAY_MS, H4_MS, HOUR_MS, myt_date


def kill_switch_path(settings: dict) -> Path:
    raw = os.environ.get("KILL_SWITCH_PATH") or settings["paper"]["kill_switch_path"]
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def kill_switch_reason(settings: dict) -> str | None:
    path = kill_switch_path(settings)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "kill switch file is unreadable"
    return text or "kill switch file is present"


def cycle_symbols(settings: dict, store, symbols: list[str] | None) -> list[str]:
    if symbols:
        return list(symbols)
    configured = list(settings["data"].get("symbols") or [])
    if configured:
        return configured
    return store.symbols_with_interval("1h")


def data_is_fresh(store, symbols: list[str], now_ms: int, settings: dict) -> tuple[bool, str]:
    if not symbols:
        return False, "no symbols to check"
    max_age = int(settings["paper"]["max_data_age_seconds"]) * 1000
    stale = []
    for symbol in symbols:
        frame = store.klines(symbol, "1h", end_ms=now_ms)
        if frame.empty:
            stale.append(symbol)
            continue
        close_time = int(frame.iloc[-1]["close_time"])
        if int(now_ms) - close_time > max_age:
            stale.append(symbol)
    if stale:
        return False, "stale or missing closed 1h bars: " + ",".join(stale)
    return True, "closed 1h bars are fresh"


def ensure_paper_account(store, settings: dict, now_ms: int) -> dict:
    existing = store.paper_account()
    if existing is not None:
        return existing
    initial = _as_float(settings["paper"].get("initial_equity"))
    if initial is not None and initial <= 0:
        initial = None
    row = {
        "initial_equity": initial,
        "equity_usdt": initial,
        "day_start_equity": initial,
        "week_start_equity": initial,
        "day_key": str(myt_date(now_ms)),
        "week_key": myt_week_key(now_ms),
        "as_of_ms": int(now_ms) if initial is not None else None,
        "started_at_ms": int(now_ms),
        "last_block": None,
        "updated_ms": int(now_ms),
    }
    store.save_paper_account(row)
    if initial is not None:
        store.append_equity_point(now_ms, initial)
    return row


def load_positions(store) -> list:
    return [load_position(row["payload"]) for row in store.paper_position_rows()]


def position_view(pos) -> dict:
    return {
        "symbol": pos.symbol,
        "setup": pos.setup,
        "direction": pos.direction,
        "cluster_key": pos.cluster_key,
        "risk_usdt": position_risk_usdt(pos),
        "notional_usdt": abs(float(pos.remaining) * float(pos.fill)),
    }


def marked_equity(store, settings: dict, now_ms: int) -> dict | None:
    """Mark the paper book. None means the equity read failed closed."""
    account = store.paper_account()
    if account is None:
        return None
    initial = _as_float(account.get("initial_equity"))
    if initial is None or initial <= 0:
        return None
    realized = 0.0
    for trade in store.list_paper_trades():
        pnl = _as_float(trade.get("pnl"))
        if pnl is None:
            return None
        realized += pnl
    unrealized = 0.0
    open_realized = 0.0
    views = []
    for pos in load_positions(store):
        frame = store.klines(pos.symbol, "1h", end_ms=now_ms)
        if frame.empty:
            return None
        mark = _as_float(frame.iloc[-1]["close"])
        if mark is None:
            return None
        unrealized += directional_pnl(pos.direction, pos.remaining, pos.fill, mark)
        open_realized += float(pos.realized)
        views.append(position_view(pos))
    books = book_contribution(store, now_ms)
    if books is None:
        return None
    book_pnl, book_views = books
    views.extend(book_views)
    equity = initial + realized + open_realized + unrealized + book_pnl
    day_key = str(myt_date(now_ms))
    week_key = myt_week_key(now_ms)
    day_start = _as_float(account.get("day_start_equity"))
    week_start = _as_float(account.get("week_start_equity"))
    if account.get("day_key") != day_key:
        previous = store.latest_equity_point()
        day_start = equity if previous is None else previous
    if account.get("week_key") != week_key:
        previous = store.latest_equity_point()
        week_start = equity if previous is None else previous
    if day_start is None or day_start <= 0 or week_start is None or week_start <= 0:
        return None
    return {
        "equity_usdt": equity,
        "daily_pnl_pct": (equity - day_start) / day_start,
        "weekly_pnl_pct": (equity - week_start) / week_start,
        "news_status": settings["account"].get("news_status"),
        "open_positions": views,
        "day_key": day_key,
        "week_key": week_key,
        "day_start_equity": day_start,
        "week_start_equity": week_start,
        "as_of_ms": int(now_ms),
    }


def review_account(store, settings: dict, now_ms: int) -> dict | None:
    """Equity for a review. A broken ledger does not fall back to the config number."""
    if store.paper_account() is not None:
        return marked_equity(store, settings, now_ms)
    initial = _as_float(settings["paper"].get("initial_equity"))
    if initial is None or initial <= 0:
        return None
    return {
        "equity_usdt": initial,
        "daily_pnl_pct": 0.0,
        "weekly_pnl_pct": 0.0,
        "news_status": settings["account"].get("news_status"),
        "open_positions": [],
    }


def _persist_mark(store, snapshot: dict, now_ms: int, last_block: str | None) -> None:
    current = store.paper_account() or {}
    store.save_paper_account(
        {
            "initial_equity": current.get("initial_equity"),
            "equity_usdt": snapshot["equity_usdt"],
            "day_start_equity": snapshot["day_start_equity"],
            "week_start_equity": snapshot["week_start_equity"],
            "day_key": snapshot["day_key"],
            "week_key": snapshot["week_key"],
            "as_of_ms": int(now_ms),
            "started_at_ms": current.get("started_at_ms"),
            "last_block": last_block,
            "updated_ms": int(now_ms),
        }
    )
    store.append_equity_point(now_ms, snapshot["equity_usdt"])


def _note_safety(store, now_ms: int, block: str | None, detail: str) -> str | None:
    current = store.paper_account() or {}
    previous = current.get("last_block")
    if block == previous:
        return previous
    if block is not None:
        store.append_paper_event(now_ms, "safety", block, None, detail)
    return block


def _journal_exit(journal: DecisionJournal, trade: dict, now_ms: int) -> None:
    journal.record_event(
        now_ms,
        trade["signal_id"],
        "exit",
        {
            "exit_price": trade.get("exit_price"),
            "reason": trade.get("exit_reason"),
            "pnl": trade.get("pnl"),
            "r_multiple": trade.get("r_multiple"),
            "fees": trade.get("fees"),
            "funding": trade.get("funding"),
            "ts_ms": trade.get("exit_time"),
        },
    )


def _resolve(store, journal: DecisionJournal, now_ms: int, signal_id: str, outcome: str, reason: str) -> None:
    store.save_resolution(signal_id, outcome, now_ms, reason)
    journal.record_event(now_ms, signal_id, "resolution", {"outcome": outcome, "reason": reason})


def manage_positions(store, settings: dict, now_ms: int) -> list[dict]:
    closed = []
    ema_fast = ema_column(settings, "fast")
    ema_slow = ema_column(settings, "slow")
    for pos in load_positions(store):
        df1h = _ready(store.klines(pos.symbol, "1h", end_ms=now_ms), settings)
        df4h = _ready(store.klines(pos.symbol, "4h", end_ms=now_ms), settings)
        df1d = _ready(store.klines(pos.symbol, "1d", end_ms=now_ms), settings)
        funding = store.funding(pos.symbol)
        last_close = int(pos.meta.get("last_bar_close_ms") or pos.entry_time)
        prev_close = last_close
        changed = False
        if df1h is None or df1h.empty:
            continue
        for _, bar in df1h.iterrows():
            bar_close = int(bar["close_time"])
            if bar_close <= last_close:
                continue
            open_time = int(bar["open_time"])
            row4h, recent_highs, recent_lows = _four_hour(df4h, open_time, settings)
            row1d = _daily(df1d, open_time)
            reason = manage_bar(
                pos, bar, row4h, row1d, settings, ema_fast, ema_slow, recent_highs, recent_lows
            )
            if reason is None and pos.remaining > 1e-12:
                apply_funding_between(pos, funding, prev_close, bar_close, float(bar["close"]))
            prev_close = bar_close
            pos.meta["last_bar_close_ms"] = bar_close
            changed = True
            if reason is not None or pos.remaining <= 1e-12:
                trade = trade_record(pos, bar_close, reason or "flat")
                store.delete_paper_position(pos.symbol)
                store.save_paper_trade(trade)
                if reason == "hard_stop":
                    until = bar_close + int(settings["risk"]["cooldown_days"]) * DAY_MS
                    store.set_cooldown(pos.symbol, until, "hard_stop")
                closed.append(trade)
                changed = False
                break
        if changed:
            store.save_paper_position(pos.symbol, dump_position(pos), now_ms)
    return closed


def flatten_open(store, settings: dict, now_ms: int, fresh_symbols: set[str]) -> tuple[list[dict], list[str]]:
    closed = []
    waiting = []
    for pos in load_positions(store):
        if pos.symbol not in fresh_symbols:
            waiting.append(pos.symbol)
            continue
        frame = store.klines(pos.symbol, "1h", end_ms=now_ms)
        if frame.empty:
            waiting.append(pos.symbol)
            continue
        last = frame.iloc[-1]
        trade = force_exit(pos, float(last["close"]), int(last["close_time"]), settings, "kill_switch")
        store.delete_paper_position(pos.symbol)
        store.save_paper_trade(trade)
        closed.append(trade)
    return closed, waiting


def fresh_symbols(store, symbols: list[str], now_ms: int, settings: dict) -> set[str]:
    max_age = int(settings["paper"]["max_data_age_seconds"]) * 1000
    ready = set()
    for symbol in symbols:
        frame = store.klines(symbol, "1h", end_ms=now_ms)
        if frame.empty:
            continue
        if int(now_ms) - int(frame.iloc[-1]["close_time"]) <= max_age:
            ready.add(symbol)
    return ready


def fill_pending(store, settings: dict, now_ms: int, snapshot: dict, journal: DecisionJournal) -> list[dict]:
    filled = []
    positions = list(snapshot["open_positions"])
    held = {item["symbol"] for item in positions}
    account = {
        "equity_usdt": snapshot["equity_usdt"],
        "daily_pnl_pct": snapshot["daily_pnl_pct"],
        "weekly_pnl_pct": snapshot["weekly_pnl_pct"],
        "news_status": snapshot["news_status"],
    }
    for packet in store.list_signal_packets(status="proposed"):
        if packet.get("packet_kind") in ("rebalance", "book_entry"):
            continue
        signal_id = packet["signal_id"]
        if store.resolution(signal_id) is not None:
            continue
        review = store.latest_review(signal_id)
        if review is None or review["effective_decision"] != "open":
            continue
        if packet["symbol"] in held:
            _refuse(store, journal, now_ms, signal_id, "conflicting_position", "symbol already has an open paper position")
            continue
        timeout_ms = int(settings["decision"]["timeout_seconds"]) * 1000
        if int(now_ms) - int(packet.get("ts_ms") or 0) > timeout_ms:
            _refuse(store, journal, now_ms, signal_id, "stale_signal", "signal is outside the decision window")
            continue
        blocks = entry_blocks(packet, account, positions, settings, now_ms)
        if blocks:
            kind = "live_gate_block"
            detail = "fill refused by " + ", ".join(blocks)
            store.append_paper_event(now_ms, "violation", kind, signal_id, detail)
            journal.record_event(now_ms, signal_id, "violation", {"kind": kind, "detail": detail, "class": "violation"})
            _resolve(store, journal, now_ms, signal_id, "refused", detail)
            continue
        pos = simulate_entry(packet, 0, settings)
        if pos is None:
            _refuse(store, journal, now_ms, signal_id, "fill_crossed_stop", "slippage crossed the stop; entry cancelled")
            continue
        pos.meta["last_bar_close_ms"] = int(packet["ts_ms"])
        store.save_paper_position(pos.symbol, dump_position(pos), now_ms)
        fee = taker_fee(pos.fill * pos.size, float(settings["costs"]["taker_fee_rate"]))
        journal.record_event(
            now_ms,
            signal_id,
            "fill",
            {
                "fill_price": pos.fill,
                "size": pos.size,
                "fee": fee,
                "stop": pos.stop,
                "ts_ms": int(packet["ts_ms"]),
            },
        )
        _resolve(store, journal, now_ms, signal_id, "filled", "simulated fill")
        view = position_view(pos)
        positions.append(view)
        held.add(pos.symbol)
        filled.append({"signal_id": signal_id, "symbol": pos.symbol, "fill_price": pos.fill, "fee": fee})
    return filled


def refuse_pending_opens(store, settings: dict, now_ms: int, journal: DecisionJournal, reason: str) -> list[str]:
    refused = []
    for packet in store.list_signal_packets(status="proposed"):
        signal_id = packet["signal_id"]
        if store.resolution(signal_id) is not None:
            continue
        review = store.latest_review(signal_id)
        if review is None or review["effective_decision"] != "open":
            continue
        _refuse(store, journal, now_ms, signal_id, reason, reason)
        refused.append(signal_id)
    return refused


def _refuse(store, journal, now_ms, signal_id, reason, detail) -> None:
    _resolve(store, journal, now_ms, signal_id, "refused", detail)


def run_cycle(
    settings: dict,
    store,
    *,
    now_ms: int,
    symbols: list[str] | None = None,
    session=None,
    onchain_raw: dict | None = None,
) -> dict:
    assert_no_live_orders(settings)
    with store.lock:
        ensure_paper_account(store, settings, now_ms)
        journal = DecisionJournal(store, settings)
        chosen = cycle_symbols(settings, store, symbols)
        for pos in load_positions(store):
            if pos.symbol not in chosen:
                chosen.append(pos.symbol)
        closed = manage_positions(store, settings, now_ms)
        for trade in closed:
            _journal_exit(journal, trade, now_ms)
        book_closed = reduce_books(settings, store, now_ms, journal, kill=None)
        fresh, fresh_reason = data_is_fresh(store, chosen, now_ms, settings)
        kill = kill_switch_reason(settings)
        flattened: list[dict] = []
        if kill and fresh:
            flattened, waiting = flatten_open(store, settings, now_ms, fresh_symbols(store, chosen, now_ms, settings))
            for trade in flattened:
                _journal_exit(journal, trade, now_ms)
            if waiting:
                fresh = False
                fresh_reason = "kill switch waiting for a fresh bar: " + ",".join(waiting)
        if kill:
            book_closed.extend(reduce_books(settings, store, now_ms, journal, kill=kill))
        snapshot = marked_equity(store, settings, now_ms)
        block = None
        block_detail = ""
        if kill:
            block = "kill_switch"
            block_detail = kill
        elif not fresh:
            block = "stale_data"
            block_detail = fresh_reason
        elif snapshot is None:
            block = "equity_unreadable"
            block_detail = "paper equity could not be read"
        last_block = _note_safety(store, now_ms, block, block_detail)
        expired = expire_pending(store, settings, now_ms, journal)
        scanned: list[dict] = []
        filled: list[dict] = []
        book_filled: list[dict] = []
        proposed: list[dict] = []
        refused: list[str] = []
        if block is None and snapshot is not None:
            account = {
                "equity_usdt": snapshot["equity_usdt"],
                "daily_pnl_pct": snapshot["daily_pnl_pct"],
                "weekly_pnl_pct": snapshot["weekly_pnl_pct"],
                "news_status": snapshot["news_status"],
            }
            scanned = scan_once(
                settings,
                store,
                symbols=chosen,
                now_ms=now_ms,
                account=account,
                open_positions=list(snapshot["open_positions"]),
                onchain_raw=onchain_raw,
            )
            snapshot = marked_equity(store, settings, now_ms) or snapshot
            proposed = propose_books(settings, store, now_ms, journal, snapshot)
            snapshot = marked_equity(store, settings, now_ms) or snapshot
            filled = fill_pending(store, settings, now_ms, snapshot, journal)
            snapshot = marked_equity(store, settings, now_ms) or snapshot
            book_filled = fill_books(settings, store, now_ms, journal, snapshot)
        elif block == "kill_switch":
            refused = refuse_pending_opens(store, settings, now_ms, journal, "kill_switch")
        webhook = []
        if settings["webhook"].get("enabled"):
            webhook = drain_outbox(store, settings, now_ms, session=session)
        snapshot = marked_equity(store, settings, now_ms)
        if snapshot is not None:
            _persist_mark(store, snapshot, now_ms, last_block)
        else:
            current = store.paper_account() or {}
            current["last_block"] = last_block
            current["updated_ms"] = int(now_ms)
            current["as_of_ms"] = None
            current["equity_usdt"] = None
            store.save_paper_account(current)
        scorecard = build_scorecard(store, settings, now_ms)
        note_drift(store, settings, journal, now_ms, scorecard)
        return {
            "cycle_ms": int(now_ms),
            "block": block,
            "block_detail": block_detail,
            "scanned": len(scanned),
            "filled": filled,
            "expired": [row["signal_id"] for row in expired],
            "closed": [trade["signal_id"] for trade in closed + flattened + book_closed],
            "refused": refused,
            "webhook": webhook,
            "books": {
                "proposed": [packet["signal_id"] for packet in proposed],
                "filled": [row["signal_id"] for row in book_filled],
                "exited": [trade["signal_id"] for trade in book_closed],
            },
            "scorecard": scorecard,
        }


def _ready(frame: pd.DataFrame, settings: dict) -> pd.DataFrame:
    if frame is None or frame.empty:
        return frame
    return add_indicators(frame, settings)


def _four_hour(df4h: pd.DataFrame, open_time: int, settings: dict):
    if df4h is None or df4h.empty or open_time % H4_MS != 3 * HOUR_MS:
        return None, None, None
    h4_open = open_time - 3 * HOUR_MS
    matched = df4h[df4h["open_time"] == h4_open]
    if matched.empty:
        return None, None, None
    row = matched.iloc[-1]
    if str(settings["exits"].get("trail_mode", "ema")) != "structure":
        return row, None, None
    from desk.backtest.replay import _structure_arrays

    highs, lows = _structure_arrays(df4h, h4_open, settings)
    return row, highs, lows


def _daily(df1d: pd.DataFrame, open_time: int):
    if df1d is None or df1d.empty or open_time % DAY_MS != 23 * HOUR_MS:
        return None
    matched = df1d[df1d["open_time"] == open_time - 23 * HOUR_MS]
    if matched.empty:
        return None
    return matched.iloc[-1]
