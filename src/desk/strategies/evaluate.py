"""Development walk-forward, then one hold-out pass on the frozen variant."""

from __future__ import annotations

import math

import numpy as np

from desk.backtest.walkforward import walk_forward_folds
from desk.strategies.catalog import family_variants, primary_variant
from desk.strategies.engine import SimResult, simulate, simulate_buy_hold
from desk.strategies.panel import Features
from desk.strategies.select import choose_frozen
from desk.strategies.stats import cagr, correlation, daily_returns, deflated_sharpe, max_drawdown, sharpe


def _years(n_days: int) -> float:
    return n_days / 365.0 if n_days else 0.0


def pack(result: SimResult, btc_returns: np.ndarray | None = None) -> dict:
    returns = daily_returns(result.equity)
    n_days = int(result.equity.size)
    total = None
    if result.start_equity > 0:
        total = result.end_equity / result.start_equity - 1.0
    avg_equity = float(np.mean(result.equity)) if n_days else result.start_equity
    years = _years(max(n_days - 1, 0))
    rs = [float(trade["r_multiple"]) for trade in result.trades if trade.get("r_multiple") is not None and np.isfinite(trade["r_multiple"])]
    expectancy = float(np.mean(rs)) if rs else None
    wins = sum(1 for value in rs if value > 0)
    corr = None
    if btc_returns is not None and returns.size:
        corr = correlation(returns, btc_returns[: returns.size] if btc_returns.size >= returns.size else btc_returns)
        if btc_returns.size != returns.size:
            # Align on the overlapping tail of equal length from the left when the windows match.
            n = min(returns.size, btc_returns.size)
            corr = correlation(returns[:n], btc_returns[:n])
    return {
        "cagr": cagr(result.start_equity, result.end_equity, max(n_days - 1, 1)),
        "sharpe": sharpe(returns),
        "max_dd": max_drawdown(result.equity),
        "total_return": total,
        "end_equity": result.end_equity,
        "fees": result.fees,
        "slippage": result.slippage,
        "funding_pnl": result.funding_pnl,
        "fees_pct": result.fees / result.start_equity if result.start_equity else None,
        "funding_pct": result.funding_pnl / result.start_equity if result.start_equity else None,
        "turnover": (result.turnover / avg_equity / years) if avg_equity and years else None,
        "turnover_dollars": result.turnover,
        "days": n_days,
        "days_in_market": result.days_in_market,
        "invested_frac": result.days_in_market / n_days if n_days else None,
        "avg_names": result.avg_names,
        "avg_gross": result.avg_gross,
        "avg_net": result.avg_net,
        "trades": len(result.trades),
        "expectancy_r": expectancy,
        "win_rate": (wins / len(rs)) if rs else None,
        "corr_btc": corr,
        "rebalances": result.rebalances,
        "weeks_flat": result.weeks_flat,
        "returns": returns,
        "equity": result.equity,
        "day_ms": result.day_ms,
    }


def _btc_returns(features: Features, start_ms: int, end_ms: int) -> np.ndarray:
    if features.btc is None:
        return np.array([])
    day = features.day_ms
    start_i = int(np.searchsorted(day, int(start_ms), side="left"))
    end_i = int(np.searchsorted(day, int(end_ms), side="left")) - 1
    if end_i <= start_i:
        return np.array([])
    # Daily close-to-close returns over the same dates the simulator records.
    # The simulator's first equity point is the close of start_i, and the first
    # return is from that close to the next, so use ret[start_i+1 : end_i+1].
    return features.ret[features.btc, start_i + 1 : end_i + 1]


def _stitch(results: list[SimResult], start_equity: float) -> SimResult:
    """Compound restarted windows into one equity path, in order."""
    if not results:
        return SimResult(
            day_ms=np.array([]),
            equity=np.array([start_equity]),
            fees=0.0,
            slippage=0.0,
            funding_pnl=0.0,
            turnover=0.0,
            days_in_market=0,
            avg_names=0.0,
            avg_gross=0.0,
            avg_net=0.0,
            start_equity=start_equity,
            end_equity=start_equity,
        )
    equity = start_equity
    curve = []
    days = []
    for result in results:
        if result.equity.size == 0 or result.start_equity <= 0:
            continue
        scaled = result.equity * (equity / result.start_equity)
        # Drop the first point of later windows so the restart close is not doubled.
        piece_eq = scaled if not curve else scaled[1:]
        piece_day = result.day_ms if not curve else result.day_ms[1:]
        curve.extend(piece_eq.tolist())
        days.extend(piece_day.tolist())
        equity = float(scaled[-1])
    fees = sum(result.fees for result in results)
    slippage = sum(result.slippage for result in results)
    funding = sum(result.funding_pnl for result in results)
    turnover = sum(result.turnover for result in results)
    days_in = sum(result.days_in_market for result in results)
    weight = sum(max(result.equity.size, 1) for result in results)
    return SimResult(
        day_ms=np.array(days, dtype=np.int64),
        equity=np.array(curve if curve else [start_equity], dtype=float),
        fees=fees,
        slippage=slippage,
        funding_pnl=funding,
        turnover=turnover,
        days_in_market=days_in,
        avg_names=sum(result.avg_names * max(result.equity.size, 1) for result in results) / weight,
        avg_gross=sum(result.avg_gross * max(result.equity.size, 1) for result in results) / weight,
        avg_net=sum(result.avg_net * max(result.equity.size, 1) for result in results) / weight,
        trades=[trade for result in results for trade in result.trades],
        start_equity=start_equity,
        end_equity=float(curve[-1]) if curve else start_equity,
        rebalances=sum(result.rebalances for result in results),
        weeks_flat=sum(result.weeks_flat for result in results),
    )


def run_variant(features: Features, spec: dict, start_ms: int, end_ms: int, equity: float) -> SimResult:
    return simulate(features, spec["decide"], spec["params"], start_ms, end_ms, equity)


def evaluate_family(
    features: Features,
    family: str,
    *,
    dev_start: int,
    dev_end: int,
    hold_start: int,
    hold_end: int,
    equity: float,
    in_sample_days: int,
    out_sample_days: int,
) -> dict:
    specs = family_variants(family)
    primary = primary_variant(family)["variant"]
    folds = walk_forward_folds(dev_start, dev_end, in_sample_days, out_sample_days)
    btc_dev = _btc_returns(features, dev_start, dev_end)
    btc_hold = _btc_returns(features, hold_start, hold_end)
    dev_rows = []
    fold_rows = []
    oos_by_variant: dict[str, list[SimResult]] = {spec["variant"]: [] for spec in specs}
    print(f"{family}: {len(specs)} variants, {len(folds)} walk-forward folds", flush=True)
    for spec in specs:
        print(f"  {spec['variant']} development", flush=True)
        dev = run_variant(features, spec, dev_start, dev_end, equity)
        packed = pack(dev, btc_dev)
        packed["variant"] = spec["variant"]
        packed["window"] = "development"
        dev_rows.append(packed)
        for fold_id, fold in enumerate(folds):
            ins = run_variant(
                features,
                spec,
                fold["in_sample"]["start_ms"],
                fold["in_sample"]["end_ms"],
                equity,
            )
            oos = run_variant(
                features,
                spec,
                fold["out_of_sample"]["start_ms"],
                fold["out_of_sample"]["end_ms"],
                equity,
            )
            ins_pack = pack(ins)
            oos_by_variant[spec["variant"]].append(oos)
            fold_rows.append(
                {
                    "family": family,
                    "variant": spec["variant"],
                    "fold": fold_id,
                    "sharpe": ins_pack["sharpe"],
                    "invested_days": ins.days_in_market,
                    "total_return": ins_pack["total_return"],
                    "window": "in_sample",
                }
            )
    pooled = {}
    for spec in specs:
        stitched = _stitch(oos_by_variant[spec["variant"]], equity)
        pooled[spec["variant"]] = pack(stitched, None)
        pooled[spec["variant"]]["variant"] = spec["variant"]
        pooled[spec["variant"]]["window"] = "walk_forward_oos"
    from desk.strategies.select import fold_winner

    votes = []
    for fold_id in range(len(folds)):
        rows = [row for row in fold_rows if row["fold"] == fold_id]
        votes.append(fold_winner(rows, primary))
    decision = choose_frozen(votes, {key: {"total_return": value["total_return"], "sharpe": value["sharpe"]} for key, value in pooled.items()}, primary)
    frozen_id = decision["frozen"]
    frozen_spec = next(spec for spec in specs if spec["variant"] == frozen_id)
    print(f"  frozen {frozen_id}: {decision['reason']}", flush=True)
    hold = run_variant(features, frozen_spec, hold_start, hold_end, equity)
    hold_pack = pack(hold, btc_hold)
    hold_pack["variant"] = frozen_id
    hold_pack["window"] = "holdout"
    trial_sharpes = [row["sharpe"] for row in dev_rows if row["sharpe"] is not None]
    frozen_dev = next(row for row in dev_rows if row["variant"] == frozen_id)
    frozen_dev["deflated_sharpe"] = deflated_sharpe(frozen_dev["returns"], [value for value in trial_sharpes if value is not None])
    hold_pack["deflated_sharpe"] = deflated_sharpe(hold_pack["returns"], [value for value in trial_sharpes if value is not None])
    frozen_dev["n_trials"] = len(specs)
    hold_pack["n_trials"] = len(specs)
    return {
        "family": family,
        "decision": decision,
        "folds": len(folds),
        "development": dev_rows,
        "walk_forward": pooled,
        "fold_rows": fold_rows,
        "holdout": hold_pack,
        "frozen": frozen_spec,
    }


def evaluate_benchmark(features: Features, start_ms: int, end_ms: int, equity: float, kind: str) -> dict:
    if kind == "btc":
        result = simulate_buy_hold(features, start_ms, end_ms, equity, {"fee_rate": 0.0005, "slip_top": 0.0004, "slip_rest": 0.0008})
    else:
        from desk.strategies.catalog import _BOOK

        result = simulate(features, "hedge", dict(_BOOK), start_ms, end_ms, equity)
    packed = pack(result, _btc_returns(features, start_ms, end_ms))
    packed["variant"] = kind
    return packed


def combine_returns(paths: list[np.ndarray]) -> np.ndarray:
    """Equal-weight daily simple returns. Shorter paths are not padded."""
    if not paths:
        return np.array([])
    n = min(path.size for path in paths)
    if n == 0:
        return np.array([])
    stacked = np.vstack([path[:n] for path in paths])
    with np.errstate(all="ignore"):
        return np.nanmean(stacked, axis=0)


def equity_from_returns(returns: np.ndarray, start: float) -> np.ndarray:
    equity = [start]
    for value in returns:
        step = 0.0 if not np.isfinite(value) else float(value)
        equity.append(equity[-1] * (1.0 + step))
    return np.array(equity, dtype=float)


def summarize_combined(returns: np.ndarray, start: float, btc_returns: np.ndarray | None) -> dict:
    equity = equity_from_returns(returns, start)
    fake = SimResult(
        day_ms=np.arange(equity.size),
        equity=equity,
        fees=0.0,
        slippage=0.0,
        funding_pnl=0.0,
        turnover=0.0,
        days_in_market=int(np.sum(np.abs(returns) > 0)) if returns.size else 0,
        avg_names=0.0,
        avg_gross=0.0,
        avg_net=0.0,
        start_equity=start,
        end_equity=float(equity[-1]),
    )
    packed = pack(fake, btc_returns)
    packed["note"] = "equal-weight of sleeve daily returns; fees and funding stay inside each sleeve"
    return packed


def ann_factor() -> float:
    return math.sqrt(365.0)
