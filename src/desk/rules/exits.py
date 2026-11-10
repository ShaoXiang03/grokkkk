"""Exit rules shared by live scanning and the backtester.

The hard stop never moves against the trade. A partial take-profit moves the
stop to breakeven when that tightens it. The 4H trail may only tighten.
"""

from __future__ import annotations

from desk.rules.regime import SETUP_MEME, SETUP_PULLBACK, SETUP_RECLAIM, SETUP_SQUEEZE


def tighten_stop(current: float, proposed: float, direction: str) -> float:
    if direction == "long":
        return max(current, proposed)
    if direction == "short":
        return min(current, proposed)
    raise ValueError(direction)


def trail_stop(
    current: float,
    direction: str,
    ema_20: float,
    atr: float,
    close: float,
    ema_mult: float,
    close_mult: float,
) -> float:
    if direction == "long":
        proposed = max(current, ema_20 - ema_mult * atr, close - close_mult * atr)
    else:
        proposed = min(current, ema_20 + ema_mult * atr, close + close_mult * atr)
    return tighten_stop(current, proposed, direction)


def chandelier_stop(current: float, direction: str, extreme: float, atr: float, atr_mult: float) -> float:
    """Ratchet a stop off the best price since entry. The extreme is known only through the closed bar."""
    if atr_mult <= 0 or atr <= 0:
        return current
    if direction == "long":
        proposed = extreme - atr_mult * atr
    else:
        proposed = extreme + atr_mult * atr
    return tighten_stop(current, proposed, direction)


def structure_stop(
    current: float,
    direction: str,
    recent_highs: list[float],
    recent_lows: list[float],
    atr: float,
    buffer_mult: float,
) -> float:
    """Trail behind the recent closed-bar extreme. `recent_*` must end at the bar being closed."""
    if not recent_highs or not recent_lows or atr <= 0:
        return current
    buffer = buffer_mult * atr
    if direction == "long":
        proposed = min(recent_lows) - buffer
    else:
        proposed = max(recent_highs) + buffer
    return tighten_stop(current, proposed, direction)


def trail_is_armed(arm: str, *, partial_done: bool, reached_1r: bool) -> bool:
    if arm == "immediate":
        return True
    if arm == "after_partial":
        return partial_done
    if arm == "after_1r":
        return reached_1r
    raise ValueError(f"unknown trail_arm {arm}")


def closed_structure_window(highs: list[float], lows: list[float], index: int, lookback: int) -> tuple[list[float], list[float]]:
    """Highs and lows of the closed window ending at `index`. Later rows are not read."""
    if index < 0 or index >= len(highs) or index >= len(lows):
        return [], []
    left = max(0, index + 1 - max(1, lookback))
    return list(highs[left : index + 1]), list(lows[left : index + 1])


def breakeven_stop(current: float, entry: float, direction: str) -> float:
    return tighten_stop(current, entry, direction)


def stop_hit(direction: str, stop: float, high: float, low: float) -> bool:
    if direction == "long":
        return low <= stop
    return high >= stop


def target_hit(direction: str, target: float, high: float, low: float) -> bool:
    if direction == "long":
        return high >= target
    return low <= target


def stop_fill_price(direction: str, stop: float, open_: float) -> float:
    """Gap through the stop fills at the open; otherwise at the stop."""
    if direction == "long":
        return open_ if open_ < stop else stop
    return open_ if open_ > stop else stop


def one_r_price(entry: float, initial_stop: float, direction: str) -> float:
    distance = abs(entry - initial_stop)
    if direction == "long":
        return entry + distance
    return entry - distance


def reached_one_r(direction: str, entry: float, initial_stop: float, high: float, low: float) -> bool:
    level = one_r_price(entry, initial_stop, direction)
    if direction == "long":
        return high >= level
    return low <= level


def invalidated(setup: str, direction: str, bars_held: int, row, meta: dict) -> bool:
    close = float(row["close"])
    if setup == SETUP_SQUEEZE:
        upper = float(row["bb_upper"])
        lower = float(row["bb_lower"])
        mid = float(row["bb_mid"])
        inside = lower <= close <= upper
        if bars_held <= 2 and inside:
            return True
        if bars_held <= 3:
            if direction == "long" and close < mid:
                return True
            if direction == "short" and close > mid:
                return True
        return False
    if setup == SETUP_PULLBACK:
        ema_50 = float(row["ema_50"])
        if direction == "long" and close < ema_50:
            return True
        if direction == "short" and close > ema_50:
            return True
        return False
    if setup == SETUP_RECLAIM:
        level = float(meta["level"])
        if direction == "long" and close < level:
            return True
        if direction == "short" and close > level:
            return True
        return False
    if setup == SETUP_MEME:
        if direction == "long" and close < float(meta["signal_low"]):
            return True
        if direction == "short" and close > float(meta["signal_high"]):
            return True
        return False
    return False


def daily_bias_invalidated(setup: str, direction: str, daily_row, ema_col: str = "ema_200") -> bool:
    if setup != SETUP_PULLBACK or daily_row is None or ema_col not in daily_row.index:
        return False
    close = float(daily_row["close"])
    ema_slow = float(daily_row[ema_col])
    if direction == "long":
        return close < ema_slow
    return close > ema_slow


def time_stop_due(setup: str, bars_held: int, time_stop_bars: int, reached_1r: bool, hard_max_bars: int | None) -> str | None:
    if hard_max_bars is not None and setup == SETUP_MEME and bars_held >= hard_max_bars:
        return "max_hold"
    if bars_held >= time_stop_bars and not reached_1r:
        return "time_stop"
    return None


def invalidation_text(setup: str, direction: str) -> str:
    if setup == SETUP_SQUEEZE:
        side = "below_middle" if direction == "long" else "above_middle"
        return f"4h_close_inside_bands_within_2 OR 4h_close_{side}_within_3"
    if setup == SETUP_PULLBACK:
        if direction == "long":
            return "4h_close_below_ema50 OR daily_close_below_ema200"
        return "4h_close_above_ema50 OR daily_close_above_ema200"
    if setup == SETUP_RECLAIM:
        if direction == "long":
            return "4h_close_below_reclaimed_support"
        return "4h_close_above_reclaimed_resistance"
    if direction == "long":
        return "4h_close_below_breakout_low OR hard_max_16_4h"
    return "4h_close_above_breakdown_high OR hard_max_16_4h"
