"""Daily simulator. Signals at the close trade at the next open. No same-bar entries.

Stops that the rule describes as a daily-close exit fill at that close. Event
hard stops can fill inside the bar; if the stop and the target are both
touched, the stop fills. A stop that gaps through the open fills at the open.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from desk.strategies.panel import Features
from desk.strategies.signals import DECISIONS, Intent
from desk.strategies.sizing import greedy_fill, inverse_vol_balanced, notionals_from_risk, scale_to_caps
from desk.timeutils import DAY_MS


@dataclass
class Position:
    symbol: str
    side: int
    qty: float
    entry_price: float
    entry_i: int
    stop_price: float
    initial_stop: float
    exit_mode: str
    trail_mult: float | None
    channel_n: int | None
    hard_stop: float | None
    target_price: float | None
    time_stop_i: int | None
    shock_dir: int
    consec: int
    cluster: str
    sticky_trail: bool
    last_mark: float
    gross_cash: float = 0.0
    risk_dollars: float = 0.0
    decision_atr: float | None = None


@dataclass
class SimResult:
    day_ms: np.ndarray
    equity: np.ndarray
    fees: float
    slippage: float
    funding_pnl: float
    turnover: float
    days_in_market: int
    avg_names: float
    avg_gross: float
    avg_net: float
    trades: list[dict] = field(default_factory=list)
    start_equity: float = 0.0
    end_equity: float = 0.0
    rebalances: int = 0
    weeks_flat: int = 0


def _finite_price(value: float) -> bool:
    return bool(np.isfinite(value) and value > 0)


def _prior_level(features: Features, index: int, i: int, window: int, side: int) -> float:
    kind = ("low" if side > 0 else "high") + str(window)
    if kind not in ("low10", "high10", "low20", "high20"):
        return float("nan")
    return float(features.column(kind)[index, i])


def _size(intents: list[Intent], positions: dict[str, Position], equity: float, features: Features, fill_i: int, decision_i: int) -> dict[str, Intent]:
    """Map symbol -> intent that will be filled, with ``_notional`` set on the object."""
    if equity <= 0:
        return {}
    chosen: dict[str, Intent] = {}
    keep_rows = []
    fresh: list[Intent] = []
    for intent in intents:
        index = features.sym.get(intent.symbol)
        if index is None:
            continue
        price = float(features.open[index, fill_i])
        if not _finite_price(price):
            continue
        if intent.max_retrace is not None and intent.shock_high is not None and intent.shock_low is not None:
            span = float(intent.shock_high) - float(intent.shock_low)
            if span > 0:
                if intent.side > 0 and price > float(intent.shock_low) + float(intent.max_retrace) * span:
                    continue
                if intent.side < 0 and price < float(intent.shock_high) - float(intent.max_retrace) * span:
                    continue
        if intent.hard_stop_price is not None:
            if intent.side > 0 and price <= float(intent.hard_stop_price):
                continue
            if intent.side < 0 and price >= float(intent.hard_stop_price):
                continue
        held = positions.get(intent.symbol)
        if intent.keep and held is not None and held.side == intent.side:
            keep_rows.append((intent, price, held))
        else:
            fresh.append(intent)

    used_total = 0.0
    used_cluster: dict[str, float] = {}
    for intent, price, held in keep_rows:
        notional = abs(held.qty) * price
        stop_ref = held.hard_stop if held.hard_stop is not None else held.stop_price
        stop_pct = abs(price - stop_ref) / price if _finite_price(price) else 0.0
        dollars = notional * stop_pct
        used_total += dollars
        used_cluster[held.cluster] = used_cluster.get(held.cluster, 0.0) + dollars
        intent._notional = notional  # type: ignore[attr-defined]
        chosen[intent.symbol] = intent

    # If the kept book itself is over the caps, scale those notionals down.
    if used_total > 0.06 * equity or any(value > 0.03 * equity for value in used_cluster.values()):
        from collections import defaultdict

        groups: dict[str, list] = defaultdict(list)
        for intent, price, held in keep_rows:
            groups[held.cluster].append((intent, price, held))
        for members in groups.values():
            cluster_dollars = 0.0
            for intent, price, held in members:
                stop_ref = held.hard_stop if held.hard_stop is not None else held.stop_price
                cluster_dollars += intent._notional * abs(price - stop_ref) / price
            if cluster_dollars > 0.03 * equity and cluster_dollars > 0:
                scale = (0.03 * equity) / cluster_dollars
                for intent, _price, _held in members:
                    intent._notional *= scale
        total_dollars = 0.0
        for intent, price, held in keep_rows:
            stop_ref = held.hard_stop if held.hard_stop is not None else held.stop_price
            total_dollars += intent._notional * abs(price - stop_ref) / price
        if total_dollars > 0.06 * equity and total_dollars > 0:
            scale = (0.06 * equity) / total_dollars
            for intent, _price, _held in keep_rows:
                intent._notional *= scale
        used_total = 0.0
        used_cluster = {}
        for intent, price, held in keep_rows:
            stop_ref = held.hard_stop if held.hard_stop is not None else held.stop_price
            dollars = intent._notional * abs(price - stop_ref) / price
            used_total += dollars
            used_cluster[held.cluster] = used_cluster.get(held.cluster, 0.0) + dollars

    rows = []
    for intent in fresh:
        index = features.sym[intent.symbol]
        price = float(features.open[index, fill_i])
        if intent.size_mode == "fixed":
            stop_pct = 1.0
        elif intent.hard_stop_price is not None:
            stop_pct = abs(price - float(intent.hard_stop_price)) / price
        else:
            atr = intent.decision_atr
            if atr is None or not np.isfinite(atr) or atr <= 0:
                continue
            stop_pct = float(intent.risk_atr_mult) * float(atr) / price
        if not np.isfinite(stop_pct) or stop_pct <= 0:
            continue
        rows.append(
            {
                "symbol": intent.symbol,
                "side": intent.side,
                "stop_pct": stop_pct,
                "cluster": intent.cluster,
                "risk_fraction": intent.risk_fraction,
                "raw_weight": intent.raw_weight,
                "score": intent.score,
                "intent": intent,
            }
        )
    if not rows:
        return chosen
    mode = rows[0]["intent"].size_mode
    if mode == "fixed":
        sized = []
        for row in rows:
            notional = min(float(row["raw_weight"]), 3.0) * equity
            if notional <= 0:
                continue
            from desk.strategies.sizing import Sized

            sized.append(Sized(row["symbol"], 1 if row["side"] >= 0 else -1, notional, row["stop_pct"]))
    elif mode == "inv_vol":
        sized = inverse_vol_balanced(rows, equity)
    elif mode == "greedy":
        sized = greedy_fill(rows, equity, used_total=used_total, used_cluster=used_cluster)
    elif mode == "vol_target":
        sized = _vol_target(rows, equity, features, decision_i)
    else:
        sized = notionals_from_risk(rows, equity, used_total=used_total, used_cluster=used_cluster)
    by_symbol = {row["symbol"]: row["intent"] for row in rows}
    for item in sized:
        intent = by_symbol[item.symbol]
        intent._notional = item.notional  # type: ignore[attr-defined]
        chosen[item.symbol] = intent
    return chosen


def _vol_target(rows: list[dict], equity: float, features: Features, decision_i: int) -> list:
    n = len(rows)
    if n == 0 or equity <= 0:
        return []
    weights = np.array([ (1.0 if row["side"] >= 0 else -1.0) / n for row in rows ])
    window = 60
    start = max(0, decision_i - window + 1)
    hist = []
    for row in rows:
        index = features.sym[row["symbol"]]
        hist.append(features.ret[index, start : decision_i + 1])
    hist_arr = np.vstack(hist)
    port = weights @ hist_arr
    finite = port[np.isfinite(port)]
    target = float(rows[0]["intent"].vol_target or 0.20)
    gross = 1.0
    if finite.size >= 20:
        sigma = float(np.std(finite, ddof=1))
        ann = sigma * np.sqrt(365.0)
        if ann > 0:
            gross = target / ann
    # raw_weight is the fraction of equity. sum |raw| = gross.
    scaled = []
    for row, weight in zip(rows, weights, strict=False):
        copied = dict(row)
        copied["raw_weight"] = abs(float(weight)) * gross
        scaled.append(copied)
    return scale_to_caps(scaled, equity)


def simulate(features: Features, decide_name: str, params: dict, start_ms: int, end_ms: int, initial_equity: float) -> SimResult:
    """Simulate ``(start_ms, end_ms)`` with a flat book at the open of the window.

    ``end_ms`` is exclusive. History before ``start_ms`` is visible to signals.
    """
    decide = DECISIONS[decide_name]
    day = features.day_ms
    start_i = int(np.searchsorted(day, int(start_ms), side="left"))
    end_i = int(np.searchsorted(day, int(end_ms), side="left")) - 1
    if end_i < start_i or start_i >= day.size:
        empty = np.array([initial_equity])
        return SimResult(
            day_ms=day[start_i : start_i + 1] if start_i < day.size else np.array([start_ms]),
            equity=empty,
            fees=0.0,
            slippage=0.0,
            funding_pnl=0.0,
            turnover=0.0,
            days_in_market=0,
            avg_names=0.0,
            avg_gross=0.0,
            avg_net=0.0,
            start_equity=initial_equity,
            end_equity=initial_equity,
        )
    fee_rate = float(params.get("fee_rate", 0.0005))
    cash = float(initial_equity)
    positions: dict[str, Position] = {}
    suspend: dict[str, int] = {}
    pending: tuple[int, list[Intent]] | None = None
    equities = []
    days = []
    fees = 0.0
    slippage = 0.0
    funding_pnl = 0.0
    turnover = 0.0
    name_sum = 0.0
    gross_sum = 0.0
    net_sum = 0.0
    in_market = 0
    trades: list[dict] = []
    rebalances = 0
    weeks_flat = 0
    observed = 0

    def mark_of(pos: Position, i: int) -> float:
        index = features.sym[pos.symbol]
        price = float(features.close[index, i])
        if _finite_price(price):
            pos.last_mark = price
            return price
        opened = float(features.open[index, i])
        if _finite_price(opened):
            pos.last_mark = opened
            return opened
        return pos.last_mark

    def equity_at(i: int) -> float:
        total = cash
        for pos in positions.values():
            total += pos.qty * mark_of(pos, i)
        return total

    def apply_cash(pos: Position | None, symbol: str, delta: float, fill: float, fee: float) -> Position | None:
        nonlocal cash, fees, slippage, turnover
        cash -= delta * fill + fee
        fees += fee
        turnover += abs(delta) * fill
        if pos is not None:
            pos.gross_cash -= delta * fill + fee
        return pos

    def close_position(symbol: str, fill: float, i: int, reason: str) -> None:
        nonlocal cash, fees, slippage, turnover
        pos = positions.get(symbol)
        if pos is None or pos.qty == 0:
            positions.pop(symbol, None)
            return
        index = features.sym[symbol]
        slip = features.slip(index, i, params)
        # Selling a long (qty>0) is a sell: fill is worse, lower.
        direction = -1 if pos.qty > 0 else 1
        slipped = fill * (1.0 + direction * slip)
        fee = abs(pos.qty) * slipped * fee_rate
        slip_dollars = abs(slipped - fill) * abs(pos.qty)
        slippage += slip_dollars
        delta = -pos.qty
        apply_cash(pos, symbol, delta, slipped, fee)
        pnl = pos.gross_cash
        risk = pos.risk_dollars
        r_mult = pnl / risk if risk > 0 else None
        trades.append(
            {
                "symbol": symbol,
                "side": "long" if pos.side > 0 else "short",
                "entry_i": pos.entry_i,
                "exit_i": i,
                "entry_time": int(features.day_ms[pos.entry_i]),
                "exit_time": int(features.day_ms[i]),
                "pnl": pnl,
                "r_multiple": r_mult,
                "exit_reason": reason,
            }
        )
        del positions[symbol]

    def open_or_resize(intent: Intent, i: int) -> None:
        nonlocal cash, fees, slippage, turnover
        index = features.sym[intent.symbol]
        raw = float(features.open[index, i])
        if not _finite_price(raw):
            return
        notional = float(getattr(intent, "_notional", 0.0))
        desired = intent.side * notional / raw
        held = positions.get(intent.symbol)
        current = held.qty if held else 0.0
        delta = desired - current
        if abs(delta) * raw < 1e-9 and held is not None:
            return
        slip = features.slip(index, i, params)
        direction = 1 if delta > 0 else -1
        if abs(delta) > 0:
            fill = raw * (1.0 + direction * slip)
            fee = abs(delta) * abs(fill) * fee_rate
            slippage += abs(fill - raw) * abs(delta)
            if held is None:
                held = Position(
                    symbol=intent.symbol,
                    side=intent.side,
                    qty=0.0,
                    entry_price=fill,
                    entry_i=i,
                    stop_price=fill,
                    initial_stop=fill,
                    exit_mode=intent.exit_mode,
                    trail_mult=intent.trail_mult,
                    channel_n=intent.channel_n,
                    hard_stop=intent.hard_stop_price,
                    target_price=None,
                    time_stop_i=None,
                    shock_dir=intent.shock_dir,
                    consec=0,
                    cluster=intent.cluster,
                    sticky_trail=intent.sticky_trail,
                    last_mark=raw,
                    decision_atr=intent.decision_atr,
                )
                positions[intent.symbol] = held
            apply_cash(held, intent.symbol, delta, fill, fee)
            held.qty = desired
        if held is None:
            return
        reset = intent.reset_entry or held.side != intent.side or held.qty == 0
        # A same-side resize that is not a fresh entry keeps the stop.
        if intent.reset_entry or held.entry_i == i and abs(current) < 1e-12:
            reset = True
        if held.side != intent.side:
            reset = True
        held.side = intent.side
        held.exit_mode = intent.exit_mode
        held.trail_mult = intent.trail_mult
        held.channel_n = intent.channel_n
        held.sticky_trail = intent.sticky_trail
        held.shock_dir = intent.shock_dir
        held.decision_atr = intent.decision_atr
        if intent.hard_stop_price is not None:
            held.hard_stop = float(intent.hard_stop_price)
        if reset:
            entry = raw * (1.0 + (1 if intent.side > 0 else -1) * features.slip(index, i, params))
            held.entry_price = entry
            held.entry_i = i
            atr = intent.decision_atr or 0.0
            if intent.exit_mode == "event" and intent.hard_stop_price is not None:
                held.stop_price = float(intent.hard_stop_price)
            elif intent.trail_mult and atr and np.isfinite(atr):
                held.stop_price = entry - intent.side * float(intent.trail_mult) * float(atr)
            else:
                # No price stop (buy-and-hold, notional control). NaN never compares true.
                held.stop_price = float("nan")
            held.initial_stop = held.stop_price
            distance = abs(entry - held.initial_stop) if np.isfinite(held.initial_stop) else 0.0
            held.risk_dollars = abs(held.qty) * distance if np.isfinite(distance) else 0.0
            if intent.target_r and distance > 0:
                held.target_price = entry + intent.side * float(intent.target_r) * distance
            else:
                held.target_price = None
            if intent.time_stop_days:
                held.time_stop_i = i + int(intent.time_stop_days) - 1
            else:
                held.time_stop_i = None
            held.consec = 0
            # gross_cash already includes this fill. Risk is for the whole spell.
        held.last_mark = raw

    for i in range(start_i, end_i + 1):
        if pending is not None and i > start_i:
            decision_i, intents = pending
            wanted: dict[str, set[int]] = {}
            for intent in intents:
                wanted.setdefault(intent.symbol, set()).add(intent.side)
            for symbol, side in list(suspend.items()):
                if side not in wanted.get(symbol, ()):
                    del suspend[symbol]
            filtered = [intent for intent in intents if suspend.get(intent.symbol) != intent.side]
            equity_now = equity_at(i)
            targets = _size(filtered, positions, equity_now, features, i, decision_i) if equity_now > 0 else {}
            if intents == []:
                weeks_flat += 1
            elif filtered:
                rebalances += 1
            for symbol in list(positions):
                if symbol not in targets:
                    index = features.sym[symbol]
                    raw = float(features.open[index, i])
                    price = raw if _finite_price(raw) else positions[symbol].last_mark
                    if _finite_price(price):
                        close_position(symbol, price, i, "rebalance")
            for symbol, intent in targets.items():
                held = positions.get(symbol)
                if held is not None and held.side != intent.side:
                    index = features.sym[symbol]
                    raw = float(features.open[index, i])
                    price = raw if _finite_price(raw) else held.last_mark
                    close_position(symbol, price, i, "flip")
                open_or_resize(intent, i)
            pending = None

        # Gap through an event hard stop at the open.
        for symbol in list(positions):
            pos = positions[symbol]
            if pos.exit_mode != "event" or pos.hard_stop is None:
                continue
            index = features.sym[symbol]
            raw = float(features.open[index, i])
            if not _finite_price(raw):
                continue
            breached = (pos.side > 0 and raw <= pos.hard_stop) or (pos.side < 0 and raw >= pos.hard_stop)
            if breached:
                close_position(symbol, raw, i, "gap_stop")

        # Funding inside (today's open, next open]. Charged if we still hold after the open.
        hi = int(features.day_ms[i]) + DAY_MS
        lo = int(features.day_ms[i])
        for symbol, pos in list(positions.items()):
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
            mark = mark_of(pos, i)
            for rate in rates[left:right]:
                if not np.isfinite(rate):
                    continue
                change = -pos.qty * mark * float(rate)
                cash += change
                pos.gross_cash += change
                funding_pnl += change

        # Intrabar event stop and target. Stop wins if both trade.
        for symbol in list(positions):
            pos = positions[symbol]
            if pos.exit_mode != "event" or pos.hard_stop is None:
                continue
            index = features.sym[symbol]
            raw_open = float(features.open[index, i])
            high = float(features.high[index, i])
            low = float(features.low[index, i])
            if not (_finite_price(raw_open) and np.isfinite(high) and np.isfinite(low)):
                continue
            # Already gap-checked. Intrabar uses the range beyond the open.
            stop_hit = (pos.side > 0 and low <= pos.hard_stop) or (pos.side < 0 and high >= pos.hard_stop)
            target_hit = False
            if pos.target_price is not None:
                target_hit = (pos.side > 0 and high >= pos.target_price) or (pos.side < 0 and low <= pos.target_price)
            if stop_hit:
                close_position(symbol, float(pos.hard_stop), i, "stop")
            elif target_hit:
                close_position(symbol, float(pos.target_price), i, "target")

        # Exposure after the open and any intrabar exit, before the close exit.
        eq_pre = equity_at(i)
        gross = 0.0
        net = 0.0
        if eq_pre > 0:
            for pos in positions.values():
                notion = pos.qty * mark_of(pos, i)
                gross += abs(notion)
                net += notion
            gross /= eq_pre
            net /= eq_pre
        name_sum += len(positions)
        gross_sum += gross
        net_sum += net
        if positions:
            in_market += 1
        observed += 1

        # Close-based exits.
        for symbol in list(positions):
            pos = positions[symbol]
            index = features.sym[symbol]
            price = float(features.close[index, i])
            if not _finite_price(price):
                # No bar: the contract did not print. Exit at the last mark.
                if _finite_price(pos.last_mark):
                    close_position(symbol, pos.last_mark, i, "delist")
                continue
            pos.last_mark = price
            prev = float(features.close[index, i - 1]) if i > 0 else float("nan")
            if pos.shock_dir != 0 and np.isfinite(prev):
                if (price - prev) * pos.shock_dir > 0:
                    pos.consec += 1
                else:
                    pos.consec = 0
                if pos.consec >= 2:
                    close_position(symbol, price, i, "two_close")
                    continue
            if pos.time_stop_i is not None and i >= pos.time_stop_i:
                close_position(symbol, price, i, "time_stop")
                continue
            channel_hit = False
            if pos.channel_n and pos.exit_mode == "trail":
                level = _prior_level(features, index, i, int(pos.channel_n), pos.side)
                if np.isfinite(level):
                    channel_hit = (pos.side > 0 and price < level) or (pos.side < 0 and price > level)
            trail_hit = False
            stop_is_live = np.isfinite(pos.stop_price)
            if stop_is_live and (pos.exit_mode in ("trail", "rebalance") or (pos.exit_mode == "event" and pos.trail_mult)):
                trail_hit = (pos.side > 0 and price <= pos.stop_price) or (pos.side < 0 and price >= pos.stop_price)
            if channel_hit or trail_hit:
                reason = "channel" if channel_hit and not trail_hit else "trail_stop"
                sticky = pos.sticky_trail and trail_hit
                side = pos.side
                close_position(symbol, price, i, reason)
                if sticky:
                    suspend[symbol] = side
                continue
            # Ratchet a trail after a close that did not stop out. The new stop
            # cannot be tested on this same close.
            if pos.trail_mult and pos.exit_mode in ("trail", "event"):
                atr = float(features.atr[index, i])
                if np.isfinite(atr) and atr > 0:
                    proposed = price - pos.side * float(pos.trail_mult) * atr
                    if pos.side > 0:
                        pos.stop_price = max(pos.stop_price, proposed)
                    else:
                        pos.stop_price = min(pos.stop_price, proposed)

        if i == end_i:
            for symbol in list(positions):
                pos = positions[symbol]
                price = mark_of(pos, i)
                if _finite_price(price):
                    close_position(symbol, price, i, "end_of_window")
            equities.append(cash)
            days.append(int(features.day_ms[i]))
            break

        equities.append(equity_at(i))
        days.append(int(features.day_ms[i]))
        held = {symbol: pos.side for symbol, pos in positions.items()}
        decision = decide(features, i, held, params)
        if decision is None:
            pending = None
        else:
            pending = (i, decision)
        if equities[-1] <= 0:
            pending = (i, [])

    equity_arr = np.array(equities, dtype=float)
    n = max(observed, 1)
    return SimResult(
        day_ms=np.array(days, dtype=np.int64),
        equity=equity_arr,
        fees=fees,
        slippage=slippage,
        funding_pnl=funding_pnl,
        turnover=turnover,
        days_in_market=in_market,
        avg_names=name_sum / n,
        avg_gross=gross_sum / n,
        avg_net=net_sum / n,
        trades=trades,
        start_equity=float(initial_equity),
        end_equity=float(equity_arr[-1]) if equity_arr.size else float(initial_equity),
        rebalances=rebalances,
        weeks_flat=weeks_flat,
    )


def simulate_buy_hold(features: Features, start_ms: int, end_ms: int, initial_equity: float, params: dict | None = None) -> SimResult:
    """1x long BTCUSDT, entered at the first open, funded, one entry and one exit."""
    params = dict(params or {})
    params.setdefault("fee_rate", 0.0005)
    params.setdefault("slip_top", 0.0004)
    params.setdefault("slip_rest", 0.0008)

    def _once(features, i, held, _params):
        if held:
            return None
        if features.btc is None:
            return []
        return [
            Intent(
                symbol="BTCUSDT",
                side=1,
                size_mode="fixed",
                raw_weight=1.0,
                risk_atr_mult=1.0,
                exit_mode="rebalance",
                trail_mult=None,
                cluster="btc",
                reset_entry=True,
                decision_atr=1.0,
            )
        ]

    DECISIONS["btc_hold"] = _once
    try:
        # The fixed sizer uses stop_pct only for metadata. decision_atr=1 and
        # risk_atr_mult=1 would imply a 100% stop if we went through equal-risk.
        # size_mode fixed ignores that.
        return simulate(features, "btc_hold", params, start_ms, end_ms, initial_equity)
    finally:
        DECISIONS.pop("btc_hold", None)
