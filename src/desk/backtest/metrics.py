"""Trade and equity-curve metrics. Sharpe is daily and annualized with sqrt(365)."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict

from desk.timeutils import DAY_MS, ms_to_utc_iso


def max_drawdown(equities: list[float]) -> float | None:
    if not equities:
        return None
    peak = equities[0]
    worst = 0.0
    for equity in equities:
        if equity > peak:
            peak = equity
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def daily_equity(curve: list[tuple[int, float]]) -> list[tuple[int, float]]:
    if not curve:
        return []
    by_day: dict[int, float] = {}
    for ts, equity in curve:
        day = ts - (ts % DAY_MS)
        by_day[day] = equity
    return sorted(by_day.items())


def sharpe_ratio(curve: list[tuple[int, float]]) -> float | None:
    days = daily_equity(curve)
    if len(days) < 3:
        return None
    returns = []
    for i in range(1, len(days)):
        prev = days[i - 1][1]
        if prev <= 0:
            continue
        returns.append(days[i][1] / prev - 1.0)
    if len(returns) < 2:
        return None
    stdev = statistics.stdev(returns)
    if stdev == 0:
        return None
    return (statistics.fmean(returns) / stdev) * math.sqrt(365)


def summarize_trades(trades: list[dict], curve: list[tuple[int, float]]) -> dict:
    rs = [float(trade["r_multiple"]) for trade in trades if trade.get("r_multiple") is not None]
    count = len(trades)
    wins = sum(1 for value in rs if value > 0)
    return {
        "trade_count": count,
        "win_rate": (wins / count) if count else None,
        "avg_r": statistics.fmean(rs) if rs else None,
        "expectancy": statistics.fmean(rs) if rs else None,
        "max_drawdown": max_drawdown([equity for _, equity in curve]) if curve else None,
        "sharpe": sharpe_ratio(curve),
        "net_pnl": sum(float(trade["pnl"]) for trade in trades) if trades else 0.0,
        "end_of_data_exits": sum(1 for trade in trades if trade.get("exit_reason") == "end_of_data"),
    }


def equity_from_flows(start_equity: float, flows: list[tuple[int, float]]) -> list[tuple[int, float]]:
    ordered = sorted(flows, key=lambda item: item[0])
    equity = start_equity
    if not ordered:
        return []
    curve = [(ordered[0][0], start_equity)]
    for ts, amount in ordered:
        equity += amount
        curve.append((ts, equity))
    return curve


def per_setup_curves(trades: list[dict], start_equity: float) -> dict[str, list[tuple[int, float]]]:
    grouped: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for trade in trades:
        grouped[trade["setup"]].extend(trade.get("flows") or [(trade["exit_time"], trade["pnl"])])
    return {setup: equity_from_flows(start_equity, flows) for setup, flows in grouped.items()}


def fill_flat_days(curve: list[tuple[int, float]], start_ms: int | None = None, end_ms: int | None = None) -> list[tuple[int, float]]:
    """Insert one point per missing UTC day so flat periods count in the daily Sharpe."""
    if not curve and start_ms is None:
        return []
    points = list(curve)
    if start_ms is not None:
        equity = points[0][1] if points else 0.0
        if not points or start_ms < points[0][0]:
            points.insert(0, (int(start_ms), equity if points else equity))
    if end_ms is not None and points and end_ms > points[-1][0]:
        points.append((int(end_ms), points[-1][1]))
    if len(points) < 2:
        return points
    out = [points[0]]
    for ts, equity in points[1:]:
        prev_ts, prev_eq = out[-1]
        day = (prev_ts - (prev_ts % DAY_MS)) + DAY_MS
        last_day = ts - (ts % DAY_MS)
        while day < last_day:
            out.append((day + DAY_MS - 1, prev_eq))
            day += DAY_MS
        out.append((int(ts), equity))
    return out


def period_bounds(curve: list[tuple[int, float]], trades: list[dict]) -> dict:
    stamps = [ts for ts, _ in curve]
    if trades:
        stamps.append(min(int(trade["entry_time"]) for trade in trades))
        stamps.append(max(int(trade["exit_time"]) for trade in trades))
    if not stamps:
        return {"start": None, "end": None}
    return {"start": ms_to_utc_iso(min(stamps)), "end": ms_to_utc_iso(max(stamps))}
