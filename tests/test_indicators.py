"""Known indicator values, computed by hand from the formulas in core.py."""

import numpy as np
import pandas as pd

from desk.config import load_settings
from desk.indicators.core import add_indicators, atr_wilder, bollinger, ema, rsi_wilder, sma


def test_ema_seeds_with_sma_then_smooths():
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    out = ema(series, 3)
    # Seed at index 2 is (1+2+3)/3 = 2. k = 0.5.
    assert out.iloc[0] != out.iloc[0]  # NaN
    assert out.iloc[2] == 2
    assert out.iloc[3] == 3
    assert out.iloc[4] == 4
    assert out.iloc[5] == 5


def test_sma_and_bollinger_population_std():
    close = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    averages = sma(close, 3)
    assert np.isnan(averages.iloc[1])
    assert averages.iloc[2] == 2
    assert averages.iloc[3] == 3
    bands = bollinger(close, 3, 2.0, ddof=0)
    # At index 2: mid=2, population std = sqrt(2/3).
    std = (2.0 / 3.0) ** 0.5
    assert bands.loc[2, "bb_mid"] == 2
    assert bands.loc[2, "bb_upper"] == 2 + 2 * std
    assert bands.loc[2, "bb_lower"] == 2 - 2 * std
    assert bands.loc[2, "bandwidth"] == (4 * std) / 2


def test_rsi_wilder_hand_values():
    close = pd.Series([10.0, 11.0, 12.0, 11.0, 10.0, 12.0])
    out = rsi_wilder(close, 3)
    assert abs(out.iloc[3] - (100 - 100 / 3)) < 1e-9
    assert abs(out.iloc[4] - (100 - 100 / 1.8)) < 1e-9
    assert abs(out.iloc[5] - (100 - 100 / 3.6)) < 1e-9


def test_atr_wilder_hand_values():
    df = pd.DataFrame(
        {
            "high": [10.0, 11.0, 12.0, 11.0, 13.0],
            "low": [8.0, 9.0, 10.0, 7.0, 8.0],
            "close": [9.0, 10.0, 11.0, 8.0, 12.0],
        }
    )
    out = atr_wilder(df, 3)
    assert abs(out.iloc[2] - 2.0) < 1e-9
    assert abs(out.iloc[3] - (8.0 / 3.0)) < 1e-9
    assert abs(out.iloc[4] - (31.0 / 9.0)) < 1e-9


def test_indicator_does_not_use_future_bars():
    rng = np.random.default_rng(4)
    n = 80
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    df = pd.DataFrame(
        {
            "open_time": np.arange(n),
            "close_time": np.arange(n),
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": np.full(n, 10.0),
        }
    )
    settings = load_settings()
    original = add_indicators(df, settings)
    mutated = df.copy()
    mutated.loc[mutated.index[-15:], "close"] = 10_000
    mutated.loc[mutated.index[-15:], "high"] = 10_001
    mutated.loc[mutated.index[-15:], "low"] = 9_999
    changed = add_indicators(mutated, settings)
    left = original.iloc[:-15]
    right = changed.iloc[:-15]
    for column in ("ema_20", "rsi_14", "atr_14", "bb_upper", "bandwidth", "sma_slow", "vol_avg_20"):
        pd.testing.assert_series_equal(left[column], right[column], check_names=False)
