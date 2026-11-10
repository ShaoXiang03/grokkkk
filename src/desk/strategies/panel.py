"""Calendar-aligned daily panel. Indicator at index i uses bars 0..i only."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from desk.strategies.universe import select_universe, volume_rank
from desk.timeutils import DAY_MS

PREMIUM_INTERVAL = "premium1d"


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    frame = pd.DataFrame(values.T)
    return frame.rolling(window, min_periods=window).mean().to_numpy().T


def _rolling_std(values: np.ndarray, window: int, min_periods: int) -> np.ndarray:
    frame = pd.DataFrame(values.T)
    return frame.rolling(window, min_periods=min_periods).std(ddof=1).to_numpy().T


def _prior_extreme(values: np.ndarray, window: int, how: str) -> np.ndarray:
    frame = pd.DataFrame(values.T)
    rolled = frame.rolling(window, min_periods=window)
    extreme = rolled.max() if how == "max" else rolled.min()
    return extreme.shift(1).to_numpy().T


def _atr_valid(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder ATR on each symbol's existing bars, mapped back onto the calendar."""
    n_sym, n_days = close.shape
    out = np.full((n_sym, n_days), np.nan)
    for s in range(n_sym):
        valid = (
            np.isfinite(close[s])
            & np.isfinite(high[s])
            & np.isfinite(low[s])
            & (close[s] > 0)
            & (high[s] >= low[s])
        )
        idx = np.flatnonzero(valid)
        if idx.size < period:
            continue
        h = high[s, idx]
        lo = low[s, idx]
        c = close[s, idx]
        prev = np.empty_like(c)
        prev[0] = np.nan
        prev[1:] = c[:-1]
        tr = np.maximum(h - lo, np.maximum(np.abs(h - prev), np.abs(lo - prev)))
        tr[0] = h[0] - lo[0]
        atr = np.empty(idx.size)
        atr[:] = np.nan
        val = float(np.mean(tr[:period]))
        atr[period - 1] = val
        span = period - 1
        for cursor in range(period, idx.size):
            val = (val * span + tr[cursor]) / period
            atr[cursor] = val
        out[s, idx] = atr
    return out


def _ema_valid(close: np.ndarray, period: int) -> np.ndarray:
    """Causal EMA. A hole resets the seed so a halt does not carry a stale value across it."""
    n_sym, n_days = close.shape
    out = np.full((n_sym, n_days), np.nan)
    k = 2.0 / (period + 1.0)
    for s in range(n_sym):
        row = close[s]
        cursor = 0
        while cursor + period <= n_days:
            window = row[cursor : cursor + period]
            if not np.isfinite(window).all() or np.any(window <= 0):
                cursor += 1
                continue
            val = float(window.mean())
            out[s, cursor + period - 1] = val
            broke = cursor + period
            for j in range(cursor + period, n_days):
                price = row[j]
                if not np.isfinite(price) or price <= 0:
                    broke = j + 1
                    break
                val = val + k * (price - val)
                out[s, j] = val
            else:
                broke = n_days
            cursor = broke
    return out


def _rolling_z(values: np.ndarray, window: int, min_periods: int) -> np.ndarray:
    frame = pd.DataFrame(values.T)
    mean = frame.rolling(window, min_periods=min_periods).mean()
    std = frame.rolling(window, min_periods=min_periods).std(ddof=1).replace(0, np.nan)
    return ((frame - mean) / std).to_numpy().T


@dataclass
class Panel:
    symbols: list[str]
    day_ms: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    quote_volume: np.ndarray
    oi: np.ndarray
    oi_value: np.ndarray
    premium: np.ndarray
    # funding_events[symbol] = (time_ms, rate, interval_hours) arrays, time-sorted.
    funding_events: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = field(default_factory=dict)
    funding_ann: np.ndarray | None = None

    @property
    def n(self) -> int:
        return len(self.symbols)

    @property
    def t(self) -> int:
        return int(self.day_ms.size)

    def index(self, symbol: str) -> int | None:
        try:
            return self.symbols.index(symbol)
        except ValueError:
            return None


class Features:
    """Lazy indicators. Caches are safe because the panel is not mutated during a run."""

    def __init__(self, panel: Panel):
        self.panel = panel
        self.symbols = panel.symbols
        self.day_ms = panel.day_ms
        self.open = panel.open
        self.high = panel.high
        self.low = panel.low
        self.close = panel.close
        self.quote_volume = panel.quote_volume
        self.oi = panel.oi
        self.oi_value = panel.oi_value
        self.premium = panel.premium
        self.funding_ann = panel.funding_ann if panel.funding_ann is not None else np.full_like(panel.close, np.nan)
        epoch_days = (self.day_ms // DAY_MS).astype(np.int64)
        # 1970-01-01 was a Thursday (weekday 3). Sunday is 6.
        self.weekday = (epoch_days + 3) % 7
        self.ret = np.full_like(panel.close, np.nan)
        prev = np.roll(panel.close, 1, axis=1)
        prev[:, 0] = np.nan
        ok = np.isfinite(panel.close) & np.isfinite(prev) & (prev > 0)
        self.ret[ok] = panel.close[ok] / prev[ok] - 1.0
        self.atr = _atr_valid(panel.high, panel.low, panel.close, 14)
        self._uni: dict[tuple, list[int]] = {}
        self._rank: dict[int, dict[int, int]] = {}
        self._extra: dict[str, np.ndarray] = {}
        self.sym = {symbol: i for i, symbol in enumerate(panel.symbols)}
        self.btc = self.sym.get("BTCUSDT")

    def ensure(self, kind: str) -> None:
        if kind in self._extra or kind == "base":
            return
        if kind == "sma50":
            self._extra[kind] = _rolling_mean(self.close, 50)
        elif kind == "sma200":
            self._extra[kind] = _rolling_mean(self.close, 200)
        elif kind == "vol20":
            self._extra[kind] = _rolling_std(self.ret, 20, 15)
        elif kind == "vol30":
            self._extra[kind] = _rolling_std(self.ret, 30, 20)
        elif kind == "high20":
            self._extra[kind] = _prior_extreme(self.high, 20, "max")
        elif kind == "low20":
            self._extra[kind] = _prior_extreme(self.low, 20, "min")
        elif kind == "high10":
            self._extra[kind] = _prior_extreme(self.high, 10, "max")
        elif kind == "low10":
            self._extra[kind] = _prior_extreme(self.low, 10, "min")
        elif kind == "ema" or (kind.startswith("ema") and kind[3:].isdigit()):
            if "ema10" not in self._extra:
                for period in (10, 20, 30, 50, 200):
                    self._extra[f"ema{period}"] = _ema_valid(self.close, period)
                self._extra["ema"] = self._extra["ema10"]
        elif kind == "funding_z":
            smooth = pd.DataFrame(self.funding_ann.T).rolling(3, min_periods=3).mean().to_numpy().T
            self._extra["funding_smooth"] = smooth
            self._extra["funding_z"] = _rolling_z(smooth, 365, 60)
        elif kind == "oi_z":
            self._extra["oi_z"] = _rolling_z(self.oi, 365, 60)
        else:
            raise KeyError(kind)

    def column(self, kind: str) -> np.ndarray:
        self.ensure(kind)
        return self._extra[kind]

    def universe(self, i: int, params: dict) -> list[int]:
        top_n = int(params.get("top_n", 40))
        min_history = int(params.get("min_history", 60))
        min_avg = float(params.get("min_avg_quote", 20_000_000.0))
        min_oi = float(params.get("min_oi_value", 5_000_000.0))
        window = int(params.get("vol_window", 30))
        key = (i, top_n, min_history, min_avg, min_oi, window)
        if key not in self._uni:
            self._uni[key] = select_universe(
                self.symbols,
                self.close,
                self.quote_volume,
                i,
                oi_value=self.oi_value,
                top_n=top_n,
                min_history=min_history,
                vol_window=window,
                min_avg_quote=min_avg,
                min_oi_value=min_oi,
            )
        return self._uni[key]

    def ranks(self, i: int, vol_window: int = 30) -> dict[int, int]:
        key = i if vol_window == 30 else -(i + 1)
        # Only the default 30-day rank is cached under the day index. Tests use 30.
        if vol_window != 30:
            return volume_rank(self.symbols, self.close, self.quote_volume, i, vol_window=vol_window)
        if i not in self._rank:
            self._rank[i] = volume_rank(self.symbols, self.close, self.quote_volume, i, vol_window=30)
        return self._rank[i]

    def slip(self, symbol_index: int, i: int, params: dict) -> float:
        rank = self.ranks(i, int(params.get("vol_window", 30))).get(symbol_index)
        top = int(params.get("top_liquid", 10))
        if rank is not None and rank < top:
            return float(params.get("slip_top", 0.0004))
        return float(params.get("slip_rest", 0.0008))

    def day_index(self, ms: int, side: str = "left") -> int:
        return int(np.searchsorted(self.day_ms, ms, side=side))


def _ohlc(symbols: list[str], day_ms: np.ndarray, frame: pd.DataFrame) -> dict[str, np.ndarray]:
    arrays = {name: np.full((len(symbols), day_ms.size), np.nan) for name in ("open", "high", "low", "close", "quote_volume")}
    if frame is None or frame.empty:
        return arrays
    lookup = {symbol: i for i, symbol in enumerate(symbols)}
    day_lookup = {int(day): i for i, day in enumerate(day_ms)}
    # Keep the last row of a duplicate day.
    ordered = frame.sort_values(["symbol", "open_time"])
    for row in ordered.itertuples(index=False):
        s = lookup.get(str(row.symbol))
        if s is None:
            continue
        day = int(row.open_time) - (int(row.open_time) % DAY_MS)
        i = day_lookup.get(day)
        if i is None:
            continue
        arrays["open"][s, i] = row.open
        arrays["high"][s, i] = row.high
        arrays["low"][s, i] = row.low
        arrays["close"][s, i] = row.close
        arrays["quote_volume"][s, i] = row.quote_volume
    return arrays


def panel_from_frames(
    bars: dict[str, pd.DataFrame] | pd.DataFrame,
    *,
    funding: pd.DataFrame | None = None,
    metrics: pd.DataFrame | None = None,
    premium: pd.DataFrame | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> Panel:
    """Build a continuous UTC calendar. Missing names and days stay NaN.

    ``bars`` is either a frame with a symbol column or a dict of symbol -> frame.
    """
    if isinstance(bars, dict):
        frames = []
        for symbol, frame in bars.items():
            copied = frame.copy()
            copied["symbol"] = symbol
            frames.append(copied)
        bars = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if bars.empty:
        raise ValueError("no daily bars")
    bars = bars.dropna(subset=["open_time"])
    day0 = int(bars["open_time"].min())
    day1 = int(bars["open_time"].max())
    day0 -= day0 % DAY_MS
    day1 -= day1 % DAY_MS
    if start_ms is not None:
        day0 = min(day0, int(start_ms) - (int(start_ms) % DAY_MS))
    if end_ms is not None:
        last = int(end_ms) - (int(end_ms) % DAY_MS)
        day1 = max(day1, last)
    n_days = int((day1 - day0) // DAY_MS) + 1
    day_ms = day0 + np.arange(n_days, dtype=np.int64) * DAY_MS
    symbols = sorted(set(bars["symbol"].astype(str)))
    ohlc = _ohlc(symbols, day_ms, bars)
    oi = np.full((len(symbols), n_days), np.nan)
    oi_value = np.full((len(symbols), n_days), np.nan)
    if metrics is not None and not metrics.empty:
        lookup = {symbol: i for i, symbol in enumerate(symbols)}
        day_lookup = {int(day): i for i, day in enumerate(day_ms)}
        ordered = metrics.sort_values(["symbol", "day_ms", "create_time"])
        for row in ordered.itertuples(index=False):
            s = lookup.get(str(row.symbol))
            i = day_lookup.get(int(row.day_ms))
            if s is None or i is None:
                continue
            oi[s, i] = row.sum_open_interest
            oi_value[s, i] = row.sum_open_interest_value
    premium_arr = np.full((len(symbols), n_days), np.nan)
    if premium is not None and not premium.empty:
        premium_arr = _ohlc(symbols, day_ms, premium)["close"]
    funding_events: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    funding_ann = np.full((len(symbols), n_days), np.nan)
    if funding is not None and not funding.empty:
        lookup = {symbol: i for i, symbol in enumerate(symbols)}
        for symbol, group in funding.groupby("symbol", sort=False):
            s = lookup.get(str(symbol))
            if s is None:
                continue
            group = group.sort_values("funding_time")
            times = group["funding_time"].to_numpy(dtype=np.int64)
            rates = group["funding_rate"].to_numpy(dtype=float)
            if "interval_hours" in group.columns:
                hours = group["interval_hours"].to_numpy(dtype=float)
                hours = np.where(np.isfinite(hours) & (hours > 0), hours, 8.0)
            else:
                hours = np.full(times.shape, 8.0)
            funding_events[str(symbol)] = (times, rates, hours)
            # Signal uses prints in [day open, next day open). Annualise by the print's own interval.
            ann = rates * (365.0 * 24.0 / hours)
            buckets: dict[int, list[float]] = {}
            for stamp, value in zip(times, ann, strict=False):
                if not np.isfinite(value):
                    continue
                day = int(stamp) - (int(stamp) % DAY_MS)
                i = int((day - int(day_ms[0])) // DAY_MS)
                if 0 <= i < n_days:
                    buckets.setdefault(i, []).append(float(value))
            for i, values in buckets.items():
                funding_ann[s, i] = float(np.mean(values))
    return Panel(
        symbols=symbols,
        day_ms=day_ms,
        open=ohlc["open"],
        high=ohlc["high"],
        low=ohlc["low"],
        close=ohlc["close"],
        quote_volume=ohlc["quote_volume"],
        oi=oi,
        oi_value=oi_value,
        premium=premium_arr,
        funding_events=funding_events,
        funding_ann=funding_ann,
    )


def load_panel(store, *, premium_interval: str = PREMIUM_INTERVAL) -> Panel:
    """Load the cached daily archive. Premium bars live in klines as ``premium1d``."""
    bars = store.load_interval_frame("1d")
    premium = store.load_interval_frame(premium_interval)
    funding = store.load_funding_frame()
    metrics = store.load_metrics_frame()
    return panel_from_frames(bars, funding=funding, metrics=metrics, premium=premium)
