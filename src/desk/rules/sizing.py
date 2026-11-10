"""Position sizing from rules v0.3 formula (1), plus leverage and notional caps.

size = equity * risk% * risk_scale / abs(entry - stop)

Leverage only changes margin and the simplified liquidation price. It does not
change dollar risk. The liquidation model is the isolated-wallet approximation

    long:  entry * (1 - 1/leverage + maintenance_margin_rate)
    short: entry * (1 + 1/leverage - maintenance_margin_rate)

which ignores tiered cumulative fees. If liquidation is not at least one 4H
ATR beyond the stop, leverage is reduced. The stop is never widened.
"""

from __future__ import annotations

from dataclasses import dataclass

from desk.backtest.costs import cost_adjusted_rr, tp1_price
from desk.rules.regime import SETUP_MEME


@dataclass
class SizePlan:
    ok: bool
    reason: str
    size_coins: float | None = None
    notional_usdt: float | None = None
    leverage: float | None = None
    risk_usdt: float | None = None
    risk_pct: float | None = None
    liq_price: float | None = None
    tp1: float | None = None
    reward_risk: float | None = None
    stop: float | None = None


def raw_size(equity: float, risk_pct: float, risk_scale: float, entry: float, stop: float) -> float:
    distance = abs(entry - stop)
    if equity <= 0 or risk_pct <= 0 or risk_scale <= 0 or distance <= 0 or entry <= 0:
        raise ValueError("size inputs must be positive and the stop must differ from entry")
    return (equity * risk_pct * risk_scale) / distance


def liquidation_price(entry: float, leverage: float, mmr: float, direction: str) -> float:
    if leverage <= 0 or entry <= 0:
        raise ValueError("leverage and entry must be positive")
    if direction == "long":
        return entry * (1.0 - (1.0 / leverage) + mmr)
    if direction == "short":
        return entry * (1.0 + (1.0 / leverage) - mmr)
    raise ValueError(f"unknown direction {direction}")


def liquidation_ok(liq: float, stop: float, atr: float, direction: str, buffer_mult: float) -> bool:
    buffer = buffer_mult * atr
    if direction == "long":
        return liq <= stop - buffer
    return liq >= stop + buffer


def select_leverage(
    entry: float,
    stop: float,
    atr: float,
    direction: str,
    max_leverage: int,
    mmr: float,
    buffer_mult: float,
) -> tuple[int, float] | None:
    for lev in range(int(max_leverage), 0, -1):
        liq = liquidation_price(entry, lev, mmr, direction)
        if liquidation_ok(liq, stop, atr, direction, buffer_mult):
            return lev, liq
    return None


def plan_position(
    *,
    equity: float,
    entry: float,
    stop: float,
    direction: str,
    setup: str,
    atr: float,
    spread_frac: float,
    settings: dict,
    last_1h_quote_volume: float | None = None,
) -> SizePlan:
    if entry <= 0 or atr is None or atr <= 0:
        return SizePlan(False, "entry or ATR is missing")
    if direction == "long" and stop >= entry:
        return SizePlan(False, "long stop must sit below entry")
    if direction == "short" and stop <= entry:
        return SizePlan(False, "short stop must sit above entry")
    stop_dist = abs(entry - stop)
    if stop_dist > float(settings["setups"][setup]["max_stop_atr"]) * atr:
        return SizePlan(False, "stop is farther than the setup ATR cap", stop=stop)

    risk_pct = float(settings["setups"][setup]["risk_pct"])
    scale = float(settings["risk"]["scale"])
    try:
        size = raw_size(equity, risk_pct, scale, entry, stop)
    except ValueError as exc:
        return SizePlan(False, str(exc))

    max_lev = int(settings["setups"][setup]["max_leverage"])
    max_lev = min(max_lev, int(settings["risk"]["max_leverage"]))
    chosen = select_leverage(
        entry,
        stop,
        atr,
        direction,
        max_lev,
        float(settings["risk"]["maintenance_margin_rate"]),
        float(settings["risk"]["liquidation_atr_buffer"]),
    )
    if chosen is None:
        return SizePlan(False, "liquidation buffer fails even at 1x", stop=stop)
    leverage, liq = chosen

    notional = size * entry
    cap_mult = (
        float(settings["risk"]["max_notional_equity_mult_setup_d"])
        if setup == SETUP_MEME
        else float(settings["risk"]["max_notional_equity_mult"])
    )
    notional_cap = cap_mult * equity
    capped = False
    if notional > notional_cap:
        capped = True
        notional = notional_cap
        size = notional / entry

    if setup == SETUP_MEME:
        fraction = float(settings["setups"]["meme_momentum"]["max_notional_of_1h_volume"])
        if last_1h_quote_volume is None or last_1h_quote_volume <= 0:
            return SizePlan(False, "Setup D 1H volume is missing", stop=stop)
        participation_cap = fraction * last_1h_quote_volume
        if notional > participation_cap:
            capped = True
            notional = participation_cap
            size = notional / entry

    risk_usdt = size * stop_dist
    implied_pct = risk_usdt / equity if equity else 0.0
    if capped and implied_pct < float(settings["risk"]["min_risk_after_cap_pct"]):
        return SizePlan(
            False,
            "notional cap cuts risk below the minimum",
            size_coins=size,
            notional_usdt=notional,
            leverage=leverage,
            risk_usdt=risk_usdt,
            risk_pct=risk_pct,
            liq_price=liq,
            stop=stop,
        )

    fee = float(settings["costs"]["taker_fee_rate"])
    partial_at_r = float(settings["exits"]["partial_at_r"])
    tp1 = tp1_price(
        entry,
        stop,
        direction,
        partial_at_r,
        fee,
        spread_frac,
        bool(settings["costs"]["cost_bump_tp1"]),
    )
    reward_risk = cost_adjusted_rr(entry, stop, tp1, fee, spread_frac)
    if settings["risk"]["enforce_min_reward_risk"] and reward_risk < float(settings["risk"]["min_reward_risk"]) - 1e-9:
        return SizePlan(
            False,
            "cost-adjusted reward/risk is below the minimum",
            size_coins=size,
            notional_usdt=notional,
            leverage=float(leverage),
            risk_usdt=risk_usdt,
            risk_pct=risk_pct,
            liq_price=liq,
            tp1=tp1,
            reward_risk=reward_risk,
            stop=stop,
        )

    return SizePlan(
        True,
        "ok",
        size_coins=size,
        notional_usdt=notional,
        leverage=float(leverage),
        risk_usdt=risk_usdt,
        risk_pct=risk_pct,
        liq_price=liq,
        tp1=tp1,
        reward_risk=reward_risk,
        stop=stop,
    )
