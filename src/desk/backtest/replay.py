"""Walk closed bars in time order and trade only scanner packets that pass.

Stops are assumed to fill before a target when both sit inside the same bar.
A stop that gaps through fills at the open. Entry at the 1H close is not
managed on that same bar, because the bar's range happened before the close.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from desk.backtest.costs import adverse_price, directional_pnl, slippage_fraction, taker_fee, tp1_price
from desk.backtest.metrics import equity_from_flows, per_setup_curves, period_bounds, summarize_trades
from desk.backtest.walkforward import select_trades, walk_forward_folds
from desk.indicators.core import add_indicators, ema_column
from desk.rules.exits import (
    breakeven_stop,
    chandelier_stop,
    closed_structure_window,
    daily_bias_invalidated,
    invalidated,
    reached_one_r,
    stop_fill_price,
    stop_hit,
    structure_stop,
    target_hit,
    time_stop_due,
    trail_is_armed,
    trail_stop,
)
from desk.scanner.engine import evaluate_frame_at
from desk.timeutils import DAY_MS, H4_MS, HOUR_MS, ms_to_utc_iso, myt_date

SURVIVORSHIP_NOTE = (
    "Binance USDT-M exchangeInfo returns contracts listed at download time. "
    "Delisted perpetuals are absent unless a historical archive was imported into the symbols table. "
    "A backtest on today's listings is biased toward contracts that survived. "
    "Volume, listing age, and settled funding are applied as of each bar. "
    "Historical order-book spreads are not in klines; the configured assumed spread is used when the book is missing. "
    "The next predicted funding rate is not archived, so the backtest reuses the last settled rate when configured and says so. "
    "News vetoes are not a market-data series. Setup D on-chain checks stay MISSING and fail closed unless a caller supplies them."
)


@dataclass
class Position:
    symbol: str
    setup: str
    direction: str
    cluster_key: str
    signal_id: str
    entry_time: int
    entry_index: int
    fill: float
    initial_stop: float
    stop: float
    tp1: float
    size: float
    remaining: float
    partial_done: bool
    reached_1r: bool
    bars_4h: int
    fees: float
    funding: float
    realized: float
    flows: list[tuple[int, float]] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    time_stop_bars: int = 8
    hard_max_bars: int | None = None
    spread_frac: float = 0.0
    partial_fraction: float = 0.5
    trail_extreme: float | None = None
    mfe_r: float = 0.0
    mae_r: float = 0.0


def _week_key(ms: int):
    day = myt_date(ms)
    return day.toordinal() - day.weekday()


def _live_risk(pos: Position) -> float:
    if pos.direction == "long":
        per = max(0.0, pos.fill - pos.stop)
    else:
        per = max(0.0, pos.stop - pos.fill)
    return per * pos.remaining


def _slip(settings: dict, spread_frac: float) -> float:
    return slippage_fraction(settings, spread_frac)


def _market_exit_price(direction: str, raw: float, slip: float) -> float:
    return adverse_price(raw, direction, is_entry=False, slip=slip)


def _close_qty(pos: Position, qty: float, price: float, ts: int, fee_rate: float) -> float:
    gross = directional_pnl(pos.direction, qty, pos.fill, price)
    fee = taker_fee(price * qty, fee_rate)
    net = gross - fee
    pos.fees += fee
    pos.realized += net
    pos.flows.append((ts, net))
    pos.remaining -= qty
    pos.meta["last_fill_price"] = price
    return net


def _funding_payment(pos: Position, rate: float, mark: float, ts: int) -> float:
    notional = pos.remaining * mark
    signed = notional * rate
    # Positive funding: longs pay, shorts receive.
    cash = -signed if pos.direction == "long" else signed
    pos.funding += cash
    pos.realized += cash
    pos.flows.append((ts, cash))
    return cash


def _record_excursion(pos: Position, high: float, low: float) -> None:
    risk = abs(pos.fill - pos.initial_stop)
    if risk <= 0:
        return
    if pos.direction == "long":
        pos.mfe_r = max(pos.mfe_r, (high - pos.fill) / risk)
        pos.mae_r = max(pos.mae_r, (pos.fill - low) / risk)
    else:
        pos.mfe_r = max(pos.mfe_r, (pos.fill - low) / risk)
        pos.mae_r = max(pos.mae_r, (high - pos.fill) / risk)


def _apply_trail(pos: Position, row4h, settings: dict, ema_fast_col: str, recent_highs, recent_lows) -> None:
    """Move the stop only in the trade's favor, and only after the configured arm."""
    exits = settings["exits"]
    arm = str(exits.get("trail_arm", "immediate"))
    if not trail_is_armed(arm, partial_done=pos.partial_done, reached_1r=pos.reached_1r):
        return
    if not pd.notna(row4h.get("atr_14")):
        return
    atr = float(row4h["atr_14"])
    if atr <= 0:
        return
    mode = str(exits.get("trail_mode", "ema"))
    direction = pos.direction
    if mode == "ema":
        if not pd.notna(row4h.get(ema_fast_col)):
            return
        pos.stop = trail_stop(
            pos.stop,
            direction,
            float(row4h[ema_fast_col]),
            atr,
            float(row4h["close"]),
            float(exits["trail_ema_atr_mult"]),
            float(exits["trail_close_atr_mult"]),
        )
        return
    if mode == "chandelier":
        high = float(row4h["high"])
        low = float(row4h["low"])
        if direction == "long":
            pos.trail_extreme = high if pos.trail_extreme is None else max(pos.trail_extreme, high)
        else:
            pos.trail_extreme = low if pos.trail_extreme is None else min(pos.trail_extreme, low)
        pos.stop = chandelier_stop(
            pos.stop,
            direction,
            pos.trail_extreme,
            atr,
            float(exits.get("chandelier_atr_mult", 2.0)),
        )
        return
    if mode == "structure":
        highs = list(recent_highs or [float(row4h["high"])])
        lows = list(recent_lows or [float(row4h["low"])])
        pos.stop = structure_stop(
            pos.stop,
            direction,
            highs,
            lows,
            atr,
            float(exits.get("structure_atr_buffer", 0.25)),
        )
        return
    raise ValueError(f"unknown trail_mode {mode}")


def manage_bar(
    pos: Position,
    bar,
    row4h,
    row1d,
    settings: dict,
    ema_fast_col: str,
    ema_slow_col: str,
    recent_highs=None,
    recent_lows=None,
) -> str | None:
    """Return an exit reason when the position is fully closed on this bar.

    The stop is tested before the target. A trail update on this 4H close
    applies to later bars only, so the current bar's own extreme is not used
    as a fill on this bar.
    """
    direction = pos.direction
    fee = float(settings["costs"]["taker_fee_rate"])
    slip = _slip(settings, pos.spread_frac)
    high = float(bar["high"])
    low = float(bar["low"])
    open_ = float(bar["open"])
    close = float(bar["close"])
    ts = int(bar["close_time"])

    if stop_hit(direction, pos.stop, high, low):
        raw = stop_fill_price(direction, pos.stop, open_)
        price = _market_exit_price(direction, raw, slip)
        _record_excursion(pos, price, price)
        reason = "hard_stop" if abs(pos.stop - pos.initial_stop) <= 1e-9 else "trail_stop"
        _close_qty(pos, pos.remaining, price, ts, fee)
        return reason

    _record_excursion(pos, high, low)

    if not pos.partial_done and target_hit(direction, pos.tp1, high, low):
        qty = pos.size * pos.partial_fraction
        qty = min(qty, pos.remaining)
        _close_qty(pos, qty, pos.tp1, ts, fee)
        pos.partial_done = True
        pos.reached_1r = True
        pos.stop = breakeven_stop(pos.stop, pos.fill, direction)
        if pos.remaining <= 1e-12:
            return "take_profit"

    if reached_one_r(direction, pos.fill, pos.initial_stop, high, low):
        pos.reached_1r = True

    if row4h is not None:
        pos.bars_4h += 1
        if invalidated(pos.setup, direction, pos.bars_4h, row4h, pos.meta):
            price = _market_exit_price(direction, close, slip)
            _close_qty(pos, pos.remaining, price, ts, fee)
            return "invalidation"
        if daily_bias_invalidated(pos.setup, direction, row1d, ema_slow_col):
            price = _market_exit_price(direction, close, slip)
            _close_qty(pos, pos.remaining, price, ts, fee)
            return "invalidation"
        reason = time_stop_due(pos.setup, pos.bars_4h, pos.time_stop_bars, pos.reached_1r, pos.hard_max_bars)
        if reason:
            price = _market_exit_price(direction, close, slip)
            _close_qty(pos, pos.remaining, price, ts, fee)
            return reason
        _apply_trail(pos, row4h, settings, ema_fast_col, recent_highs, recent_lows)
    return None


def _historical_status(meta: dict) -> str:
    """A stored bar means the contract was listed then, including names later delisted.

    The catalog status stays on the symbol row and is counted as delisted. The
    product gate sees TRADING for USDT-M perpetuals while their klines exist.
    """
    contract = str(meta.get("contract_type") or "PERPETUAL").upper()
    quote = str(meta.get("quote_asset") or "USDT").upper()
    if contract == "PERPETUAL" and quote == "USDT":
        return "TRADING"
    return str(meta.get("status") or "")


def _structure_arrays(df4h: pd.DataFrame, open_time: int, settings: dict) -> tuple[list[float], list[float]]:
    opens = df4h["open_time"].to_numpy()
    index = int(np.searchsorted(opens, int(open_time), side="right") - 1)
    lookback = int(settings["exits"].get("structure_lookback", 3))
    return closed_structure_window(
        df4h["high"].astype(float).tolist(),
        df4h["low"].astype(float).tolist(),
        index,
        lookback,
    )


def _row_at(df: pd.DataFrame | None, open_time: int):
    if df is None or df.empty:
        return None
    matched = df[df["open_time"] == open_time]
    if matched.empty:
        return None
    return matched.iloc[-1]


def _finished_trade(pos: Position, exit_time: int, exit_reason: str) -> dict:
    risk = pos.size * abs(pos.fill - pos.initial_stop)
    pnl = pos.realized
    return {
        "symbol": pos.symbol,
        "setup": pos.setup,
        "direction": pos.direction,
        "signal_id": pos.signal_id,
        "entry_time": pos.entry_time,
        "exit_time": exit_time,
        "pnl": pnl,
        "initial_risk": risk,
        "r_multiple": (pnl / risk) if risk else None,
        "exit_reason": exit_reason,
        "fees": pos.fees,
        "funding": pos.funding,
        "partial_taken": pos.partial_done,
        "mfe_r": pos.mfe_r,
        "mae_r": pos.mae_r,
        "flows": list(pos.flows),
    }


def _open_from_packet(packet: dict, bar_index: int, settings: dict) -> Position | None:
    spread = float(packet.get("spread_frac") or 0.0)
    slip = _slip(settings, spread)
    direction = packet["direction"]
    fill = adverse_price(float(packet["entry"]), direction, is_entry=True, slip=slip)
    stop = float(packet["stop"])
    if direction == "long" and stop >= fill:
        return None
    if direction == "short" and stop <= fill:
        return None
    fee_rate = float(settings["costs"]["taker_fee_rate"])
    partial = float(settings["exits"]["partial_at_r"])
    tp1 = tp1_price(
        fill,
        stop,
        direction,
        partial,
        fee_rate,
        spread,
        bool(settings["costs"]["cost_bump_tp1"]),
    )
    size = float(packet["size_coins"])
    entry_fee = taker_fee(fill * size, fee_rate)
    pos = Position(
        symbol=packet["symbol"],
        setup=packet["setup"],
        direction=direction,
        cluster_key=packet["cluster_key"],
        signal_id=packet["signal_id"],
        entry_time=int(packet["ts_ms"]),
        entry_index=bar_index,
        fill=fill,
        initial_stop=stop,
        stop=stop,
        tp1=tp1,
        size=size,
        remaining=size,
        partial_done=False,
        reached_1r=False,
        bars_4h=0,
        fees=entry_fee,
        funding=0.0,
        realized=-entry_fee,
        meta=dict(packet.get("meta") or {}),
        time_stop_bars=int(packet["time_stop_bars"]),
        hard_max_bars=packet.get("hard_max_bars"),
        spread_frac=spread,
        partial_fraction=float(packet["partial_fraction"]),
    )
    pos.flows.append((pos.entry_time, -entry_fee))
    return pos


def _account_view(equity: float, day_start: float, week_start: float, settings: dict) -> dict:
    daily = None if day_start <= 0 else (equity - day_start) / day_start
    weekly = None if week_start <= 0 else (equity - week_start) / week_start
    return {
        "equity_usdt": equity,
        "daily_pnl_pct": daily,
        "weekly_pnl_pct": weekly,
        "news_status": settings["backtest"].get("news_status"),
    }


def _apply_due_funding(pos: Position, funding: pd.DataFrame | None, prev_close: int, bar_close: int, mark: float) -> None:
    if funding is None or funding.empty or pos is None:
        return
    due = funding[(funding["funding_time"] > max(prev_close, pos.entry_time)) & (funding["funding_time"] <= bar_close)]
    for _, row in due.iterrows():
        _funding_payment(pos, float(row["funding_rate"]), mark, int(row["funding_time"]))


def run_backtest(
    *,
    symbols: dict[str, dict],
    btc_daily: pd.DataFrame,
    settings: dict,
    delisted_symbols_included: int = 0,
    onchain_raw: dict | None = None,
) -> dict:
    """symbols[name] = {1h, 4h, 1d, funding, market_meta}.

    market_meta supplies base_asset, onboard_date_ms, status, contract_type.
    """
    prepared = {}
    for symbol, blob in symbols.items():
        prepared[symbol] = {
            "1h": add_indicators(blob["1h"].sort_values("open_time").reset_index(drop=True), settings),
            "4h": add_indicators(blob["4h"].sort_values("open_time").reset_index(drop=True), settings),
            "1d": add_indicators(blob["1d"].sort_values("open_time").reset_index(drop=True), settings),
            "funding": blob.get("funding"),
            "meta": blob.get("meta") or {},
        }
    btc = add_indicators(btc_daily.sort_values("open_time").reset_index(drop=True), settings)
    timeline = sorted({int(t) for blob in prepared.values() for t in blob["1h"]["open_time"]})
    index_of = {
        symbol: {int(row.open_time): i for i, row in blob["1h"].iterrows()}
        for symbol, blob in prepared.items()
    }

    start_equity = float(settings["backtest"]["initial_equity"])
    realized = 0.0
    positions: dict[str, Position] = {}
    trades: list[dict] = []
    rejects: Counter = Counter()
    proposals = 0
    equity_points: list[tuple[int, float]] = []
    day_key = None
    week_key = None
    day_start = start_equity
    week_start = start_equity
    ema_fast = ema_column(settings, "fast")
    ema_slow = ema_column(settings, "slow")
    prev_close = 0

    def mark_equity(ts: int, mark_prices: dict[str, float]) -> float:
        unrealized = 0.0
        for sym, pos in positions.items():
            mark = mark_prices.get(sym, pos.fill)
            unrealized += directional_pnl(pos.direction, pos.remaining, pos.fill, mark)
        return start_equity + realized + unrealized

    for open_time in timeline:
        mark_prices = {}
        for symbol, blob in prepared.items():
            idx = index_of[symbol].get(open_time)
            if idx is None:
                continue
            bar = blob["1h"].iloc[idx]
            mark_prices[symbol] = float(bar["close"])
            bar_close = int(bar["close_time"])
            pos = positions.get(symbol)
            if pos is not None and pos.entry_index < idx:
                row4h = None
                row1d = None
                recent_highs = None
                recent_lows = None
                if open_time % H4_MS == 3 * HOUR_MS:
                    h4_open = open_time - 3 * HOUR_MS
                    row4h = _row_at(blob["4h"], h4_open)
                    if row4h is not None and str(settings["exits"].get("trail_mode", "ema")) == "structure":
                        recent_highs, recent_lows = _structure_arrays(blob["4h"], h4_open, settings)
                if open_time % DAY_MS == 23 * HOUR_MS:
                    row1d = _row_at(blob["1d"], open_time - 23 * HOUR_MS)
                reason = manage_bar(
                    pos, bar, row4h, row1d, settings, ema_fast, ema_slow, recent_highs, recent_lows
                )
                if reason is None and pos.remaining > 1e-12:
                    _apply_due_funding(pos, blob["funding"], prev_close, bar_close, float(bar["close"]))
                if reason is not None or pos.remaining <= 1e-12:
                    trade = _finished_trade(pos, bar_close, reason or "flat")
                    trades.append(trade)
                    if reason == "hard_stop":
                        # Caller may record a cooldown; the next evaluation reads it below.
                        pos.meta["cooldown_until"] = bar_close + int(settings["risk"]["cooldown_days"]) * DAY_MS
                        blob.setdefault("cooldown_until", pos.meta["cooldown_until"])
                        blob["cooldown_until"] = pos.meta["cooldown_until"]
                    del positions[symbol]
            elif pos is not None:
                _apply_due_funding(pos, blob["funding"], prev_close, bar_close, float(bar["close"]))

        # Realized is the sum of closed-trade pnl plus open-position realized.
        realized = sum(trade["pnl"] for trade in trades) + sum(pos.realized for pos in positions.values())
        if mark_prices:
            any_close = max(int(prepared[s]["1h"].iloc[index_of[s][open_time]]["close_time"]) for s in mark_prices)
        else:
            any_close = open_time + HOUR_MS - 1
        equity = mark_equity(any_close, mark_prices)
        equity_points.append((any_close, equity))

        this_day = myt_date(any_close)
        this_week = _week_key(any_close)
        if day_key is None:
            day_key = this_day
            day_start = start_equity
        elif this_day != day_key:
            day_key = this_day
            day_start = equity
        if week_key is None:
            week_key = this_week
            week_start = start_equity
        elif this_week != week_key:
            week_key = this_week
            week_start = equity

        account = _account_view(equity, day_start, week_start, settings)
        open_view = [
            {
                "symbol": pos.symbol,
                "setup": pos.setup,
                "direction": pos.direction,
                "cluster_key": pos.cluster_key,
                "risk_usdt": _live_risk(pos),
            }
            for pos in positions.values()
        ]

        for symbol, blob in prepared.items():
            idx = index_of[symbol].get(open_time)
            if idx is None or symbol in positions:
                continue
            meta = blob["meta"]
            market = {
                "symbol": symbol,
                "status": _historical_status(meta),
                "contract_type": meta.get("contract_type", "PERPETUAL"),
                "quote_asset": meta.get("quote_asset", "USDT"),
                "base_asset": meta.get("base_asset"),
                "onboard_date_ms": meta.get("onboard_date_ms"),
                "catalog_status": meta.get("status", "TRADING"),
                "bid": None,
                "ask": None,
                "funding_rate": _funding_as_of(blob["funding"], any_close),
                "predicted_funding_rate": None,
                "funding_predicted_source": "missing",
            }
            if settings["backtest"].get("funding_predicted_model") == "persist_last_settled":
                market["predicted_funding_rate"] = market["funding_rate"]
                market["funding_predicted_source"] = "persist_last_settled"
            packets = evaluate_frame_at(
                symbol=symbol,
                df1h=blob["1h"],
                df4h=blob["4h"],
                df1d=blob["1d"],
                btc_1d=btc,
                i_1h=idx,
                settings=settings,
                market=market,
                account=account,
                open_positions=open_view,
                cooldown_until_ms=blob.get("cooldown_until"),
                blocklist_symbols=set(settings["blocklist"].get("symbols") or []),
                blocklist_available=bool(settings["blocklist"].get("available", True)),
                onchain_raw=onchain_raw,
                historical=True,
            )
            opened = False
            for packet in packets:
                if packet["status"] != "proposed":
                    rejects[packet["votes"]["scanner"]["reason"]] += 1
                    continue
                if opened or symbol in positions:
                    rejects["second setup on the same symbol"] += 1
                    continue
                pos = _open_from_packet(packet, idx, settings)
                if pos is None:
                    rejects["fill crossed the stop"] += 1
                    continue
                proposals += 1
                positions[symbol] = pos
                open_view.append(
                    {
                        "symbol": pos.symbol,
                        "setup": pos.setup,
                        "direction": pos.direction,
                        "cluster_key": pos.cluster_key,
                        "risk_usdt": _live_risk(pos),
                    }
                )
                opened = True
        prev_close = any_close

    if settings["backtest"].get("close_at_end", True):
        for symbol, pos in list(positions.items()):
            blob = prepared[symbol]
            last = blob["1h"].iloc[-1]
            slip = _slip(settings, pos.spread_frac)
            price = _market_exit_price(pos.direction, float(last["close"]), slip)
            fee = float(settings["costs"]["taker_fee_rate"])
            _close_qty(pos, pos.remaining, price, int(last["close_time"]), fee)
            trades.append(_finished_trade(pos, int(last["close_time"]), "end_of_data"))
            equity_points.append((int(last["close_time"]), start_equity + sum(t["pnl"] for t in trades)))
            del positions[symbol]

    portfolio_flows = [flow for trade in trades for flow in trade["flows"]]
    # Open-trade flows are already inside `trades` after the end-of-data close.
    curve = equity_from_flows(start_equity, portfolio_flows) or [(timeline[0] if timeline else 0, start_equity)]
    # Prefer the marked curve captured during the loop; fall back if it is empty.
    if len(equity_points) >= 2:
        curve = equity_points
    overall = summarize_trades(trades, curve)
    setups = {}
    setup_curves = per_setup_curves(trades, start_equity)
    for setup in ("squeeze_breakout", "trend_pullback", "range_reclaim", "meme_momentum"):
        subset = [trade for trade in trades if trade["setup"] == setup]
        setups[setup] = summarize_trades(subset, setup_curves.get(setup, []))

    start_ms = min((int(blob["1h"]["open_time"].iloc[0]) for blob in prepared.values() if len(blob["1h"])), default=0)
    end_ms = max((int(blob["1h"]["close_time"].iloc[-1]) for blob in prepared.values() if len(blob["1h"])), default=0)
    folds = walk_forward_folds(
        start_ms,
        end_ms,
        int(settings["backtest"]["walk_forward_in_sample_days"]),
        int(settings["backtest"]["walk_forward_out_sample_days"]),
    )
    oos_trades = []
    fold_reports = []
    for fold in folds:
        oos = fold["out_of_sample"]
        chosen = select_trades(trades, oos["start_ms"], oos["end_ms"])
        oos_trades.extend(chosen)
        fold_reports.append(
            {
                "in_sample": {k: ms_to_utc_iso(v) for k, v in fold["in_sample"].items()},
                "out_of_sample": {k: ms_to_utc_iso(v) for k, v in oos.items()},
                "out_of_sample_metrics": summarize_trades(chosen, equity_from_flows(start_equity, [f for t in chosen for f in t["flows"]])),
            }
        )
    if folds:
        oos_metrics = summarize_trades(
            oos_trades,
            equity_from_flows(start_equity, [flow for trade in oos_trades for flow in trade["flows"]]),
        )
        walk_note = "Parameters are the frozen v0.3 defaults. Quote the out-of-sample block, not the in-sample block."
    else:
        oos_metrics = None
        walk_note = (
            "The sample is shorter than one in-sample window plus one out-of-sample window, "
            "so there is no out-of-sample figure to quote."
        )

    public_trades = [{k: v for k, v in trade.items() if k != "flows"} for trade in trades]
    return {
        "period": period_bounds(curve, trades),
        "symbols": sorted(prepared),
        "symbol_count": len(prepared),
        "delisted_symbols_included": delisted_symbols_included,
        "survivorship_note": SURVIVORSHIP_NOTE,
        "partial_at_r": float(settings["exits"]["partial_at_r"]),
        "costs": {
            "taker_fee_rate": float(settings["costs"]["taker_fee_rate"]),
            "slippage_rate": float(settings["costs"]["slippage_rate"]),
            "spread_model": "assumed" if settings["backtest"].get("allow_assumed_spread") else "book_or_fail",
            "assumed_spread_pct": float(settings["backtest"].get("assumed_spread_pct") or 0),
            "funding_predicted_model": settings["backtest"].get("funding_predicted_model"),
            "news_vetoes_modeled": False,
            "on_chain_modeled": onchain_raw is not None,
            "margin_mode_confirmed_on_exchange": False,
            "liquidation_model": "isolated_initial_margin_approximation",
            "same_bar_policy": "stop_before_target",
        },
        "proposals": proposals,
        "reject_counts": dict(rejects),
        "per_setup": setups,
        "overall": overall,
        "walk_forward": {
            "folds": fold_reports,
            "out_of_sample": oos_metrics,
            "note": walk_note,
        },
        "trades": public_trades,
        "caveats": [
            SURVIVORSHIP_NOTE,
            "News Watcher history is not applied; backtest.news_status is an explicit assumption, not a recorded veto feed.",
            "Setup D stays rejected while any on-chain check is MISSING.",
            "This process never places orders.",
        ],
    }


def simulate_entry(packet: dict, bar_index: int, settings: dict) -> Position | None:
    """Fill one packet with this module's fee and slippage. The paper loop uses it."""
    return _open_from_packet(packet, bar_index, settings)


def apply_funding_between(pos: Position, funding, prev_close: int, bar_close: int, mark: float) -> None:
    _apply_due_funding(pos, funding, prev_close, bar_close, mark)


def trade_record(pos: Position, exit_time: int, exit_reason: str) -> dict:
    trade = _finished_trade(pos, exit_time, exit_reason)
    price = pos.meta.get("last_fill_price")
    if price is not None:
        trade["exit_price"] = price
    return trade


def position_risk_usdt(pos: Position) -> float:
    return _live_risk(pos)


def force_exit(pos: Position, raw_price: float, ts: int, settings: dict, reason: str) -> dict:
    """Market out the remaining size. Slippage and the taker fee match manage_bar."""
    slip = _slip(settings, pos.spread_frac)
    price = _market_exit_price(pos.direction, raw_price, slip)
    fee = float(settings["costs"]["taker_fee_rate"])
    _close_qty(pos, pos.remaining, price, ts, fee)
    return trade_record(pos, ts, reason)


def _funding_as_of(funding: pd.DataFrame | None, ts: int) -> float | None:
    if funding is None or funding.empty:
        return None
    prior = funding[funding["funding_time"] <= ts]
    if prior.empty:
        return None
    return float(prior.iloc[-1]["funding_rate"])

