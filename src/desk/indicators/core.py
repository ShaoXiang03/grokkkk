"""Causal indicators. Value at index i depends only on rows 0..i.

EMA is seeded with the SMA of the first `period` samples, then smoothed with
k = 2 / (period + 1). RSI and ATR use Wilder smoothing. Bollinger Bands use
the population standard deviation (ddof=0), which matches the usual charting
default. Bandwidth is (upper - lower) / middle.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    if period <= 0 or len(values) < period:
        return pd.Series(out, index=series.index)
    if np.isnan(values[:period]).any():
        return pd.Series(out, index=series.index)
    k = 2.0 / (period + 1.0)
    out[period - 1] = float(values[:period].mean())
    for i in range(period, len(values)):
        price = values[i]
        if np.isnan(price):
            out[i] = np.nan
            continue
        prev = out[i - 1]
        if np.isnan(prev):
            out[i] = np.nan
            continue
        out[i] = prev + k * (price - prev)
    return pd.Series(out, index=series.index)


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def rsi_wilder(series: pd.Series, period: int) -> pd.Series:
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) <= period:
        return pd.Series(out, index=series.index)
    delta = np.diff(values, prepend=np.nan)
    gain = np.where(np.isnan(delta), np.nan, np.maximum(delta, 0.0))
    loss = np.where(np.isnan(delta), np.nan, np.maximum(-delta, 0.0))
    # First average uses the first `period` changes, which end at index `period`.
    window_gain = gain[1 : period + 1]
    window_loss = loss[1 : period + 1]
    if np.isnan(window_gain).any() or np.isnan(window_loss).any():
        return pd.Series(out, index=series.index)
    avg_gain = float(window_gain.mean())
    avg_loss = float(window_loss.mean())
    out[period] = _rsi_from_averages(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        if np.isnan(gain[i]) or np.isnan(loss[i]):
            out[i] = np.nan
            continue
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        out[i] = _rsi_from_averages(avg_gain, avg_loss)
    return pd.Series(out, index=series.index)


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0 or not np.isfinite(avg_loss) or not np.isfinite(avg_gain):
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    if not np.isfinite(rs):
        return 100.0
    return 100.0 - (100.0 / (1.0 + rs))


def true_range(df: pd.DataFrame) -> pd.Series:
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    prev_close = np.roll(close, 1)
    prev_close[0] = np.nan
    hl = high - low
    hc = np.abs(high - prev_close)
    lc = np.abs(low - prev_close)
    tr = np.fmax(hl, np.fmax(hc, lc))
    tr[0] = hl[0]
    return pd.Series(tr, index=df.index)


def atr_wilder(df: pd.DataFrame, period: int) -> pd.Series:
    tr = true_range(df).to_numpy(dtype=float)
    out = np.full(len(tr), np.nan)
    if len(tr) < period or np.isnan(tr[:period]).any():
        return pd.Series(out, index=df.index)
    out[period - 1] = float(tr[:period].mean())
    for i in range(period, len(tr)):
        if np.isnan(tr[i]) or np.isnan(out[i - 1]):
            out[i] = np.nan
            continue
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return pd.Series(out, index=df.index)


def bollinger(series: pd.Series, period: int, std_mult: float, ddof: int = 0) -> pd.DataFrame:
    mid = series.rolling(period, min_periods=period).mean()
    std = series.rolling(period, min_periods=period).std(ddof=ddof)
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    bandwidth = (upper - lower) / mid.replace(0, np.nan)
    return pd.DataFrame(
        {"bb_mid": mid, "bb_upper": upper, "bb_lower": lower, "bandwidth": bandwidth},
        index=series.index,
    )


def add_indicators(df: pd.DataFrame, settings: dict) -> pd.DataFrame:
    """Return a copy with indicator columns. Existing indicator columns are recomputed."""
    cfg = settings["indicators"]
    out = df.copy()
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "quote_volume" in out.columns:
        out["quote_volume"] = pd.to_numeric(out["quote_volume"], errors="coerce")
    else:
        out["quote_volume"] = out["close"] * out["volume"]

    close = out["close"]
    for period in cfg["ema_periods"]:
        out[f"ema_{int(period)}"] = ema(close, int(period))
    out["rsi_14"] = rsi_wilder(close, int(cfg["rsi_period"]))
    out["atr_14"] = atr_wilder(out, int(cfg["atr_period"]))
    vol_period = int(cfg["volume_avg_period"])
    out["vol_avg_20"] = sma(out["volume"], vol_period)
    bands = bollinger(close, int(cfg["bb_period"]), float(cfg["bb_std"]), int(cfg["bb_ddof"]))
    for col in bands.columns:
        out[col] = bands[col]
    out["sma_fast"] = sma(close, int(cfg["sma_fast"]))
    out["sma_slow"] = sma(close, int(cfg["sma_slow"]))
    # Aliases so a 50/200 configuration is visible under the rule names.
    out["sma_50"] = out["sma_fast"]
    out["sma_200"] = out["sma_slow"]
    return out


def ema_column(settings: dict, role: str) -> str:
    """Map fast/mid/slow onto the configured EMA periods. Defaults are 20/50/200."""
    periods = [int(p) for p in settings["indicators"]["ema_periods"]]
    if len(periods) < 3:
        raise ValueError("indicators.ema_periods needs three values (fast, mid, slow)")
    index = {"fast": 0, "mid": 1, "slow": 2}[role]
    return f"ema_{periods[index]}"
