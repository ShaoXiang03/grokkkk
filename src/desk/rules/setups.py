"""Setups A–D, long and short. Functions read only the rows they are given.

A caller that wants no lookahead passes a frame whose last row is the closed
bar under test. Indicator columns are produced by desk.indicators.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from desk.indicators.core import ema_column
from desk.rules.exits import invalidation_text
from desk.rules.regime import (
    SETUP_MEME,
    SETUP_PULLBACK,
    SETUP_RECLAIM,
    SETUP_SQUEEZE,
    daily_bias_ok,
    direction_allowed,
)
from desk.timeutils import H4_MS, HOUR_MS


@dataclass
class SetupCandidate:
    symbol: str
    setup: str
    direction: str
    setup_open_time: int
    setup_close_time: int
    level: float
    provisional_stop: float | None
    atr: float
    trigger_bars: int
    time_stop_bars: int
    hard_max_bars: int | None
    invalidation: str
    meta: dict = field(default_factory=dict)


def _finite(value) -> bool:
    try:
        return value is not None and np.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _volume_ok(row, mult: float) -> bool:
    if not _finite(row["volume"]) or not _finite(row["vol_avg_20"]) or float(row["vol_avg_20"]) <= 0:
        return False
    return float(row["volume"]) >= mult * float(row["vol_avg_20"])


def squeeze_at(df: pd.DataFrame, i: int, direction: str, settings: dict) -> dict | None:
    cfg = settings["setups"][SETUP_SQUEEZE]
    lookback = int(cfg["bandwidth_lookback"])
    # Bandwidth is measured on the bar before the breakout, against the 20 bars before that.
    if i < lookback + lookback:
        return None
    measure = i - 1
    bw = df["bandwidth"].to_numpy(dtype=float)
    window = bw[measure - lookback : measure]
    if not _finite(bw[measure]) or not np.isfinite(window).all():
        return None
    if bw[measure] > float(np.min(window)):
        return None
    row = df.iloc[i]
    if direction == "long":
        if not (_finite(row["bb_upper"]) and float(row["close"]) > float(row["bb_upper"])):
            return None
        level = float(row["bb_upper"])
    else:
        if not (_finite(row["bb_lower"]) and float(row["close"]) < float(row["bb_lower"])):
            return None
        level = float(row["bb_lower"])
    if not _volume_ok(row, float(cfg["volume_mult"])):
        return None
    if not _finite(row["atr_14"]):
        return None
    atr = float(row["atr_14"])
    squeeze = df.iloc[i - lookback : i]
    buffer = float(cfg["stop_atr_buffer"]) * atr
    if direction == "long":
        anchor = min(float(squeeze["low"].min()), float(row["low"]))
        stop = anchor - buffer
    else:
        anchor = max(float(squeeze["high"].max()), float(row["high"]))
        stop = anchor + buffer
    return {
        "level": level,
        "provisional_stop": stop,
        "atr": atr,
        "meta": {"squeeze_anchor": anchor, "signal_low": float(row["low"]), "signal_high": float(row["high"])},
    }


def pullback_at(df: pd.DataFrame, i: int, direction: str, settings: dict) -> dict | None:
    cfg = settings["setups"][SETUP_PULLBACK]
    row = df.iloc[i]
    col_fast, col_mid, col_slow = ema_column(settings, "fast"), ema_column(settings, "mid"), ema_column(settings, "slow")
    needed = (col_fast, col_mid, col_slow, "rsi_14", "atr_14", "low", "high")
    if any(name not in row.index or not _finite(row[name]) for name in needed):
        return None
    ema20, ema50, ema200 = float(row[col_fast]), float(row[col_mid]), float(row[col_slow])
    rsi = float(row["rsi_14"])
    low, high = float(row["low"]), float(row["high"])
    if direction == "long":
        if not (ema20 > ema50 > ema200):
            return None
        lo, hi = cfg["rsi_long"]
        if not (float(lo) <= rsi <= float(hi)):
            return None
        tagged = (low <= ema20 <= high) or (low <= ema50 <= high)
        if not tagged:
            return None
        stop = low - float(cfg["stop_atr_buffer"]) * float(row["atr_14"])
        swing = low
    else:
        if not (ema20 < ema50 < ema200):
            return None
        lo, hi = cfg["rsi_short"]
        if not (float(lo) <= rsi <= float(hi)):
            return None
        tagged = (low <= ema20 <= high) or (low <= ema50 <= high)
        if not tagged:
            return None
        stop = high + float(cfg["stop_atr_buffer"]) * float(row["atr_14"])
        swing = high
    return {
        "level": ema20,
        "provisional_stop": stop,
        "atr": float(row["atr_14"]),
        "meta": {"swing": swing, "signal_low": low, "signal_high": high},
    }


def reclaim_at(df: pd.DataFrame, i: int, direction: str, settings: dict) -> dict | None:
    """Range is the `range_bars` bars immediately before a fakeout.

    The fakeout must sit inside `fakeout_lookback` bars before the reclaim, and
    this bar must be the first later close back through the level.
    """
    cfg = settings["setups"][SETUP_RECLAIM]
    range_bars = int(cfg["range_bars"])
    lookback = int(cfg["fakeout_lookback"])
    if range_bars < int(cfg["min_range_bars"]) or i < range_bars + 1:
        return None
    start_k = max(range_bars, i - lookback)
    for k in range(i - 1, start_k - 1, -1):
        window = df.iloc[k - range_bars : k]
        if len(window) < range_bars:
            continue
        atr_row = df.iloc[k - 1]
        if not _finite(atr_row["atr_14"]):
            continue
        atr = float(atr_row["atr_14"])
        range_high = float(window["high"].max())
        range_low = float(window["low"].min())
        if range_high - range_low < float(cfg["range_atr_mult"]) * atr:
            continue
        fake = df.iloc[k]
        if direction == "long":
            broke = float(fake["low"]) < range_low or float(fake["close"]) < range_low
            level = range_low
            reclaimed = float(df.iloc[i]["close"]) > level
            earlier = any(float(df.iloc[m]["close"]) > level for m in range(k + 1, i))
        else:
            broke = float(fake["high"]) > range_high or float(fake["close"]) > range_high
            level = range_high
            reclaimed = float(df.iloc[i]["close"]) < level
            earlier = any(float(df.iloc[m]["close"]) < level for m in range(k + 1, i))
        if not broke or earlier or not reclaimed:
            continue
        row = df.iloc[i]
        if not _finite(row["atr_14"]):
            continue
        return {
            "level": level,
            "provisional_stop": None,
            "atr": float(row["atr_14"]),
            "meta": {
                "range_high": range_high,
                "range_low": range_low,
                "fakeout_low": float(fake["low"]),
                "fakeout_high": float(fake["high"]),
                "fakeout_open_time": int(fake["open_time"]),
                "reclaim_open_time": int(row["open_time"]),
                "reclaim_volume": float(row["volume"]),
                "reclaim_vol_avg": float(row["vol_avg_20"]) if _finite(row["vol_avg_20"]) else None,
                "volume_mult": float(cfg["volume_mult"]),
                "signal_low": float(fake["low"]),
                "signal_high": float(fake["high"]),
                "level": level,
            },
        }
    return None


def meme_stop(direction: str, entry: float, signal_extreme: float, atr: float, buffer: float, cap_mult: float) -> float | None:
    """Tighter of the two structural stops, and it must sit beyond the signal bar."""
    if direction == "long":
        structural = signal_extreme - buffer * atr
        capped = entry - cap_mult * atr
        beyond = [price for price in (structural, capped) if price < signal_extreme]
        if not beyond:
            return None
        return max(beyond)
    structural = signal_extreme + buffer * atr
    capped = entry + cap_mult * atr
    beyond = [price for price in (structural, capped) if price > signal_extreme]
    if not beyond:
        return None
    return min(beyond)


def meme_at(df: pd.DataFrame, i: int, direction: str, settings: dict) -> dict | None:
    cfg = settings["setups"][SETUP_MEME]
    lookback = int(cfg["breakout_lookback"])
    if i < lookback:
        return None
    prior = df.iloc[i - lookback : i]
    row = df.iloc[i]
    if not _finite(row["atr_14"]) or not _volume_ok(row, float(cfg["volume_mult"])):
        return None
    prior_high = float(prior["high"].max())
    prior_low = float(prior["low"].min())
    if direction == "long":
        if float(row["close"]) <= prior_high:
            return None
        level = prior_high
    else:
        if float(row["close"]) >= prior_low:
            return None
        level = prior_low
    return {
        "level": level,
        "provisional_stop": None,
        "atr": float(row["atr_14"]),
        "meta": {
            "signal_low": float(row["low"]),
            "signal_high": float(row["high"]),
            "prior_high": prior_high,
            "prior_low": prior_low,
        },
    }


_DETECTORS = {
    SETUP_SQUEEZE: squeeze_at,
    SETUP_PULLBACK: pullback_at,
    SETUP_RECLAIM: reclaim_at,
    SETUP_MEME: meme_at,
}


def detect_setups_on_bar(
    symbol: str,
    df4h: pd.DataFrame,
    j: int,
    df1d: pd.DataFrame | None,
    regime: str | None,
    settings: dict,
) -> list[SetupCandidate]:
    """Chart conditions plus regime and daily-bias eligibility for one closed 4H bar."""
    found: list[SetupCandidate] = []
    daily_bars = 0 if df1d is None else len(df1d)
    row = df4h.iloc[j]
    for setup, detector in _DETECTORS.items():
        cfg = settings["setups"][setup]
        if not cfg.get("enabled", True):
            continue
        if setup in (SETUP_SQUEEZE, SETUP_PULLBACK) and daily_bars < int(cfg["min_daily_bars"]):
            continue
        for direction in ("long", "short"):
            if not direction_allowed(regime, setup, direction):
                continue
            if setup == SETUP_MEME:
                bias_required = daily_bars >= int(cfg["min_daily_bars_for_bias"])
                bias_bars = int(cfg["min_daily_bars_for_bias"])
            elif setup == SETUP_RECLAIM:
                bias_required = False
                bias_bars = int(settings["indicators"]["sma_slow"])
            else:
                bias_required = True
                bias_bars = int(cfg["min_daily_bars"])
            bias = daily_bias_ok(
                df1d,
                direction,
                required=bias_required,
                min_bars=bias_bars,
                ema_column=ema_column(settings, "slow"),
            )
            if bias is False:
                continue
            hit = detector(df4h, j, direction, settings)
            if hit is None:
                continue
            hit["meta"]["level"] = float(hit["level"])
            hard_max = int(cfg["hard_max_bars"]) if setup == SETUP_MEME else None
            found.append(
                SetupCandidate(
                    symbol=symbol,
                    setup=setup,
                    direction=direction,
                    setup_open_time=int(row["open_time"]),
                    setup_close_time=int(row["close_time"]),
                    level=float(hit["level"]),
                    provisional_stop=None if hit["provisional_stop"] is None else float(hit["provisional_stop"]),
                    atr=float(hit["atr"]),
                    trigger_bars=int(cfg["trigger_bars"]),
                    time_stop_bars=int(cfg["time_stop_bars"]),
                    hard_max_bars=hard_max,
                    invalidation=invalidation_text(setup, direction),
                    meta=hit["meta"],
                )
            )
    return found


def window_bounds(candidate: SetupCandidate) -> tuple[int, int]:
    """1H open times that may trigger, half-open [start, end)."""
    start = candidate.setup_open_time + H4_MS
    end = start + candidate.trigger_bars * HOUR_MS
    return start, end


def _latest_4h_value(df4h: pd.DataFrame, open_time_1h: int, column: str) -> float | None:
    """Last 4H value whose bar is closed at or before this 1H bar's close."""
    close_1h = open_time_1h + HOUR_MS - 1
    closed = df4h[df4h["close_time"] <= close_1h]
    if closed.empty or column not in closed.columns:
        return None
    value = closed.iloc[-1][column]
    if not _finite(value):
        return None
    return float(value)


def trigger_hit(candidate: SetupCandidate, bar, prev, df4h: pd.DataFrame, settings: dict | None = None) -> bool:
    direction = candidate.direction
    if candidate.setup == SETUP_SQUEEZE:
        if direction == "long":
            return float(bar["close"]) > candidate.level
        return float(bar["close"]) < candidate.level
    if candidate.setup == SETUP_PULLBACK:
        level = _latest_4h_value(df4h, int(bar["open_time"]), ema_column(settings, "fast"))
        if level is None or prev is None:
            return False
        if not _finite(bar["rsi_14"]) or not _finite(prev["rsi_14"]):
            return False
        if direction == "long":
            crossed = float(prev["close"]) <= level and float(bar["close"]) > level
            rsi_ok = float(bar["rsi_14"]) > float(prev["rsi_14"])
        else:
            crossed = float(prev["close"]) >= level and float(bar["close"]) < level
            rsi_ok = float(bar["rsi_14"]) < float(prev["rsi_14"])
        return crossed and rsi_ok
    if candidate.setup == SETUP_RECLAIM:
        level = candidate.level
        if direction == "long":
            return float(bar["low"]) <= level and float(bar["close"]) > level
        return float(bar["high"]) >= level and float(bar["close"]) < level
    if candidate.setup == SETUP_MEME:
        if direction == "long":
            return float(bar["close"]) > candidate.level
        return float(bar["close"]) < candidate.level
    return False


def first_trigger(candidate: SetupCandidate, df1h: pd.DataFrame, df4h: pd.DataFrame, settings: dict | None = None):
    """Return the first 1H row in the window that qualifies, or None.

    `df1h` must be sorted by open_time.
    """
    start, end = window_bounds(candidate)
    opens = df1h["open_time"].to_numpy()
    left = int(np.searchsorted(opens, start, side="left"))
    right = int(np.searchsorted(opens, end, side="left"))
    window_index = range(left, right)
    for pos in window_index:
        prev = df1h.iloc[pos - 1] if pos > 0 else None
        bar = df1h.iloc[pos]
        if trigger_hit(candidate, bar, prev, df4h, settings):
            return bar
    return None


def latest_atr(df4h: pd.DataFrame, as_of_close_ms: int, fallback: float) -> float:
    closed = df4h[df4h["close_time"] <= as_of_close_ms]
    if closed.empty or not _finite(closed.iloc[-1]["atr_14"]):
        return fallback
    return float(closed.iloc[-1]["atr_14"])


def stop_for_trigger(candidate: SetupCandidate, bar, entry: float, atr: float, settings: dict) -> float | None:
    cfg = settings["setups"][candidate.setup]
    buffer = float(cfg["stop_atr_buffer"]) * atr
    if candidate.setup in (SETUP_SQUEEZE, SETUP_PULLBACK):
        return candidate.provisional_stop
    if candidate.setup == SETUP_RECLAIM:
        if candidate.direction == "long":
            extreme = min(float(candidate.meta["fakeout_low"]), float(bar["low"]))
            return extreme - buffer
        extreme = max(float(candidate.meta["fakeout_high"]), float(bar["high"]))
        return extreme + buffer
    if candidate.setup == SETUP_MEME:
        extreme = float(candidate.meta["signal_low"] if candidate.direction == "long" else candidate.meta["signal_high"])
        return meme_stop(
            candidate.direction,
            entry,
            extreme,
            atr,
            float(cfg["stop_atr_buffer"]),
            float(cfg["stop_cap_atr"]),
        )
    return None


def reclaim_volume_ok(candidate: SetupCandidate, bar, df4h: pd.DataFrame) -> bool:
    if candidate.setup != SETUP_RECLAIM:
        return True
    mult = float(candidate.meta["volume_mult"])
    reclaim_avg = candidate.meta.get("reclaim_vol_avg")
    if reclaim_avg and float(candidate.meta["reclaim_volume"]) >= mult * float(reclaim_avg):
        return True
    # The 4H bar that contains this 1H retest.
    open_1h = int(bar["open_time"])
    contains = df4h[(df4h["open_time"] <= open_1h) & (df4h["open_time"] + H4_MS > open_1h)]
    if contains.empty:
        return False
    host = contains.iloc[-1]
    if not _finite(host["vol_avg_20"]) or float(host["vol_avg_20"]) <= 0:
        return False
    return float(host["volume"]) >= mult * float(host["vol_avg_20"])


def range_room_ok(candidate: SetupCandidate, entry: float, stop: float, settings: dict) -> bool:
    if candidate.setup != SETUP_RECLAIM:
        return True
    room = abs(entry - stop) * float(settings["exits"]["partial_at_r"])
    if candidate.direction == "long":
        cap = float(candidate.meta["range_high"]) - entry
    else:
        cap = entry - float(candidate.meta["range_low"])
    return cap >= room


def recent_setup_bars(df4h: pd.DataFrame, as_of_close_ms: int, max_hours: int = 16) -> list[int]:
    """Integer positions of 4H bars whose trigger window might still be open."""
    horizon = max_hours * HOUR_MS
    positions = []
    for pos in range(len(df4h)):
        close_time = int(df4h.iloc[pos]["close_time"])
        if close_time > as_of_close_ms:
            continue
        if as_of_close_ms - close_time <= horizon:
            positions.append(pos)
    return positions
