"""Fee, spread, and slippage helpers used by sizing and the backtester.

Taker fee is charged per side. The cost-adjusted reward/risk uses a single
price distance of two taker fees plus the full spread, all measured off the
entry price. Funding is applied separately while a position is open.
"""

from __future__ import annotations


def cost_distance(entry: float, fee_rate: float, spread_frac: float) -> float:
    return abs(entry) * (2.0 * fee_rate + spread_frac)


def cost_adjusted_rr(
    entry: float,
    stop: float,
    tp1: float,
    fee_rate: float,
    spread_frac: float,
) -> float:
    costs = cost_distance(entry, fee_rate, spread_frac)
    tp_dist = abs(tp1 - entry)
    stop_dist = abs(entry - stop)
    denom = stop_dist + costs
    if denom <= 0:
        return 0.0
    return (tp_dist - costs) / denom


def tp1_price(
    entry: float,
    stop: float,
    direction: str,
    partial_at_r: float,
    fee_rate: float,
    spread_frac: float,
    cost_bump: bool,
) -> float:
    stop_dist = abs(entry - stop)
    if cost_bump:
        costs = cost_distance(entry, fee_rate, spread_frac)
        distance = partial_at_r * (stop_dist + costs) + costs
    else:
        distance = partial_at_r * stop_dist
    if direction == "long":
        return entry + distance
    return entry - distance


def adverse_price(price: float, direction: str, *, is_entry: bool, slip: float) -> float:
    """Move the fill against the trader by a fraction of price."""
    if price <= 0:
        return price
    bump = price * slip
    pay_up = (direction == "long" and is_entry) or (direction == "short" and not is_entry)
    return price + bump if pay_up else max(price - bump, 1e-12)


def slippage_fraction(settings: dict, spread_frac: float) -> float:
    """Half-spread plus the configured slippage allowance, as a fraction of price."""
    return abs(spread_frac) / 2.0 + float(settings["costs"]["slippage_rate"])


def directional_pnl(direction: str, qty: float, entry: float, exit_price: float) -> float:
    if direction == "long":
        return (exit_price - entry) * qty
    return (entry - exit_price) * qty


def taker_fee(notional: float, fee_rate: float) -> float:
    return abs(notional) * fee_rate
