"""Paper books for E2 and E3.

E2 is a weekly residual book. The Sunday UTC daily close (Monday 00:00 UTC,
Monday 08:00 Asia/Kuala_Lumpur) becomes one rebalance packet. The packet
lists the target book and the diff against the current book. A reviewer
open applies it at the next daily open. A skip, or a timeout, leaves the
current book where it is.

E3 is the 50-day trend with a 3x ATR trail. Each daily close can emit one
packet per new entry or add. The trail, a stop-out, and any other
risk-reducing change fill without a review. A hard gate still cannot be
overridden, and ``mode: live`` is refused.

Both books share the paper ledger and the desk caps: 6% open risk, 3%
inside a cluster, 3x notional on a name. Fills use the strategy backtest's
taker fee, 4/8 bp slippage, and archived funding. Slippage rank uses the
last closed day, so the forming day's volume is not an input.

Nothing here places an order or reads a trading key.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from collections import defaultdict

import numpy as np

from desk.config import assert_no_live_orders
from desk.delivery.outbox import enqueue_packet
from desk.journal.decision import DecisionJournal
from desk.rules.risk_gates import gate
from desk.scanner.signals import empty_market_context, empty_master_decision, validate_packet
from desk.strategies.catalog import primary_variant, variant_by_id
from desk.strategies.engine import Position as StratPosition
from desk.strategies.engine import _size
from desk.strategies.panel import Features, panel_from_frames
from desk.strategies.signals import DECISIONS
from desk.strategies.sizing import PORTFOLIO_MIN_RISK
from desk.timeutils import DAY_MS, ms_to_myt_iso, ms_to_utc_iso

# Sunday's daily bar. weekday() of the bar's UTC open, matching the research panel.
E2_WEEKDAY = 6
# The bar closes at the next 00:00 UTC, which is 08:00 in Asia/Kuala_Lumpur.
DAILY_CLOSE_UTC = "00:00"
DAILY_CLOSE_MYT = "08:00"

_SPECS = {
    "e2": {
        "family": "E2",
        "config_key": "e2_residual_momentum",
        "cadence": "weekly",
    },
    "e3": {
        "family": "E3",
        "config_key": "e3_slow_trend",
        "cadence": "daily",
    },
}


@dataclass
class Leg:
    symbol: str
    side: int
    notional: float
    cluster: str
    stop_pct: float
    risk_usdt: float
    book: str = ""
    weight: float = 0.0
    trail_mult: float | None = None
    exit_mode: str = "rebalance"
    decision_atr: float | None = None
    sticky: bool = False
    reset_entry: bool = True


@dataclass
class BookPosition:
    book: str
    symbol: str
    side: int
    qty: float
    entry_price: float
    stop_price: float
    trail_mult: float | None
    exit_mode: str
    cluster: str
    risk_usdt: float
    stop_pct: float
    decision_atr: float | None
    last_mark: float
    entry_time: int
    opened_day_ms: int
    cash_impact: float
    fees: float
    slippage: float
    funding: float
    turnover: float
    notional: float
    initial_risk: float
    signal_id: str | None
    sticky: bool
    setup: str
    last_day_ms: int


def enabled_specs(settings: dict) -> list[dict]:
    """Enabled E2/E3 books. E6 is not in this table."""
    found = []
    strategies = settings.get("strategies") or {}
    for book, spec in _SPECS.items():
        cfg = strategies.get(spec["config_key"]) or {}
        if not cfg.get("enabled"):
            continue
        variant = cfg.get("variant") or primary_variant(spec["family"])["variant"]
        row = variant_by_id(variant)
        if row["family"] != spec["family"]:
            continue
        found.append({**spec, "book": book, "variant": variant, "decide": row["decide"], "params": dict(row["params"]), "expected": cfg.get("expected") or {}})
    return found


def diff_book(current: dict[str, Leg], target: dict[str, Leg]) -> dict[str, list[dict]]:
    """Classify a rebalance. Notionals are absolute. A flip is its own row."""
    diffs = {name: [] for name in ("open", "close", "increase", "decrease", "flip", "unchanged")}
    for symbol in sorted(set(current) | set(target)):
        cur = current.get(symbol)
        nxt = target.get(symbol)
        if cur is None and nxt is not None:
            diffs["open"].append(_diff_row(symbol, None, nxt))
        elif nxt is None and cur is not None:
            diffs["close"].append(_diff_row(symbol, cur, None))
        elif int(cur.side) != int(nxt.side):
            diffs["flip"].append(_diff_row(symbol, cur, nxt))
        elif nxt.notional > cur.notional + 1e-6:
            diffs["increase"].append(_diff_row(symbol, cur, nxt))
        elif nxt.notional + 1e-6 < cur.notional:
            diffs["decrease"].append(_diff_row(symbol, cur, nxt))
        else:
            diffs["unchanged"].append(_diff_row(symbol, cur, nxt))
    return diffs


def diff_is_empty(diffs: dict) -> bool:
    return not any(diffs[name] for name in ("open", "close", "increase", "decrease", "flip"))


def enforce_caps(existing: list[Leg], proposed: list[Leg], equity: float, settings: dict) -> list[Leg]:
    """Scale proposed legs down until they fit beside ``existing``.

    Existing risk is left alone. A proposed leg is never scaled up. Freed
    risk is not given to another name. A leg that ends below the portfolio
    dust floor is dropped.
    """
    if equity <= 0 or not proposed:
        return []
    risk = settings["risk"]
    max_total = float(risk["max_open_risk_pct"]) * equity
    max_cluster = float(risk["max_cluster_risk_pct"]) * equity
    max_notional = float(risk["max_notional_equity_mult"]) * equity
    min_risk = PORTFOLIO_MIN_RISK * equity
    exist_total = 0.0
    exist_cluster: dict[str, float] = defaultdict(float)
    exist_notional: dict[str, float] = defaultdict(float)
    for leg in existing:
        exist_total += max(0.0, float(leg.risk_usdt))
        exist_cluster[leg.cluster] += max(0.0, float(leg.risk_usdt))
        exist_notional[leg.symbol] += abs(float(leg.notional))
    legs = [replace(leg) for leg in proposed if leg.notional > 0 and leg.risk_usdt > 0 and leg.stop_pct > 0]

    def _scale(members: list[Leg], factor: float) -> None:
        factor = max(0.0, float(factor))
        for leg in members:
            leg.notional *= factor
            leg.risk_usdt *= factor

    by_cluster: dict[str, list[Leg]] = defaultdict(list)
    for leg in legs:
        by_cluster[leg.cluster].append(leg)
    for cluster, members in by_cluster.items():
        total = sum(leg.risk_usdt for leg in members)
        room = max_cluster - exist_cluster[cluster]
        if total <= 0:
            continue
        if room <= 0:
            _scale(members, 0.0)
        elif total > room:
            _scale(members, room / total)
    total = sum(leg.risk_usdt for leg in legs)
    room = max_total - exist_total
    if total > 0:
        if room <= 0:
            _scale(legs, 0.0)
        elif total > room:
            _scale(legs, room / total)
    by_symbol: dict[str, list[Leg]] = defaultdict(list)
    for leg in legs:
        by_symbol[leg.symbol].append(leg)
    for symbol, members in by_symbol.items():
        notion = sum(leg.notional for leg in members)
        room_n = max_notional - exist_notional[symbol]
        if notion <= 0:
            continue
        if room_n <= 0:
            _scale(members, 0.0)
        elif notion > room_n:
            _scale(members, room_n / notion)
    out = []
    for leg in legs:
        if leg.notional <= 0 or leg.risk_usdt < min_risk:
            continue
        out.append(leg)
    return out


def cap_breach_names(existing: list[Leg], proposed: list[Leg], equity: float, settings: dict) -> list[str]:
    """Gate names that fire when ``proposed`` does not already fit."""
    if equity <= 0:
        return ["open_risk"]
    risk = settings["risk"]
    max_total = float(risk["max_open_risk_pct"]) * equity
    max_cluster = float(risk["max_cluster_risk_pct"]) * equity
    max_notional = float(risk["max_notional_equity_mult"]) * equity
    exist_total = sum(max(0.0, leg.risk_usdt) for leg in existing)
    exist_cluster: dict[str, float] = defaultdict(float)
    exist_notional: dict[str, float] = defaultdict(float)
    for leg in existing:
        exist_cluster[leg.cluster] += max(0.0, leg.risk_usdt)
        exist_notional[leg.symbol] += abs(leg.notional)
    prop_total = sum(max(0.0, leg.risk_usdt) for leg in proposed)
    prop_cluster: dict[str, float] = defaultdict(float)
    prop_notional: dict[str, float] = defaultdict(float)
    for leg in proposed:
        prop_cluster[leg.cluster] += max(0.0, leg.risk_usdt)
        prop_notional[leg.symbol] += abs(leg.notional)
    names = []
    if exist_total + prop_total > max_total + 1e-6:
        names.append("open_risk")
    if any(exist_cluster[key] + value > max_cluster + 1e-6 for key, value in prop_cluster.items()):
        names.append("cluster")
    if any(exist_notional[key] + value > max_notional + 1e-6 for key, value in prop_notional.items()):
        names.append("name_notional")
    return names


def book_contribution(store, now_ms: int) -> tuple[float, list[dict]] | None:
    """Mark open book positions. None means a mark could not be read."""
    rows = store.book_position_rows()
    if not rows:
        return 0.0, []
    pnl = 0.0
    views = []
    for row in rows:
        pos = load_book_position(row["payload"])
        mark = mark_price(store, pos.symbol, now_ms)
        if mark is None:
            return None
        pnl += float(pos.cash_impact) + float(pos.qty) * mark
        views.append(position_view(pos, mark))
    return pnl, views


def position_view(pos: BookPosition, mark: float) -> dict:
    return {
        "symbol": pos.symbol,
        "setup": pos.setup,
        "direction": "long" if pos.side > 0 else "short",
        "cluster": pos.cluster,
        "cluster_key": pos.cluster,
        "risk_usdt": float(pos.risk_usdt),
        "notional_usdt": abs(float(pos.qty) * mark),
        "book": pos.book,
    }


def reduce_books(settings: dict, store, now_ms: int, journal: DecisionJournal, *, kill: str | None) -> list[dict]:
    """Stops, state exits, and a kill-switch flatten. None of these wait for a review."""
    assert_no_live_orders(settings)
    specs = enabled_specs(settings)
    if not specs:
        return []
    features = features_as_of(store, now_ms)
    closed = []
    if features is None:
        if kill:
            closed.extend(_flatten_all(store, settings, now_ms, journal, None))
        return closed
    decision_i = last_closed_index(features, now_ms)
    for spec in specs:
        if decision_i is None:
            break
        closed.extend(_catch_up_stops(store, settings, features, spec, decision_i, now_ms, journal))
        closed.extend(_execute_pending(store, settings, features, spec, now_ms, journal))
    if kill:
        closed.extend(_flatten_all(store, settings, now_ms, journal, features))
    return closed


def propose_books(settings: dict, store, now_ms: int, journal: DecisionJournal, snapshot: dict) -> list[dict]:
    """Emit at most one E2 rebalance packet and one packet per E3 entry."""
    assert_no_live_orders(settings)
    specs = enabled_specs(settings)
    if not specs or snapshot is None:
        return []
    features = features_as_of(store, now_ms)
    if features is None:
        return []
    decision_i = last_closed_index(features, now_ms)
    if decision_i is None:
        return []
    equity = float(snapshot["equity_usdt"])
    if equity <= 0:
        return []
    packets = []
    held_books = {spec["book"]: _load_positions(store, spec["book"]) for spec in specs}
    views = list(snapshot.get("open_positions") or [])
    for spec in specs:
        suspend = {symbol: int(side) for symbol, side in (_meta(store, spec["book"]).get("suspend") or {}).items()}
        legs = _target_legs(features, spec, decision_i, now_ms, held_books[spec["book"]], equity, suspend)
        if legs is None:
            continue
        # Cap this book beside the other books as they stand now. A second
        # approval in the same cycle is checked again at fill time.
        others = _views_to_legs(views, skip_books={spec["book"]})
        fitted = enforce_caps(others, legs, equity, settings)
        target = {leg.symbol: leg for leg in fitted}
        current = {pos.symbol: _leg_from_position(pos) for pos in held_books[spec["book"]].values()}
        diffs = diff_book(current, target)
        if spec["cadence"] == "weekly":
            packet = _maybe_rebalance(store, settings, spec, features, decision_i, now_ms, journal, snapshot, current, target, diffs)
            if packet is not None:
                packets.append(packet)
        else:
            packets.extend(
                _maybe_entries(store, settings, spec, features, decision_i, now_ms, journal, snapshot, current, target, diffs)
            )
            _queue_e3_reductions(store, spec, features, decision_i, current, target, diffs)
    return packets


def fill_books(settings: dict, store, now_ms: int, journal: DecisionJournal, snapshot: dict) -> list[dict]:
    """Apply an approved rebalance or entry. A skip does not reach this fill."""
    assert_no_live_orders(settings)
    if snapshot is None:
        return []
    features = features_as_of(store, now_ms)
    if features is None:
        return []
    filled = []
    equity = float(snapshot["equity_usdt"])
    views = list(snapshot.get("open_positions") or [])
    for packet in store.list_signal_packets(status="proposed"):
        if packet.get("packet_kind") not in ("rebalance", "book_entry"):
            continue
        signal_id = packet["signal_id"]
        if store.resolution(signal_id) is not None:
            continue
        review = store.latest_review(signal_id)
        if review is None or review["effective_decision"] != "open":
            continue
        timeout_ms = int(settings["decision"]["timeout_seconds"]) * 1000
        if int(now_ms) - int(packet.get("ts_ms") or 0) > timeout_ms:
            _resolve(store, journal, now_ms, signal_id, "refused", "signal is outside the decision window")
            continue
        from desk.delivery.eligibility import entry_blocks

        blocks = entry_blocks(packet, _account(snapshot), views, settings, now_ms)
        if blocks:
            detail = "fill refused by " + ", ".join(blocks)
            store.append_paper_event(now_ms, "violation", "live_gate_block", signal_id, detail)
            journal.record_event(now_ms, signal_id, "violation", {"kind": "live_gate_block", "detail": detail, "class": "violation"})
            _resolve(store, journal, now_ms, signal_id, "refused", detail)
            continue
        spec = _spec_for(settings, packet["book"])
        if spec is None:
            _resolve(store, journal, now_ms, signal_id, "refused", "book is disabled")
            continue
        decision_i = _index_of(features, int(packet["decision_day_ms"]))
        if decision_i is None:
            continue
        targets = [_leg_from_dict(row) for row in packet.get("targets") or []]
        existing = _existing_for_packet(views, packet, targets)
        fitted = {leg.symbol: leg for leg in enforce_caps(existing, targets, equity, settings)}
        # Do not take more notional than the approved packet.
        for symbol, leg in list(fitted.items()):
            approved = next((row for row in targets if row.symbol == symbol), None)
            if approved is None or leg.notional > approved.notional + 1e-6:
                if approved is None:
                    fitted.pop(symbol, None)
                else:
                    scale = approved.notional / leg.notional if leg.notional else 0.0
                    fitted[symbol] = replace(leg, notional=approved.notional, risk_usdt=leg.risk_usdt * scale)
        held = _load_positions(store, packet["book"])
        if packet["packet_kind"] == "rebalance":
            trades = _apply_rebalance(store, settings, features, spec, held, fitted, decision_i, now_ms, signal_id, journal)
        else:
            trades = _apply_entries(store, settings, features, spec, held, fitted, decision_i, now_ms, signal_id, journal)
        _resolve(store, journal, now_ms, signal_id, "filled", "simulated book fill")
        filled.append({"signal_id": signal_id, "book": packet["book"], "trades": trades})
        views = _refresh_views(store, views, packet["book"], now_ms)
    return filled


def book_fresh_blocks(packet: dict, account: dict | None, open_positions: list[dict], settings: dict, now_ms: int) -> list[str]:
    """Recheck news, loss pauses, and the shared caps for a book packet."""
    blocks = []
    news = None if account is None else account.get("news_status")
    if news is None or str(news).upper() != "ALLOW":
        blocks.append("news")
    equity = _as_float(None if account is None else account.get("equity_usdt"))
    if equity is None or equity <= 0:
        blocks.extend(["equity", "open_risk"])
    else:
        targets = [_leg_from_dict(row) for row in packet.get("targets") or []]
        existing = _existing_for_packet(open_positions, packet, targets)
        blocks.extend(name for name in cap_breach_names(existing, targets, equity, settings) if name not in blocks)
    daily = _as_float(None if account is None else account.get("daily_pnl_pct"))
    weekly = _as_float(None if account is None else account.get("weekly_pnl_pct"))
    pause_d = float(settings["risk"]["daily_loss_pause_pct"])
    pause_w = float(settings["risk"]["weekly_loss_pause_pct"])
    if daily is None or daily <= pause_d:
        blocks.append("daily_loss")
    if weekly is None or weekly <= pause_w:
        blocks.append("weekly_loss")
    chain = (packet.get("hard_gates") or {}).get("on_chain")
    if isinstance(chain, dict) and chain.get("result") != "pass":
        blocks.append("on_chain")
    _ = now_ms
    return blocks


def book_actuals(store, settings: dict, now_ms: int) -> dict:
    """Turnover, slippage, and return for each open book and the pair."""
    account = store.paper_account() or {}
    initial = _as_float(account.get("initial_equity")) or _as_float(settings["paper"].get("initial_equity")) or 0.0
    started = account.get("started_at_ms")
    elapsed_days = 0.0 if started is None else max(0.0, (int(now_ms) - int(started)) / DAY_MS)
    curve = store.equity_curve()
    avg_equity = float(np.mean([point[1] for point in curve])) if curve else initial
    per = {}
    for spec in enabled_specs(settings):
        per[spec["book"]] = _one_book_actual(store, spec["book"], initial, avg_equity, elapsed_days)
    combined = _combine_actuals(list(per.values()), initial, avg_equity, elapsed_days)
    return {"elapsed_days": elapsed_days, "books": per, "combined": combined, "initial_equity": initial}


def load_book_position(payload: str) -> BookPosition:
    data = json.loads(payload)
    return BookPosition(**data)


def dump_book_position(pos: BookPosition) -> str:
    return json.dumps(asdict(pos), sort_keys=True)


def mark_price(store, symbol: str, now_ms: int) -> float | None:
    daily = store.klines(symbol, "1d", end_ms=now_ms)
    if daily is not None and not daily.empty:
        price = float(daily.iloc[-1]["close"])
        if price > 0:
            return price
    hourly = store.klines(symbol, "1h", end_ms=now_ms)
    if hourly is not None and not hourly.empty:
        price = float(hourly.iloc[-1]["close"])
        if price > 0:
            return price
    return None


def last_closed_index(features: Features, now_ms: int) -> int | None:
    """Last daily bar whose close is at or before ``now_ms``. The next open is not included."""
    limit = int(now_ms) - DAY_MS
    index = int(np.searchsorted(features.day_ms, limit, side="right")) - 1
    if index < 0:
        return None
    return index


def features_as_of(store, now_ms: int) -> Features | None:
    """Daily panel of bars that have already opened. Later bars are not loaded."""
    bars = store.load_interval_frame("1d")
    if bars is None or bars.empty:
        return None
    bars = bars[bars["open_time"] <= int(now_ms)]
    if bars.empty:
        return None
    funding = store.load_funding_frame()
    if funding is not None and not funding.empty:
        funding = funding[funding["funding_time"] <= int(now_ms)]
    metrics = store.load_metrics_frame()
    if metrics is not None and not metrics.empty:
        metrics = metrics[metrics["day_ms"] <= int(now_ms)]
    premium = store.load_interval_frame("premium1d")
    if premium is not None and not premium.empty:
        premium = premium[premium["open_time"] <= int(now_ms)]
    else:
        premium = None
    try:
        panel = panel_from_frames(
            bars,
            funding=None if funding is None or funding.empty else funding,
            metrics=None if metrics is None or metrics.empty else metrics,
            premium=premium,
        )
    except ValueError:
        return None
    return Features(panel)


def _diff_row(symbol: str, cur: Leg | None, nxt: Leg | None) -> dict:
    def _side(leg: Leg | None):
        if leg is None:
            return None
        return {
            "side": "long" if leg.side > 0 else "short",
            "notional": float(leg.notional),
            "risk_usdt": float(leg.risk_usdt),
            "cluster": leg.cluster,
        }

    return {"symbol": symbol, "from": _side(cur), "to": _side(nxt)}


def _leg_from_position(pos: BookPosition) -> Leg:
    return Leg(
        symbol=pos.symbol,
        side=pos.side,
        notional=abs(pos.notional),
        cluster=pos.cluster,
        stop_pct=pos.stop_pct if pos.stop_pct > 0 else 1.0,
        risk_usdt=pos.risk_usdt,
        book=pos.book,
        trail_mult=pos.trail_mult,
        exit_mode=pos.exit_mode,
        decision_atr=pos.decision_atr,
        sticky=pos.sticky,
        reset_entry=False,
    )


def _leg_from_dict(row: dict) -> Leg:
    side = row.get("side")
    if side in ("long", "short"):
        side = 1 if side == "long" else -1
    return Leg(
        symbol=str(row["symbol"]),
        side=int(side),
        notional=float(row["notional"]),
        cluster=str(row.get("cluster") or "alts"),
        stop_pct=float(row.get("stop_pct") or 0.0),
        risk_usdt=float(row.get("risk_usdt") or 0.0),
        book=str(row.get("book") or ""),
        weight=float(row.get("weight") or 0.0),
        trail_mult=None if row.get("trail_mult") is None else float(row["trail_mult"]),
        exit_mode=str(row.get("exit_mode") or "rebalance"),
        decision_atr=None if row.get("decision_atr") is None else float(row["decision_atr"]),
        sticky=bool(row.get("sticky")),
        reset_entry=bool(row.get("reset_entry", True)),
    )


def _existing_for_packet(views: list[dict], packet: dict, targets: list[Leg]) -> list[Leg]:
    """Positions the packet does not replace.

    A rebalance replaces its whole book. An entry replaces that symbol inside
    its own book and adds beside every other position.
    """
    if packet.get("packet_kind") == "rebalance":
        return _views_to_legs(views, skip_books={packet.get("book")})
    symbols = {leg.symbol for leg in targets}
    book = packet.get("book")
    kept = []
    for view in views:
        if view.get("book") == book and view.get("symbol") in symbols:
            continue
        kept.append(view)
    return _views_to_legs(kept, skip_books=set())


def _views_to_legs(views: list[dict], skip_books: set[str]) -> list[Leg]:
    legs = []
    for view in views:
        if view.get("book") in skip_books:
            continue
        risk = _as_float(view.get("risk_usdt")) or 0.0
        notional = abs(_as_float(view.get("notional_usdt")) or 0.0)
        if risk <= 0 and notional <= 0:
            continue
        cluster = view.get("cluster") or view.get("cluster_key") or "alts"
        side = 1 if view.get("direction") != "short" else -1
        stop_pct = risk / notional if notional > 0 else 1.0
        legs.append(
            Leg(
                symbol=str(view["symbol"]),
                side=side,
                notional=notional,
                cluster=str(cluster),
                stop_pct=stop_pct,
                risk_usdt=risk,
                book=str(view.get("book") or ""),
            )
        )
    return legs


def _load_positions(store, book: str) -> dict[str, BookPosition]:
    found = {}
    for row in store.book_position_rows(book):
        pos = load_book_position(row["payload"])
        found[pos.symbol] = pos
    return found


def _save_position(store, pos: BookPosition, now_ms: int) -> None:
    store.save_book_position(pos.book, pos.symbol, dump_book_position(pos), now_ms)


def _meta(store, book: str) -> dict:
    return store.book_meta(book) or {
        "managed_day_ms": None,
        "pending": [],
        "suspend": {},
        "proposed": {},
    }


def _save_meta(store, book: str, meta: dict, now_ms: int) -> None:
    store.save_book_meta(book, meta, now_ms)


def _catch_up_stops(store, settings, features, spec, decision_i, now_ms, journal) -> list[dict]:
    meta = _meta(store, spec["book"])
    last = meta.get("managed_day_ms")
    if last is None:
        indexes = [decision_i]
    else:
        start = _index_of(features, int(last))
        if start is None:
            indexes = [decision_i]
        else:
            indexes = list(range(start + 1, decision_i + 1))
    closed = []
    for index in indexes:
        closed.extend(_stop_day(store, settings, features, spec, index, now_ms, journal))
        meta = _meta(store, spec["book"])
        meta["managed_day_ms"] = int(features.day_ms[index])
        _save_meta(store, spec["book"], meta, now_ms)
    return closed


def _stop_day(store, settings, features, spec, index, now_ms, journal) -> list[dict]:
    """Close-based stop for one closed day. The ratchet cannot fire on the same close."""
    day = int(features.day_ms[index])
    positions = _load_positions(store, spec["book"])
    _apply_funding(store, settings, features, spec, positions, index, now_ms)
    positions = _load_positions(store, spec["book"])
    closed = []
    meta = _meta(store, spec["book"])
    suspend = dict(meta.get("suspend") or {})
    for symbol, pos in list(positions.items()):
        if int(pos.opened_day_ms) > day:
            continue
        sym_i = features.sym.get(symbol)
        if sym_i is None:
            continue
        price = float(features.close[sym_i, index])
        if not _finite_price(price):
            continue
        pos.last_mark = price
        pos.last_day_ms = day
        hit = _stop_hit(pos.side, price, pos.stop_price)
        if hit:
            trade = _close_position(store, settings, features, spec, pos, price, index, now_ms, "trail_stop", journal)
            closed.append(trade)
            if pos.sticky:
                suspend[symbol] = int(pos.side)
            continue
        if pos.trail_mult and pos.exit_mode == "trail":
            atr = float(features.atr[sym_i, index])
            if np.isfinite(atr) and atr > 0:
                proposed = price - pos.side * float(pos.trail_mult) * atr
                if pos.side > 0:
                    pos.stop_price = max(pos.stop_price, proposed)
                else:
                    pos.stop_price = min(pos.stop_price, proposed)
                distance = abs(price - pos.stop_price)
                pos.risk_usdt = abs(pos.qty) * distance
                pos.stop_pct = distance / price if price else pos.stop_pct
        _save_position(store, pos, now_ms)
    # Drop a suspension once the rule no longer wants that side.
    if spec["cadence"] == "daily" and suspend:
        held = {symbol: pos.side for symbol, pos in _load_positions(store, spec["book"]).items()}
        wanted = DECISIONS[spec["decide"]](features, index, held, spec["params"]) or []
        still = {intent.symbol: intent.side for intent in wanted}
        for symbol, side in list(suspend.items()):
            if still.get(symbol) != int(side):
                suspend.pop(symbol, None)
    meta["suspend"] = {symbol: int(side) for symbol, side in suspend.items()}
    _save_meta(store, spec["book"], meta, now_ms)
    return closed


def _execute_pending(store, settings, features, spec, now_ms, journal) -> list[dict]:
    meta = _meta(store, spec["book"])
    pending = list(meta.get("pending") or [])
    if not pending:
        return []
    kept = []
    closed = []
    positions = _load_positions(store, spec["book"])
    for item in pending:
        decision_i = _index_of(features, int(item["decision_day_ms"]))
        fill_i = None if decision_i is None else _fill_index(features, decision_i, now_ms)
        if fill_i is None:
            kept.append(item)
            continue
        pos = positions.get(item["symbol"])
        if pos is None:
            continue
        raw = float(features.open[features.sym[pos.symbol], fill_i])
        if not _finite_price(raw):
            kept.append(item)
            continue
        target_notional = float(item.get("target_notional") or 0.0)
        if target_notional <= 1e-8:
            trade = _close_position(
                store, settings, features, spec, pos, raw, fill_i, now_ms, item.get("reason") or "state_exit", journal, slip_index=decision_i
            )
            closed.append(trade)
            positions.pop(pos.symbol, None)
        else:
            target_qty = pos.side * target_notional / raw
            _resize_to(
                store,
                settings,
                features,
                spec,
                pos,
                target_qty,
                raw,
                fill_i,
                now_ms,
                journal,
                signal_id=None,
                reset=False,
                decision_i=decision_i,
            )
            positions = _load_positions(store, spec["book"])
    meta["pending"] = kept
    _save_meta(store, spec["book"], meta, now_ms)
    return closed


def _flatten_all(store, settings, now_ms, journal, features) -> list[dict]:
    closed = []
    for spec in enabled_specs(settings):
        for pos in list(_load_positions(store, spec["book"]).values()):
            price = None if features is None else _last_close(features, pos.symbol, now_ms)
            if price is None:
                price = mark_price(store, pos.symbol, now_ms)
            if price is None:
                continue
            trade = _close_position(store, settings, features, spec, pos, price, None, now_ms, "kill_switch", journal)
            closed.append(trade)
    return closed


def _target_legs(features, spec, decision_i, now_ms, positions, equity, suspend: dict | None = None) -> list[Leg] | None:
    """Sized book at the decision close. None means this bar is not a decision bar.

    ``suspend`` drops a side that just stopped out until the rule itself
    stops wanting it. The check uses the decision bar only.
    """
    if spec["cadence"] == "weekly" and int(features.weekday[decision_i]) != E2_WEEKDAY:
        return None
    held = {symbol: pos.side for symbol, pos in positions.items()}
    decision = DECISIONS[spec["decide"]](features, decision_i, held, spec["params"])
    if decision is None:
        return None
    suspended = suspend or {}
    decision = [intent for intent in decision if suspended.get(intent.symbol) != int(intent.side)]
    fill_i = _fill_index(features, decision_i, now_ms)
    price_index = fill_i if fill_i is not None else decision_i
    saved = []
    if fill_i is None:
        for intent in decision:
            index = features.sym.get(intent.symbol)
            if index is None:
                continue
            saved.append((index, float(features.open[index, decision_i])))
            close = float(features.close[index, decision_i])
            if _finite_price(close):
                features.open[index, decision_i] = close
    try:
        strat = {symbol: _as_strat(pos) for symbol, pos in positions.items()}
        chosen = _size(list(decision), strat, equity, features, price_index, decision_i)
    finally:
        for index, value in saved:
            features.open[index, price_index] = value
    legs = []
    for symbol, intent in chosen.items():
        notional = float(getattr(intent, "_notional", 0.0) or 0.0)
        if notional <= 0:
            continue
        index = features.sym[symbol]
        price = float(features.open[index, price_index])
        if not _finite_price(price):
            continue
        if intent.hard_stop_price is not None:
            stop_pct = abs(price - float(intent.hard_stop_price)) / price
        else:
            atr = intent.decision_atr
            if atr is None or not np.isfinite(atr) or atr <= 0:
                continue
            stop_pct = float(intent.risk_atr_mult) * float(atr) / price
        if not np.isfinite(stop_pct) or stop_pct <= 0:
            continue
        legs.append(
            Leg(
                symbol=symbol,
                side=int(intent.side),
                notional=notional,
                cluster=intent.cluster,
                stop_pct=float(stop_pct),
                risk_usdt=notional * float(stop_pct),
                book=spec["book"],
                weight=notional / equity if equity else 0.0,
                trail_mult=intent.trail_mult,
                exit_mode=intent.exit_mode,
                decision_atr=None if intent.decision_atr is None else float(intent.decision_atr),
                sticky=bool(intent.sticky_trail),
                reset_entry=bool(intent.reset_entry),
            )
            )
    return legs


def _maybe_rebalance(store, settings, spec, features, decision_i, now_ms, journal, snapshot, current, target, diffs):
    if diff_is_empty(diffs):
        return None
    day = int(features.day_ms[decision_i])
    meta = _meta(store, spec["book"])
    if str(day) in (meta.get("proposed") or {}):
        return None
    ts = day + DAY_MS
    timeout_ms = int(settings["decision"]["timeout_seconds"]) * 1000
    if int(now_ms) - ts > timeout_ms:
        return None
    packet = _build_packet(
        settings,
        spec,
        kind="rebalance",
        symbol=spec["book"].upper(),
        direction="rebalance",
        day=day,
        ts=ts,
        now_ms=now_ms,
        snapshot=snapshot,
        targets=list(target.values()),
        diffs=diffs,
        risk_usdt=sum(leg.risk_usdt for leg in target.values()),
        notional=sum(leg.notional for leg in target.values()),
        cluster=None,
        stop_pct=None,
    )
    _emit(store, settings, journal, packet, now_ms)
    meta = _meta(store, spec["book"])
    proposed = dict(meta.get("proposed") or {})
    proposed[str(day)] = packet["signal_id"]
    meta["proposed"] = proposed
    _save_meta(store, spec["book"], meta, now_ms)
    return packet


def _maybe_entries(store, settings, spec, features, decision_i, now_ms, journal, snapshot, current, target, diffs):
    day = int(features.day_ms[decision_i])
    ts = day + DAY_MS
    timeout_ms = int(settings["decision"]["timeout_seconds"]) * 1000
    if int(now_ms) - ts > timeout_ms:
        return []
    meta = _meta(store, spec["book"])
    suspend = {symbol: int(side) for symbol, side in (meta.get("suspend") or {}).items()}
    packets = []
    rows = list(diffs["open"]) + list(diffs["increase"]) + list(diffs["flip"])
    for row in rows:
        leg = target.get(row["symbol"])
        if leg is None:
            continue
        if suspend.get(leg.symbol) == int(leg.side):
            continue
        key = f"{day}|{leg.symbol}|{leg.side}"
        if key in (meta.get("proposed") or {}):
            continue
        one = diff_book({} if row["symbol"] not in current else {row["symbol"]: current[row["symbol"]]}, {leg.symbol: leg})
        packet = _build_packet(
            settings,
            spec,
            kind="book_entry",
            symbol=leg.symbol,
            direction="long" if leg.side > 0 else "short",
            day=day,
            ts=ts,
            now_ms=now_ms,
            snapshot=snapshot,
            targets=[leg],
            diffs=one,
            risk_usdt=leg.risk_usdt,
            notional=leg.notional,
            cluster=leg.cluster,
            stop_pct=leg.stop_pct,
        )
        _emit(store, settings, journal, packet, now_ms)
        meta = _meta(store, spec["book"])
        proposed = dict(meta.get("proposed") or {})
        proposed[key] = packet["signal_id"]
        meta["proposed"] = proposed
        _save_meta(store, spec["book"], meta, now_ms)
        packets.append(packet)
    return packets


def _queue_e3_reductions(store, spec, features, decision_i, current, target, diffs) -> None:
    """Exits and trims wait for the next open and do not wait for a review."""
    meta = _meta(store, spec["book"])
    day = int(features.day_ms[decision_i])
    symbols = {row["symbol"] for row in diffs["close"]}
    for row in diffs["flip"]:
        symbols.add(row["symbol"])
    queued = []
    for symbol in sorted(symbols):
        queued.append({"symbol": symbol, "reason": "state_exit", "decision_day_ms": day, "target_notional": 0.0})
    for row in diffs["decrease"]:
        leg = target[row["symbol"]]
        queued.append(
            {
                "symbol": row["symbol"],
                "reason": "reduce",
                "decision_day_ms": day,
                "target_notional": float(leg.notional),
            }
        )
    fresh_symbols = {item["symbol"] for item in queued}
    kept = [item for item in (meta.get("pending") or []) if item["symbol"] not in fresh_symbols]
    meta["pending"] = kept + queued
    _save_meta(store, spec["book"], meta, int(features.day_ms[decision_i]))


def _build_packet(settings, spec, *, kind, symbol, direction, day, ts, now_ms, snapshot, targets, diffs, risk_usdt, notional, cluster, stop_pct) -> dict:
    equity = float(snapshot["equity_usdt"])
    packet_stub = {"packet_kind": kind, "book": spec["book"]}
    existing = _existing_for_packet(snapshot.get("open_positions") or [], packet_stub, targets)
    breaches = set(cap_breach_names(existing, targets, equity, settings))
    news = snapshot.get("news_status")
    news_ok = news is not None and str(news).upper() == "ALLOW"
    daily = snapshot.get("daily_pnl_pct")
    weekly = snapshot.get("weekly_pnl_pct")
    daily_ok = daily is not None and float(daily) > float(settings["risk"]["daily_loss_pause_pct"])
    weekly_ok = weekly is not None and float(weekly) > float(settings["risk"]["weekly_loss_pause_pct"])
    checks = {
        "news": (news_ok, "News Watcher ALLOW" if news_ok else "News Watcher status is missing"),
        "equity": (equity > 0, "paper equity"),
        "open_risk": ("open_risk" not in breaches, "open risk inside 6%" if "open_risk" not in breaches else "open risk cap"),
        "cluster": ("cluster" not in breaches, "cluster risk inside 3%" if "cluster" not in breaches else "cluster cap"),
        "name_notional": ("name_notional" not in breaches, "name notional inside 3x" if "name_notional" not in breaches else "name cap"),
        "daily_loss": (daily_ok, "daily loss inside the limit" if daily_ok else "daily loss pause"),
        "weekly_loss": (weekly_ok, "weekly loss inside the limit" if weekly_ok else "weekly loss pause"),
        "closed_bar": (True, "decision uses the last closed daily bar"),
    }
    hard_gates = {name: gate("pass" if ok else "fail", reason, ts) for name, (ok, reason) in checks.items()}
    passed = all(ok for ok, _reason in checks.values())
    signal_id = _signal_id(spec["book"], kind, symbol, direction, day)
    dedupe = f"{spec['book']}|{kind}|{symbol}|{direction}|{day}"
    target_rows = [_public_leg(leg) for leg in targets]
    packet = {
        "signal_id": signal_id,
        "dedupe_key": dedupe,
        "ts_utc": ms_to_utc_iso(ts),
        "ts_myt": ms_to_myt_iso(ts),
        "ts_ms": int(ts),
        "symbol": symbol,
        "venue": "Binance USDT-M",
        "margin_mode": "isolated",
        "setup": spec["config_key"],
        "setup_timeframe": "1d",
        "entry_timeframe": "1d",
        "direction": direction,
        "entry": None,
        "stop": None,
        "tp1": None,
        "tp2": None,
        "atr_4h": None,
        "size_coins": None,
        "notional_usdt": float(notional),
        "leverage": None,
        "risk_pct": float(risk_usdt) / equity if equity else None,
        "risk_scale": float(settings["risk"]["scale"]),
        "risk_usdt": float(risk_usdt),
        "reward_risk": None,
        "equity_usdt": equity,
        "funding_rate": None,
        "liq_price": None,
        "listing_age_days": None,
        "cluster": cluster or "book",
        "cluster_key": cluster or "book",
        "time_stop_bars": None,
        "time_stop_at": None,
        "invalidation": "weekly rebalance" if kind == "rebalance" else "3x ATR trail or 50-day state",
        "checks_passed": [name for name, (ok, _reason) in checks.items() if ok],
        "on_blocklist": False,
        "hard_gates": hard_gates,
        "votes": {
            "scanner": {
                "result": "pass" if passed else "fail",
                "reason": "book gates passed" if passed else "book gate failed",
                "ts": ms_to_utc_iso(ts),
            }
        },
        "market_context": empty_market_context(),
        "decision_path": "pending_master" if passed else "hard_gate_reject",
        "master_decision": empty_master_decision(),
        "mode": "paper",
        "rules_followed": None,
        "status": "proposed" if passed else "rejected",
        "rules_version": settings.get("rules_version", "0.3"),
        "packet_kind": kind,
        "book": spec["book"],
        "variant": spec["variant"],
        "decision_day_ms": int(day),
        "targets": target_rows,
        "diffs": diffs,
        "stop_pct": None if stop_pct is None else float(stop_pct),
    }
    validate_packet(packet, scanner_output=True)
    return packet


def _public_leg(leg: Leg) -> dict:
    return {
        "symbol": leg.symbol,
        "side": "long" if leg.side > 0 else "short",
        "side_sign": int(leg.side),
        "notional": float(leg.notional),
        "cluster": leg.cluster,
        "stop_pct": float(leg.stop_pct),
        "risk_usdt": float(leg.risk_usdt),
        "weight": float(leg.weight),
        "book": leg.book,
        "trail_mult": None if leg.trail_mult is None else float(leg.trail_mult),
        "exit_mode": leg.exit_mode,
        "decision_atr": None if leg.decision_atr is None else float(leg.decision_atr),
        "sticky": bool(leg.sticky),
        "reset_entry": bool(leg.reset_entry),
    }


def _emit(store, settings, journal, packet, now_ms) -> None:
    if store.has_signal(packet["signal_id"]) or store.has_dedupe(packet["dedupe_key"]):
        return
    store.save_signal(packet)
    journal.record_signal(packet, int(packet["ts_ms"]))
    if settings["webhook"].get("enabled") and packet.get("status") == "proposed":
        enqueue_packet(store, packet, settings, now_ms)


def _apply_rebalance(store, settings, features, spec, held, fitted, decision_i, now_ms, signal_id, journal) -> list[str]:
    touched = []
    for symbol, pos in list(held.items()):
        if symbol in fitted:
            continue
        raw, fill_i = _execution_price(features, symbol, decision_i, now_ms)
        if raw is None:
            continue
        _close_position(
            store, settings, features, spec, pos, raw, fill_i, now_ms, "rebalance", journal, signal_id=signal_id, slip_index=decision_i
        )
        touched.append(symbol)
    for symbol, leg in fitted.items():
        raw, fill_i = _execution_price(features, symbol, decision_i, now_ms)
        if raw is None:
            continue
        pos = _load_positions(store, spec["book"]).get(symbol)
        target_qty = leg.side * leg.notional / raw
        _resize_to(
            store,
            settings,
            features,
            spec,
            pos,
            target_qty,
            raw,
            fill_i,
            now_ms,
            journal,
            signal_id=signal_id,
            reset=True,
            leg=leg,
            decision_i=decision_i,
        )
        touched.append(symbol)
    return touched


def _apply_entries(store, settings, features, spec, held, fitted, decision_i, now_ms, signal_id, journal) -> list[str]:
    touched = []
    for symbol, leg in fitted.items():
        raw, fill_i = _execution_price(features, symbol, decision_i, now_ms)
        if raw is None:
            continue
        pos = held.get(symbol)
        if pos is not None and pos.side != leg.side:
            _close_position(
                store, settings, features, spec, pos, raw, fill_i, now_ms, "flip", journal, signal_id=signal_id, slip_index=decision_i
            )
            pos = None
        target_qty = leg.side * leg.notional / raw
        _resize_to(
            store,
            settings,
            features,
            spec,
            pos,
            target_qty,
            raw,
            fill_i,
            now_ms,
            journal,
            signal_id=signal_id,
            reset=pos is None or leg.reset_entry,
            leg=leg,
            decision_i=decision_i,
        )
        touched.append(symbol)
    return touched


def _execution_price(features, symbol, decision_i: int, now_ms: int) -> tuple[float | None, int]:
    """Next daily open when that bar has started, otherwise the decision close.

    The decision close is already knowable. Waiting for a daily bar that the
    archive only stores once it closes would push the fill past the review window.
    """
    sym = features.sym.get(symbol)
    if sym is None:
        return None, decision_i
    fill_i = _fill_index(features, decision_i, now_ms)
    if fill_i is not None:
        raw = float(features.open[sym, fill_i])
        if _finite_price(raw):
            return raw, fill_i
    raw = float(features.close[sym, decision_i])
    if _finite_price(raw):
        return raw, decision_i
    return None, decision_i


def _resize_to(store, settings, features, spec, pos, target_qty, raw, fill_i, now_ms, journal, *, signal_id, reset, leg=None, decision_i=None) -> None:
    current = 0.0 if pos is None else pos.qty
    delta = target_qty - current
    if abs(delta) * raw < 1e-8 and pos is not None:
        return
    slip = _slip(features, spec, pos.symbol if pos else leg.symbol, decision_i if decision_i is not None else fill_i)
    direction = 1 if delta > 0 else -1
    fill = raw * (1.0 + direction * slip)
    fee_rate = float(spec["params"].get("fee_rate", 0.0005))
    fee = abs(delta) * abs(fill) * fee_rate
    slip_dollars = abs(fill - raw) * abs(delta)
    symbol = pos.symbol if pos is not None else leg.symbol
    if pos is None:
        pos = BookPosition(
            book=spec["book"],
            symbol=symbol,
            side=int(leg.side),
            qty=0.0,
            entry_price=fill,
            stop_price=fill,
            trail_mult=leg.trail_mult,
            exit_mode=leg.exit_mode,
            cluster=leg.cluster,
            risk_usdt=0.0,
            stop_pct=leg.stop_pct,
            decision_atr=leg.decision_atr,
            last_mark=raw,
            entry_time=int(features.day_ms[fill_i]),
            opened_day_ms=int(features.day_ms[fill_i]),
            cash_impact=0.0,
            fees=0.0,
            slippage=0.0,
            funding=0.0,
            turnover=0.0,
            notional=0.0,
            initial_risk=0.0,
            signal_id=signal_id,
            sticky=bool(leg.sticky),
            setup=spec["config_key"],
            last_day_ms=int(features.day_ms[fill_i]),
        )
    pos.cash_impact -= delta * fill + fee
    pos.fees += fee
    pos.slippage += slip_dollars
    pos.turnover += abs(delta) * abs(fill)
    pos.qty = target_qty
    pos.notional = abs(target_qty) * raw
    pos.side = int(leg.side) if leg is not None else pos.side
    if leg is not None:
        pos.exit_mode = leg.exit_mode
        pos.trail_mult = leg.trail_mult
        pos.cluster = leg.cluster
        pos.sticky = bool(leg.sticky)
        pos.stop_pct = leg.stop_pct
        pos.decision_atr = leg.decision_atr
    if reset and leg is not None:
        entry = raw * (1.0 + (1 if leg.side > 0 else -1) * slip)
        pos.entry_price = entry
        pos.entry_time = int(features.day_ms[fill_i])
        pos.opened_day_ms = int(features.day_ms[fill_i])
        atr = leg.decision_atr or 0.0
        if leg.trail_mult and atr and np.isfinite(atr):
            pos.stop_price = entry - leg.side * float(leg.trail_mult) * float(atr)
        else:
            pos.stop_price = float("nan")
        pos.initial_risk = abs(pos.qty) * leg.stop_pct * raw
        pos.signal_id = signal_id
    pos.risk_usdt = abs(pos.notional) * float(pos.stop_pct if pos.stop_pct else 0.0)
    pos.last_mark = raw
    if abs(pos.qty) * raw < 1e-8:
        _close_record(store, pos, fill, int(features.day_ms[fill_i]), "rebalance", signal_id, journal, now_ms)
        store.delete_book_position(pos.book, pos.symbol)
        return
    _save_position(store, pos, now_ms)
    journal.record_event(
        now_ms,
        signal_id or f"{spec['book']}|fill|{symbol}|{int(features.day_ms[fill_i])}",
        "fill",
        {"fill_price": fill, "size": pos.qty, "fee": fee, "stop": pos.stop_price, "book": spec["book"], "ts_ms": int(features.day_ms[fill_i])},
    )


def _close_position(store, settings, features, spec, pos, raw, index, now_ms, reason, journal, signal_id=None, slip_index=None) -> dict:
    rank_index = index if slip_index is None else slip_index
    slip = _slip(features, spec, pos.symbol, rank_index if rank_index is not None else 0) if features is not None and rank_index is not None else float(spec["params"].get("slip_rest", 0.0008))
    direction = -1 if pos.qty > 0 else 1
    fill = raw * (1.0 + direction * slip)
    fee_rate = float(spec["params"].get("fee_rate", 0.0005))
    fee = abs(pos.qty) * abs(fill) * fee_rate
    slip_dollars = abs(fill - raw) * abs(pos.qty)
    delta = -pos.qty
    pos.cash_impact -= delta * fill + fee
    pos.fees += fee
    pos.slippage += slip_dollars
    pos.turnover += abs(delta) * abs(fill)
    pos.qty = 0.0
    ts = int(now_ms if index is None or features is None else features.day_ms[index])
    trade = _close_record(store, pos, fill, ts, reason, signal_id, journal, now_ms)
    store.delete_book_position(pos.book, pos.symbol)
    return trade


def _close_record(store, pos, fill, ts, reason, signal_id, journal, now_ms) -> dict:
    pnl = pos.cash_impact
    risk = pos.initial_risk
    trade = {
        "signal_id": signal_id or pos.signal_id or f"{pos.book}|exit|{pos.symbol}|{ts}",
        "symbol": pos.symbol,
        "book": pos.book,
        "setup": pos.setup,
        "direction": "long" if pos.side > 0 else "short",
        "pnl": pnl,
        "r_multiple": (pnl / risk) if risk and risk > 0 else None,
        "fees": pos.fees,
        "funding": pos.funding,
        "slippage": pos.slippage,
        "turnover": pos.turnover,
        "exit_reason": reason,
        "exit_time": int(ts),
        "exit_price": fill,
        "entry_time": pos.entry_time,
    }
    store.save_paper_trade(trade)
    journal.record_event(
        now_ms,
        trade["signal_id"],
        "exit",
        {
            "exit_price": fill,
            "reason": reason,
            "pnl": pnl,
            "r_multiple": trade["r_multiple"],
            "fees": pos.fees,
            "funding": pos.funding,
            "book": pos.book,
            "ts_ms": int(ts),
            "approval": "not_required",
        },
    )
    return trade


def _apply_funding(store, settings, features, spec, positions, index, now_ms) -> None:
    if not spec["params"].get("charge_funding", True):
        return
    lo = int(features.day_ms[index])
    hi = lo + DAY_MS
    for symbol, pos in positions.items():
        if int(pos.opened_day_ms) > lo:
            continue
        events = features.panel.funding_events.get(symbol)
        if not events:
            continue
        times, rates, _hours = events
        if times.size == 0:
            continue
        left = int(np.searchsorted(times, lo, side="right"))
        right = int(np.searchsorted(times, hi, side="right"))
        if right <= left:
            continue
        mark = float(features.close[features.sym[symbol], index])
        if not _finite_price(mark):
            mark = pos.last_mark
        for rate in rates[left:right]:
            if not np.isfinite(rate):
                continue
            change = -pos.qty * mark * float(rate)
            pos.cash_impact += change
            pos.funding += change
        _save_position(store, pos, now_ms)


def _slip(features, spec, symbol, index) -> float:
    """Slippage from the decision day's volume rank. The fill day's volume is not used."""
    if features is None or index is None:
        return float(spec["params"].get("slip_rest", 0.0008))
    sym_i = features.sym.get(symbol)
    if sym_i is None:
        return float(spec["params"].get("slip_rest", 0.0008))
    return float(features.slip(sym_i, int(index), spec["params"]))


def _as_strat(pos: BookPosition) -> StratPosition:
    return StratPosition(
        symbol=pos.symbol,
        side=pos.side,
        qty=pos.qty,
        entry_price=pos.entry_price,
        entry_i=0,
        stop_price=pos.stop_price,
        initial_stop=pos.stop_price,
        exit_mode=pos.exit_mode,
        trail_mult=pos.trail_mult,
        channel_n=None,
        hard_stop=None,
        target_price=None,
        time_stop_i=None,
        shock_dir=0,
        consec=0,
        cluster=pos.cluster,
        sticky_trail=pos.sticky,
        last_mark=pos.last_mark,
        decision_atr=pos.decision_atr,
    )


def _fill_index(features: Features, decision_i: int, now_ms: int) -> int | None:
    nxt = decision_i + 1
    if nxt >= features.day_ms.size:
        return None
    if int(features.day_ms[nxt]) <= int(now_ms):
        return nxt
    return None


def _index_of(features: Features, day_ms: int) -> int | None:
    index = int(np.searchsorted(features.day_ms, int(day_ms), side="left"))
    if index >= features.day_ms.size or int(features.day_ms[index]) != int(day_ms):
        return None
    return index


def _last_close(features: Features, symbol: str, now_ms: int) -> float | None:
    index = features.sym.get(symbol)
    if index is None:
        return None
    closed = last_closed_index(features, now_ms)
    if closed is None:
        return None
    price = float(features.close[index, closed])
    if _finite_price(price):
        return price
    return None


def _spec_for(settings, book: str) -> dict | None:
    for spec in enabled_specs(settings):
        if spec["book"] == book:
            return spec
    return None


def _account(snapshot: dict) -> dict:
    return {
        "equity_usdt": snapshot.get("equity_usdt"),
        "daily_pnl_pct": snapshot.get("daily_pnl_pct"),
        "weekly_pnl_pct": snapshot.get("weekly_pnl_pct"),
        "news_status": snapshot.get("news_status"),
    }


def _refresh_views(store, views, book, now_ms) -> list[dict]:
    kept = [view for view in views if view.get("book") != book]
    contribution = book_contribution(store, now_ms)
    if contribution is None:
        return kept
    _pnl, book_views = contribution
    return kept + [view for view in book_views if view.get("book") == book]


def _resolve(store, journal, now_ms, signal_id, outcome, reason) -> None:
    store.save_resolution(signal_id, outcome, now_ms, reason)
    journal.record_event(now_ms, signal_id, "resolution", {"outcome": outcome, "reason": reason})


def _one_book_actual(store, book, initial, avg_equity, elapsed_days) -> dict:
    trades = [trade for trade in store.list_paper_trades() if trade.get("book") == book]
    positions = [_pos for _pos in (_load_positions(store, book)).values()]
    turnover = sum(float(trade.get("turnover") or 0.0) for trade in trades) + sum(pos.turnover for pos in positions)
    slippage = sum(float(trade.get("slippage") or 0.0) for trade in trades) + sum(pos.slippage for pos in positions)
    fees = sum(float(trade.get("fees") or 0.0) for trade in trades) + sum(pos.fees for pos in positions)
    funding = sum(float(trade.get("funding") or 0.0) for trade in trades) + sum(pos.funding for pos in positions)
    pnl = sum(float(trade.get("pnl") or 0.0) for trade in trades)
    for pos in positions:
        mark = pos.last_mark
        pnl += pos.cash_impact + pos.qty * mark
    years = elapsed_days / 365.0
    base = avg_equity if avg_equity else initial
    return {
        "book": book,
        "positions": len(positions),
        "trades": len(trades),
        "turnover_notional": turnover,
        "turnover": (turnover / base) if base else 0.0,
        "turnover_per_year": (turnover / base / years) if base and years else 0.0,
        "slippage": slippage,
        "slippage_frac": (slippage / initial) if initial else 0.0,
        "fees": fees,
        "funding": funding,
        "net_pnl": pnl,
        "return": (pnl / initial) if initial else 0.0,
    }


def _combine_actuals(rows: list[dict], initial, avg_equity, elapsed_days) -> dict:
    if not rows:
        return _one_book_actual_zero()
    turnover = sum(row["turnover_notional"] for row in rows)
    slippage = sum(row["slippage"] for row in rows)
    pnl = sum(row["net_pnl"] for row in rows)
    years = elapsed_days / 365.0
    base = avg_equity if avg_equity else initial
    return {
        "positions": sum(row["positions"] for row in rows),
        "trades": sum(row["trades"] for row in rows),
        "turnover_notional": turnover,
        "turnover": (turnover / base) if base else 0.0,
        "turnover_per_year": (turnover / base / years) if base and years else 0.0,
        "slippage": slippage,
        "slippage_frac": (slippage / initial) if initial else 0.0,
        "fees": sum(row["fees"] for row in rows),
        "funding": sum(row["funding"] for row in rows),
        "net_pnl": pnl,
        "return": (pnl / initial) if initial else 0.0,
    }


def _one_book_actual_zero() -> dict:
    return {
        "positions": 0,
        "trades": 0,
        "turnover_notional": 0.0,
        "turnover": 0.0,
        "turnover_per_year": 0.0,
        "slippage": 0.0,
        "slippage_frac": 0.0,
        "fees": 0.0,
        "funding": 0.0,
        "net_pnl": 0.0,
        "return": 0.0,
    }


def _signal_id(*parts) -> str:
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _finite_price(value: float) -> bool:
    return bool(np.isfinite(value) and value > 0)


def _stop_hit(side: int, price: float, stop: float) -> bool:
    if not np.isfinite(stop):
        return False
    if side > 0:
        return price <= stop
    return price >= stop


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number
