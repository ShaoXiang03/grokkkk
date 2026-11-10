"""Decision functions. A list is the book to trade at the next open. None means hold.

Every read is at index ``i`` or earlier. The next open is not an input.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from desk.strategies.panel import Features
from desk.strategies.sizing import SHOCK_ATR
from desk.strategies.universe import momentum_score, strategy_cluster


@dataclass
class Intent:
    symbol: str
    side: int
    size_mode: str = "equal_risk"
    risk_fraction: float = 0.02
    risk_atr_mult: float = SHOCK_ATR
    exit_mode: str = "rebalance"  # rebalance | trail | event
    trail_mult: float | None = None
    channel_n: int | None = None
    hard_stop_price: float | None = None
    target_r: float | None = None
    time_stop_days: int | None = None
    shock_dir: int = 0
    shock_low: float | None = None
    shock_high: float | None = None
    max_retrace: float | None = None
    score: float = 0.0
    raw_weight: float = 1.0
    cluster: str = "alts"
    keep: bool = False
    reset_entry: bool = True
    sticky_trail: bool = False
    vol_target: float | None = None
    decision_atr: float | None = None


def _both_sides(scores: list[tuple[int, float]], fraction: float, min_side: int) -> tuple[list[int], list[int]] | None:
    finite = [(index, score) for index, score in scores if np.isfinite(score)]
    finite.sort(key=lambda item: (-item[1], item[0]))
    n = len(finite)
    take = int(np.floor(fraction * n))
    if take < min_side:
        return None
    longs = [index for index, _score in finite[:take]]
    shorts = [index for index, _score in finite[-take:]]
    if set(longs) & set(shorts):
        return None
    return longs, shorts


def _weight(features: Features, index: int, i: int, params: dict) -> float:
    features.ensure("vol30")
    vol = features.column("vol30")[index, i]
    if not np.isfinite(vol) or vol <= 0:
        return float("nan")
    return 1.0 / float(vol)


def decide_e1(features: Features, i: int, held: dict[str, int], params: dict) -> list[Intent] | None:
    """Sunday close, top and bottom fifth by trailing return. Both sides or flat."""
    if int(features.weekday[i]) != 6:
        return None
    lookback = int(params.get("lookback", 21))
    universe = features.universe(i, params)
    scores = []
    for index in universe:
        scores.append((index, momentum_score(features.close[index], i, lookback)))
    picked = _both_sides(scores, float(params.get("side_fraction", 0.20)), int(params.get("min_side", 3)))
    if picked is None:
        return []
    longs, shorts = picked
    mode = params.get("size_mode", "equal_risk")
    intents = []
    for index, side in [(index, 1) for index in longs] + [(index, -1) for index in shorts]:
        weight = 1.0
        if mode == "inv_vol":
            weight = _weight(features, index, i, params)
            if not np.isfinite(weight):
                continue
        atr = features.atr[index, i]
        intents.append(
            Intent(
                symbol=features.symbols[index],
                side=side,
                size_mode="inv_vol" if mode == "inv_vol" else "equal_risk",
                risk_fraction=0.02,
                risk_atr_mult=SHOCK_ATR,
                exit_mode="rebalance",
                trail_mult=float(params.get("catastrophe_atr", 5.0)),
                score=abs(momentum_score(features.close[index], i, lookback)),
                raw_weight=weight,
                cluster=strategy_cluster(features.symbols[index]),
                reset_entry=True,
                decision_atr=float(atr) if np.isfinite(atr) else None,
            )
        )
    # Re-check side counts after inv-vol drops.
    n_long = sum(1 for item in intents if item.side > 0)
    n_short = sum(1 for item in intents if item.side < 0)
    if n_long < int(params.get("min_side", 3)) or n_short < int(params.get("min_side", 3)):
        return []
    return intents


def _residual(features: Features, index: int, i: int, params: dict) -> float:
    if features.btc is None or index == features.btc:
        return float("nan")
    beta_n = int(params.get("beta_days", 60))
    resid_n = int(params.get("resid_days", 15))
    if i < beta_n or resid_n > beta_n:
        return float("nan")
    asset = features.ret[index, i - beta_n + 1 : i + 1]
    market = features.ret[features.btc, i - beta_n + 1 : i + 1]
    if asset.size < beta_n or not np.isfinite(asset).all() or not np.isfinite(market).all():
        return float("nan")
    var_m = float(np.var(market, ddof=1))
    if var_m <= 0:
        return float("nan")
    cov = float(np.cov(asset, market, ddof=1)[0, 1])
    beta = cov / var_m
    resid = asset[-resid_n:] - beta * market[-resid_n:]
    features.ensure("vol30")
    vol = features.column("vol30")[index, i]
    if not np.isfinite(vol) or vol <= 0:
        return float("nan")
    return float(np.sum(resid) / vol)


def decide_e2(features: Features, i: int, held: dict[str, int], params: dict) -> list[Intent] | None:
    if int(features.weekday[i]) != 6:
        return None
    universe = [index for index in features.universe(i, params) if index != features.btc]
    scores = [(index, _residual(features, index, i, params)) for index in universe]
    picked = _both_sides(scores, float(params.get("side_fraction", 0.20)), int(params.get("min_side", 3)))
    if picked is None:
        return []
    longs, shorts = picked
    intents = []
    for index, side in [(index, 1) for index in longs] + [(index, -1) for index in shorts]:
        weight = _weight(features, index, i, params)
        if not np.isfinite(weight):
            continue
        atr = features.atr[index, i]
        intents.append(
            Intent(
                symbol=features.symbols[index],
                side=side,
                size_mode="inv_vol",
                risk_fraction=0.02,
                risk_atr_mult=SHOCK_ATR,
                exit_mode="rebalance",
                trail_mult=float(params.get("catastrophe_atr", 5.0)),
                score=abs(_residual(features, index, i, params)),
                raw_weight=weight,
                cluster=strategy_cluster(features.symbols[index]),
                reset_entry=True,
                decision_atr=float(atr) if np.isfinite(atr) else None,
            )
        )
    n_long = sum(1 for item in intents if item.side > 0)
    n_short = sum(1 for item in intents if item.side < 0)
    if n_long < int(params.get("min_side", 3)) or n_short < int(params.get("min_side", 3)):
        return []
    return intents


def _majors(features: Features, i: int, params: dict) -> list[int]:
    ranked = features.universe(i, {**params, "top_n": max(int(params.get("top_n", 40)), 30)})
    wanted = []
    for name in ("BTCUSDT", "ETHUSDT"):
        index = features.sym.get(name)
        if index is None:
            continue
        # Force majors in when they are actually tradable that day, even if the
        # shared top-N slice was built with a smaller cap. They still need a bar.
        if np.isfinite(features.close[index, i]) and features.close[index, i] > 0:
            history = int(np.isfinite(features.close[index, : i + 1]).sum())
            if history >= int(params.get("min_history", 60)):
                wanted.append(index)
    extra = int(params.get("majors_extra", 8))
    for index in ranked:
        if index in wanted:
            continue
        if len(wanted) >= 2 + extra:
            break
        wanted.append(index)
    return wanted[: 2 + extra]


def _sma_side(features: Features, index: int, i: int, sma_n: int, is_btc: bool) -> int:
    key = "sma50" if sma_n == 50 else "sma200"
    if sma_n not in (50, 200):
        features._extra[f"sma{sma_n}"] = None  # pragma: no cover - catalog only uses 50/200
    features.ensure(key if sma_n in (50, 200) else "sma50")
    column = features.column(key if sma_n in (50, 200) else "sma50")
    sma = column[index, i]
    price = features.close[index, i]
    if not np.isfinite(sma) or not np.isfinite(price) or sma <= 0:
        return 0
    if price > sma:
        return 1
    if price < sma and not is_btc:
        return -1
    return 0


def decide_e3a(features: Features, i: int, held: dict[str, int], params: dict) -> list[Intent] | None:
    """50-day state. BTC is long or flat. Alts are long or short. Trail exits stick until the state clears."""
    features.ensure("sma50")
    if params.get("majors_only"):
        universe = _majors(features, i, params)
    else:
        universe = features.universe(i, params)
    sma_n = int(params.get("sma_n", 50))
    trail = float(params.get("trail_mult", 3.0))
    intents: list[Intent] = []
    for index in universe:
        symbol = features.symbols[index]
        side = _sma_side(features, index, i, sma_n, symbol == "BTCUSDT")
        if side == 0:
            continue
        atr = features.atr[index, i]
        price = features.close[index, i]
        if not np.isfinite(atr) or atr <= 0 or not np.isfinite(price) or price <= 0:
            continue
        sma = features.column("sma50")[index, i]
        score = abs(price - sma) / atr if np.isfinite(sma) else 0.0
        already = held.get(symbol) == side
        intents.append(
            Intent(
                symbol=symbol,
                side=side,
                size_mode="greedy",
                risk_fraction=float(params.get("risk_fraction", 0.02)),
                risk_atr_mult=trail,
                exit_mode="trail",
                trail_mult=trail,
                score=float(score),
                cluster=strategy_cluster(symbol),
                keep=already,
                reset_entry=not already,
                sticky_trail=True,
                decision_atr=float(atr),
            )
        )
    return intents


def _fresh_break(close_now: float, close_prev: float, level_now: float, level_prev: float, side: int) -> bool:
    if not all(np.isfinite(value) for value in (close_now, close_prev, level_now, level_prev)):
        return False
    if side > 0:
        return close_now > level_now and close_prev <= level_prev
    return close_now < level_now and close_prev >= level_prev


def _vol_too_hot(features: Features, index: int, i: int, params: dict) -> bool:
    features.ensure("vol20")
    vol = features.column("vol20")
    history_n = int(params.get("vol_history", 252))
    if i + 1 < history_n:
        return True
    window = vol[index, i - history_n + 1 : i + 1]
    finite = window[np.isfinite(window)]
    if finite.size < 60 or not np.isfinite(vol[index, i]):
        return True
    threshold = float(np.quantile(finite, float(params.get("vol_decile", 0.90))))
    return float(vol[index, i]) >= threshold


def decide_e3b(features: Features, i: int, held: dict[str, int], params: dict) -> list[Intent] | None:
    """20-day Donchian cross, 200-day filter, 10-day channel or 3× ATR trail, skip the hottest vol decile."""
    features.ensure("sma200")
    features.ensure("high20")
    features.ensure("low20")
    features.ensure("high10")
    features.ensure("low10")
    entry_high = features.column("high20")
    entry_low = features.column("low20")
    trail = float(params.get("trail_mult", 3.0))
    exit_n = int(params.get("exit_n", 10))
    intents: list[Intent] = []
    if i < 1:
        return intents
    for index in features.universe(i, params):
        symbol = features.symbols[index]
        price = features.close[index, i]
        prev = features.close[index, i - 1]
        sma = features.column("sma200")[index, i]
        atr = features.atr[index, i]
        if not np.isfinite(price) or not np.isfinite(atr) or atr <= 0 or not np.isfinite(sma):
            continue
        held_side = held.get(symbol, 0)
        long_break = _fresh_break(price, prev, entry_high[index, i], entry_high[index, i - 1], 1)
        short_break = _fresh_break(price, prev, entry_low[index, i], entry_low[index, i - 1], -1)
        hot = _vol_too_hot(features, index, i, params)
        want = 0
        if held_side == 1:
            want = 1
        elif held_side == -1:
            want = -1
        elif long_break and price > sma and not hot:
            want = 1
        elif short_break and price < sma and not hot:
            want = -1
        if want == 0:
            continue
        already = held_side == want
        score = abs(price / sma - 1.0) / (atr / price)
        intents.append(
            Intent(
                symbol=symbol,
                side=want,
                size_mode="greedy",
                risk_fraction=float(params.get("risk_fraction", 0.02)),
                risk_atr_mult=trail,
                exit_mode="trail",
                trail_mult=trail,
                channel_n=exit_n,
                score=float(score),
                cluster=strategy_cluster(symbol),
                keep=already,
                reset_entry=not already,
                sticky_trail=False,
                decision_atr=float(atr),
            )
        )
    return intents


def _trailing_mean(row: np.ndarray, i: int, days: int, min_obs: int) -> float:
    if i + 1 < days:
        return float("nan")
    window = row[i - days + 1 : i + 1]
    finite = window[np.isfinite(window)]
    if finite.size < min_obs:
        return float("nan")
    return float(np.mean(finite))


def decide_e4(features: Features, i: int, held: dict[str, int], params: dict) -> list[Intent] | None:
    """Long the cheapest trailing funding or premium, short the richest. Weekly."""
    if int(features.weekday[i]) != 6:
        return None
    days = int(params.get("funding_days", 7))
    signal = params.get("signal", "funding")
    source = features.funding_ann if signal == "funding" else features.premium
    universe = features.universe(i, params)
    scored: list[tuple[int, float]] = []
    z_filter = bool(params.get("z_filter", False))
    z_values = None
    if z_filter:
        features.ensure("funding_z")
        z_values = features.column("funding_z")
    for index in universe:
        value = _trailing_mean(source[index], i, days, max(3, days // 2))
        if not np.isfinite(value):
            continue
        if z_filter:
            z = z_values[index, i]
            if not np.isfinite(z) or abs(float(z)) <= float(params.get("z_min", 1.0)):
                continue
        scored.append((index, value))
    # Low funding is the long. _both_sides longs the high score, so rank on the negative.
    ranked = [(index, -value) for index, value in scored]
    picked = _both_sides(ranked, float(params.get("side_fraction", 0.20)), int(params.get("min_side", 3)))
    if picked is None:
        return []
    longs, shorts = picked
    intents = []
    for index, side in [(index, 1) for index in longs] + [(index, -1) for index in shorts]:
        atr = features.atr[index, i]
        intents.append(
            Intent(
                symbol=features.symbols[index],
                side=side,
                size_mode="equal_risk",
                risk_fraction=0.02,
                risk_atr_mult=SHOCK_ATR,
                exit_mode="rebalance",
                trail_mult=float(params.get("catastrophe_atr", 5.0)),
                score=abs(dict(scored)[index]),
                cluster=strategy_cluster(features.symbols[index]),
                reset_entry=True,
                decision_atr=float(atr) if np.isfinite(atr) else None,
            )
        )
    n_long = sum(1 for item in intents if item.side > 0)
    n_short = sum(1 for item in intents if item.side < 0)
    if n_long < int(params.get("min_side", 3)) or n_short < int(params.get("min_side", 3)):
        return []
    return intents


def _trend_score(features: Features, index: int, i: int) -> float:
    features.ensure("ema")
    atr = features.atr[index, i]
    if not np.isfinite(atr) or atr <= 0:
        return float("nan")
    gaps = []
    pairs = ((10, 30), (20, 50), (50, 200))
    for fast, slow in pairs:
        a = features.column(f"ema{fast}")[index, i]
        b = features.column(f"ema{slow}")[index, i]
        if not np.isfinite(a) or not np.isfinite(b):
            return float("nan")
        gaps.append((a - b) / atr)
    # Standardise each gap versus its own trailing year. The z-score columns are
    # built once per pair and cached under gap names.
    z_parts = []
    for (fast, slow), gap in zip(pairs, gaps, strict=False):
        key = f"gap{fast}_{slow}"
        if key not in features._extra:
            fast_col = features.column(f"ema{fast}")
            slow_col = features.column(f"ema{slow}")
            with np.errstate(divide="ignore", invalid="ignore"):
                series = (fast_col - slow_col) / features.atr
            from desk.strategies.panel import _rolling_z

            features._extra[key] = _rolling_z(series, 365, 60)
        z = features._extra[key][index, i]
        if not np.isfinite(z):
            return float("nan")
        z_parts.append(float(z))
    return float(np.mean(z_parts))


def decide_e5(features: Features, i: int, held: dict[str, int], params: dict) -> list[Intent] | None:
    """Daily long/short. Top and bottom scores, a side with fewer than 3 names stays flat."""
    features.ensure("funding_z")
    if params.get("use_oi", True):
        features.ensure("oi_z")
    universe = features.universe(i, params)
    trail = float(params.get("trail_mult", 2.5))
    scored: list[tuple[int, float]] = []
    for index in universe:
        trend = _trend_score(features, index, i)
        fund = features.column("funding_z")[index, i]
        if not np.isfinite(trend) or not np.isfinite(fund):
            continue
        fund = float(np.clip(fund, -3.0, 3.0))
        if params.get("use_oi", True):
            oi_z = features.column("oi_z")[index, i]
            if np.isfinite(oi_z):
                mult = 1.0 + 0.5 * float(np.clip(oi_z, -1.0, 1.0))
            else:
                mult = 0.5
        else:
            mult = 1.0
        score = (trend + fund) / 2.0 * mult
        if np.isfinite(score):
            scored.append((index, float(score)))
    if not scored:
        return []
    positive = sorted([(index, score) for index, score in scored if score > 0], key=lambda item: (-item[1], item[0]))
    negative = sorted([(index, score) for index, score in scored if score < 0], key=lambda item: (item[1], item[0]))
    top_k = int(params.get("top_k", 5))
    min_side = int(params.get("min_side", 3))
    longs = positive[:top_k] if len(positive) >= min_side else []
    shorts = negative[:top_k] if len(negative) >= min_side else []
    intents = []
    for index, score in [(index, score) for index, score in longs] + [(index, score) for index, score in shorts]:
        side = 1 if score > 0 else -1
        atr = features.atr[index, i]
        if not np.isfinite(atr) or atr <= 0:
            continue
        symbol = features.symbols[index]
        already = held.get(symbol) == side
        intents.append(
            Intent(
                symbol=symbol,
                side=side,
                size_mode="vol_target",
                risk_fraction=float(params.get("risk_fraction", 0.02)),
                risk_atr_mult=trail,
                exit_mode="trail",
                trail_mult=trail,
                score=abs(score),
                raw_weight=1.0,
                cluster=strategy_cluster(symbol),
                keep=False,
                reset_entry=not already,
                sticky_trail=False,
                vol_target=float(params.get("vol_target", 0.20)),
                decision_atr=float(atr),
            )
        )
    return intents


def _event_universe(features: Features, i: int, params: dict) -> list[int]:
    universe = list(features.universe(i, params))
    for name in params.get("always") or ():
        index = features.sym.get(name)
        if index is None or index in universe:
            continue
        if np.isfinite(features.close[index, i]) and features.close[index, i] > 0:
            history = int(np.isfinite(features.close[index, : i + 1]).sum())
            if history >= int(params.get("min_history", 60)):
                universe.append(index)
    return universe


def decide_e6a(features: Features, i: int, held: dict[str, int], params: dict) -> list[Intent] | None:
    """OI-flush fade. New events only; open fades are kept until the engine stops them."""
    intents: list[Intent] = []
    for symbol, side in held.items():
        index = features.sym.get(symbol)
        if index is None:
            continue
        atr = features.atr[index, i]
        intents.append(
            Intent(
                symbol=symbol,
                side=side,
                size_mode="equal_risk",
                risk_fraction=float(params.get("risk_fraction", 0.01)),
                exit_mode="event",
                keep=True,
                reset_entry=False,
                cluster=strategy_cluster(symbol),
                decision_atr=float(atr) if np.isfinite(atr) else None,
            )
        )
    if i < 20:
        return intents
    for index in _event_universe(features, i, params):
        symbol = features.symbols[index]
        if symbol in held:
            continue
        price = features.close[index, i]
        prev = features.close[index, i - 1]
        atr = features.atr[index, i]
        if not all(np.isfinite(value) for value in (price, prev, atr)) or atr <= 0 or prev == 0:
            continue
        move = abs(price - prev)
        span = features.high[index, i] - features.low[index, i]
        shock = max(move, span)
        if shock < float(params.get("shock_atr", 3.0)) * atr:
            continue
        oi_now = features.oi[index, i]
        oi_prev = features.oi[index, i - 1]
        if not np.isfinite(oi_now) or not np.isfinite(oi_prev) or oi_prev <= 0:
            continue
        if (oi_now - oi_prev) / oi_prev > -float(params.get("oi_drop", 0.05)):
            continue
        history = features.quote_volume[index, i - 20 : i]
        base = history[np.isfinite(history)]
        volume = features.quote_volume[index, i]
        if base.size < 10 or not np.isfinite(volume) or volume <= float(np.mean(base)):
            continue
        if price == prev:
            continue
        side = 1 if price < prev else -1
        shock_dir = -1 if price < prev else 1
        pad = float(params.get("stop_pad_atr", 0.25)) * atr
        if side > 0:
            stop = float(features.low[index, i] - pad)
        else:
            stop = float(features.high[index, i] + pad)
        intents.append(
            Intent(
                symbol=symbol,
                side=side,
                size_mode="greedy",
                risk_fraction=float(params.get("risk_fraction", 0.01)),
                exit_mode="event",
                hard_stop_price=stop,
                target_r=float(params.get("target_r", 1.5)),
                time_stop_days=int(params.get("time_stop_days", 3)),
                shock_dir=shock_dir,
                shock_low=float(features.low[index, i]),
                shock_high=float(features.high[index, i]),
                max_retrace=float(params.get("retrace", 0.5)),
                score=shock / atr,
                cluster=strategy_cluster(symbol),
                keep=False,
                reset_entry=True,
                decision_atr=float(atr),
            )
        )
    return intents


def decide_e6b(features: Features, i: int, held: dict[str, int], params: dict) -> list[Intent] | None:
    """Crowded squeeze: price reclaims the 20-day edge, OI is heavy, funding is on the other side."""
    features.ensure("high20")
    features.ensure("low20")
    features.ensure("funding_z")
    features.ensure("oi_z")
    intents: list[Intent] = []
    for symbol, side in held.items():
        index = features.sym.get(symbol)
        atr = features.atr[index, i] if index is not None else float("nan")
        intents.append(
            Intent(
                symbol=symbol,
                side=side,
                size_mode="equal_risk",
                risk_fraction=float(params.get("risk_fraction", 0.02)),
                exit_mode="event",
                keep=True,
                reset_entry=False,
                cluster=strategy_cluster(symbol),
                decision_atr=float(atr) if np.isfinite(atr) else None,
                trail_mult=float(params.get("trail_mult", 2.5)),
            )
        )
    range_n = int(params.get("range_n", 20))
    if i < max(range_n, 30):
        return intents
    near = float(params.get("near_pct", 0.05))
    for index in features.universe(i, params):
        symbol = features.symbols[index]
        if symbol in held:
            continue
        price = features.close[index, i]
        high = features.column("high20")[index, i]
        low = features.column("low20")[index, i]
        atr = features.atr[index, i]
        oi_z = features.column("oi_z")[index, i]
        fund_z = features.column("funding_z")[index, i]
        if not all(np.isfinite(value) for value in (price, high, low, atr, oi_z, fund_z)) or atr <= 0:
            continue
        if not np.isfinite(features.close[index, i - 30]):
            continue
        if float(oi_z) < float(params.get("oi_z", 1.5)):
            continue
        side = 0
        stop = None
        if price > high and price <= high * (1.0 + near) and float(fund_z) <= -float(params.get("funding_z", 1.5)):
            side = 1
            stop = float(high - float(params.get("stop_atr", 2.0)) * atr)
        elif price < low and price >= low * (1.0 - near) and float(fund_z) >= float(params.get("funding_z", 1.5)):
            side = -1
            stop = float(low + float(params.get("stop_atr", 2.0)) * atr)
        if side == 0 or stop is None:
            continue
        intents.append(
            Intent(
                symbol=symbol,
                side=side,
                size_mode="greedy",
                risk_fraction=float(params.get("risk_fraction", 0.02)),
                risk_atr_mult=float(params.get("stop_atr", 2.0)),
                exit_mode="event",
                trail_mult=float(params.get("trail_mult", 2.5)),
                hard_stop_price=stop,
                target_r=float(params.get("target_r", 3.0)),
                time_stop_days=int(params.get("time_stop_days", 15)),
                score=abs(float(oi_z)) + abs(float(fund_z)),
                cluster=strategy_cluster(symbol),
                keep=False,
                reset_entry=True,
                decision_atr=float(atr),
            )
        )
    return intents


def decide_hedge(features: Features, i: int, held: dict[str, int], params: dict) -> list[Intent] | None:
    """50/50 control: long BTC, short an equal-weight basket of the next liquid names.

    Notional is half of equity on each leg. The 6% risk cap is not applied.
    This is a baseline, not a candidate book.
    """
    if int(features.weekday[i]) != 6:
        return None
    if features.btc is None:
        return []
    universe = [index for index in features.universe(i, params) if index != features.btc]
    if len(universe) < 5:
        return []
    shorts = universe[:10]
    leg = 0.5 / len(shorts)
    intents = [
        Intent(
            symbol="BTCUSDT",
            side=1,
            size_mode="fixed",
            raw_weight=0.5,
            exit_mode="rebalance",
            trail_mult=None,
            cluster="btc",
            reset_entry=True,
            risk_atr_mult=1.0,
        )
    ]
    for index in shorts:
        symbol = features.symbols[index]
        intents.append(
            Intent(
                symbol=symbol,
                side=-1,
                size_mode="fixed",
                raw_weight=leg,
                exit_mode="rebalance",
                trail_mult=None,
                cluster=strategy_cluster(symbol),
                reset_entry=True,
                risk_atr_mult=1.0,
            )
        )
    return intents


DECISIONS = {
    "e1": decide_e1,
    "e2": decide_e2,
    "e3a": decide_e3a,
    "e3b": decide_e3b,
    "e4": decide_e4,
    "e5": decide_e5,
    "e6a": decide_e6a,
    "e6b": decide_e6b,
    "hedge": decide_hedge,
}
