"""Point-in-time USDT-M universe. Nothing here reads a bar after the decision index."""

from __future__ import annotations

import numpy as np

# Quoted stables and fiat. A name whose base is in this set is not a coin book.
STABLE_BASES = frozenset(
    {
        "USDT",
        "USDC",
        "BUSD",
        "TUSD",
        "USDP",
        "DAI",
        "FDUSD",
        "USDD",
        "FRAX",
        "LUSD",
        "GUSD",
        "PYUSD",
        "USD1",
        "RLUSD",
        "USDE",
        "BFUSD",
        "AEUR",
        "EUR",
        "EURI",
        "UST",
        "USTC",
        "SUSD",
        "USDN",
        "USDJ",
        "USDS",
        "XUSD",
        "USDQ",
    }
)

# Tokenized equities and index perps. Not a coin, and not in the academic cross-section.
EQUITY_OR_INDEX_BASES = frozenset(
    {
        "AAPL",
        "TSLA",
        "AMZN",
        "GOOGL",
        "GOOG",
        "META",
        "FB",
        "MSFT",
        "NVDA",
        "COIN",
        "MSTR",
        "NFLX",
        "AMD",
        "INTC",
        "BABA",
        "NIO",
        "PLTR",
        "HOOD",
        "GME",
        "AMC",
        "SPY",
        "QQQ",
        "IWM",
        "GLD",
        "SLV",
        "USO",
        "BTCDOM",
        "DEFI",
    }
)

# Major L1 / large non-BTC non-ETH names. Used only for the 3% cluster cap.
L1_BASES = frozenset(
    {
        "SOL",
        "BNB",
        "ADA",
        "AVAX",
        "DOT",
        "ATOM",
        "NEAR",
        "APT",
        "SUI",
        "SEI",
        "TON",
        "TRX",
        "LTC",
        "BCH",
        "XRP",
        "ETC",
        "FIL",
        "ICP",
        "HBAR",
        "ALGO",
        "EGLD",
        "FTM",
        "OP",
        "ARB",
        "INJ",
        "TIA",
        "EOS",
        "XLM",
        "VET",
        "XTZ",
    }
)


def base_asset(symbol: str) -> str:
    symbol = symbol.upper()
    if symbol.endswith("USDT"):
        return symbol[: -len("USDT")]
    return symbol


def is_excluded_symbol(symbol: str) -> bool:
    base = base_asset(symbol)
    return base in STABLE_BASES or base in EQUITY_OR_INDEX_BASES


def strategy_cluster(symbol: str) -> str:
    """btc, eth, l1, or alts. Open risk is summed inside a cluster, both directions."""
    base = base_asset(symbol)
    if base == "BTC":
        return "btc"
    if base == "ETH":
        return "eth"
    if base in L1_BASES:
        return "l1"
    return "alts"


def _window_avg_quote(quote_volume: np.ndarray, close: np.ndarray, i: int, window: int) -> np.ndarray:
    """Mean daily quote volume over [i-window+1, i], missing days as zero, divided by `window`."""
    start = i - window + 1
    slab = quote_volume[:, start : i + 1]
    present = np.isfinite(close[:, start : i + 1])
    values = np.where(present & np.isfinite(slab), slab, 0.0)
    return values.sum(axis=1) / float(window)


def select_universe(
    symbols: list[str],
    close: np.ndarray,
    quote_volume: np.ndarray,
    i: int,
    *,
    oi_value: np.ndarray | None = None,
    top_n: int = 40,
    min_history: int = 60,
    vol_window: int = 30,
    min_avg_quote: float = 20_000_000.0,
    min_oi_value: float = 5_000_000.0,
    oi_max_age: int = 3,
) -> list[int]:
    """Eligible symbol indices at decision day `i`, best liquidity first.

    Uses only columns at or before `i`. A name must have a bar on day `i`
    (so a delisted contract drops out the day it stops printing), at least
    `min_history` daily closes up to and including `i`, trailing quote volume
    of at least `min_avg_quote` per day, and, when an open-interest print
    exists within `oi_max_age` days, notional OI of at least `min_oi_value`.
    A missing OI print does not exclude the name. Stables and tokenized
    equities are excluded before the rank, so they do not take a top-N slot.
    """
    n = len(symbols)
    if i < 0 or n == 0 or i >= close.shape[1] or i + 1 < vol_window:
        return []
    has_bar = np.isfinite(close[:, i]) & (close[:, i] > 0)
    history = np.isfinite(close[:, : i + 1]).sum(axis=1) >= int(min_history)
    avg = _window_avg_quote(quote_volume, close, i, int(vol_window))
    liquid = avg >= float(min_avg_quote)
    allowed = np.array([not is_excluded_symbol(symbol) for symbol in symbols], dtype=bool)
    oi_ok = np.ones(n, dtype=bool)
    if oi_value is not None and min_oi_value > 0:
        age = max(0, int(oi_max_age))
        recent = oi_value[:, max(0, i - age) : i + 1]
        finite = np.isfinite(recent)
        has_reading = finite.any(axis=1)
        # Last finite reading in the age window. Walk from the decision day backward.
        last = np.full(n, np.nan)
        for column in range(recent.shape[1] - 1, -1, -1):
            values = recent[:, column]
            take = np.isnan(last) & np.isfinite(values)
            last[take] = values[take]
        too_small = has_reading & (last < float(min_oi_value))
        oi_ok = ~too_small
    mask = has_bar & history & liquid & allowed & oi_ok
    chosen = [index for index in range(n) if mask[index]]
    chosen.sort(key=lambda index: (-float(avg[index]), symbols[index]))
    if top_n is not None and top_n > 0:
        chosen = chosen[: int(top_n)]
    return chosen


def volume_rank(
    symbols: list[str],
    close: np.ndarray,
    quote_volume: np.ndarray,
    i: int,
    *,
    vol_window: int = 30,
) -> dict[int, int]:
    """0 is the most liquid name with a bar on day `i`. Ties break on the symbol."""
    if i < 0 or i >= close.shape[1] or i + 1 < vol_window:
        return {}
    has_bar = np.isfinite(close[:, i]) & (close[:, i] > 0)
    avg = _window_avg_quote(quote_volume, close, i, int(vol_window))
    order = [index for index in range(len(symbols)) if has_bar[index] and not is_excluded_symbol(symbols[index])]
    order.sort(key=lambda index: (-float(avg[index]), symbols[index]))
    return {index: rank for rank, index in enumerate(order)}


def gap_ok(close_row: np.ndarray, i: int, lookback: int, max_missing: int = 2) -> bool:
    """True when the calendar window ending at `i` is missing at most `max_missing` bars."""
    if lookback <= 0 or i < lookback or i >= len(close_row):
        return False
    window = close_row[i - lookback : i + 1]
    if not np.isfinite(window[0]) or not np.isfinite(window[-1]) or window[0] <= 0 or window[-1] <= 0:
        return False
    missing = int(np.sum(~np.isfinite(window)))
    return missing <= int(max_missing)


def momentum_score(close_row: np.ndarray, i: int, lookback: int, max_missing: int = 2) -> float:
    """close[i] / close[i-lookback] - 1. NaN when the window has too many holes."""
    if not gap_ok(close_row, i, lookback, max_missing=max_missing):
        return float("nan")
    return float(close_row[i] / close_row[i - lookback] - 1.0)
