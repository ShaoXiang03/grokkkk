"""BTC daily regime and per-coin daily EMA-200 bias.

Equal to the 200-day SMA is treated as not above it, so the regime is
short-favored. Equal to the 50-day SMA while above the 200-day is caution,
because the long-favored row requires a close above both averages.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

SETUP_SQUEEZE = "squeeze_breakout"
SETUP_PULLBACK = "trend_pullback"
SETUP_RECLAIM = "range_reclaim"
SETUP_MEME = "meme_momentum"

LONG_FAVORED = "long_favored"
CAUTION = "caution"
SHORT_FAVORED = "short_favored"


def classify_regime(close: float, sma_50: float, sma_200: float) -> str | None:
    if any(_bad(v) for v in (close, sma_50, sma_200)):
        return None
    if close > sma_200 and close > sma_50:
        return LONG_FAVORED
    if close > sma_200 and close <= sma_50:
        return CAUTION
    return SHORT_FAVORED


def regime_from_frame(btc_daily: pd.DataFrame | None, min_bars: int = 200) -> str | None:
    if btc_daily is None or len(btc_daily) < min_bars:
        return None
    row = btc_daily.iloc[-1]
    sma_fast = row["sma_fast"] if "sma_fast" in row.index else row["sma_50"]
    sma_slow = row["sma_slow"] if "sma_slow" in row.index else row["sma_200"]
    return classify_regime(float(row["close"]), float(sma_fast), float(sma_slow))


def direction_allowed(regime: str | None, setup: str, direction: str) -> bool:
    """Range reclaim may trade the unfavored side. A missing regime allows nothing."""
    if regime is None:
        return False
    if setup == SETUP_RECLAIM:
        return direction in ("long", "short")
    if regime == LONG_FAVORED:
        return direction == "long"
    if regime == SHORT_FAVORED:
        return direction == "short"
    if regime == CAUTION:
        return setup in (SETUP_PULLBACK, SETUP_MEME) and direction in ("long", "short")
    return False


def daily_bias_ok(
    daily: pd.DataFrame | None,
    direction: str,
    *,
    required: bool,
    min_bars: int = 200,
    ema_column: str = "ema_200",
) -> bool | None:
    """Return True/False, or None when the check is not required.

    None means "not applicable", which the caller treats as a pass.
    A required check with too little history returns False.
    """
    if not required:
        return None
    if daily is None or len(daily) < min_bars or ema_column not in daily.columns:
        return False
    row = daily.iloc[-1]
    close = float(row["close"])
    ema_slow = float(row[ema_column])
    if _bad(close) or _bad(ema_slow):
        return False
    if direction == "long":
        return close > ema_slow
    return close < ema_slow


def _bad(value: Any) -> bool:
    try:
        return value is None or (isinstance(value, float) and math.isnan(value)) or pd.isna(value)
    except TypeError:
        return False
