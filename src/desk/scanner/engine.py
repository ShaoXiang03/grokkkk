"""Evaluate one closed 1H bar. The backtester calls this same function."""

from __future__ import annotations

import pandas as pd

from desk.indicators.core import add_indicators, ema_column
from desk.rules.regime import daily_bias_ok, direction_allowed, regime_from_frame
from desk.rules.setups import (
    detect_setups_on_bar,
    first_trigger,
    latest_atr,
    range_room_ok,
    reclaim_volume_ok,
    recent_setup_bars,
    stop_for_trigger,
)
from desk.scanner.signals import compose_signal
from desk.timeutils import HOUR_MS


def with_indicators(df: pd.DataFrame | None, settings: dict) -> pd.DataFrame | None:
    if df is None or df.empty:
        return df
    return add_indicators(df, settings)


def quote_volume_24h(df1h: pd.DataFrame) -> float | None:
    if df1h is None or len(df1h) < 24:
        return None
    last_close = int(df1h.iloc[-1]["close_time"])
    window = df1h[df1h["close_time"] > last_close - 24 * HOUR_MS]
    if len(window) < 24:
        return None
    return float(window["quote_volume"].tail(24).sum())


def evaluate_frame_at(
    *,
    symbol: str,
    df1h: pd.DataFrame,
    df4h: pd.DataFrame,
    df1d: pd.DataFrame | None,
    btc_1d: pd.DataFrame | None,
    i_1h: int,
    settings: dict,
    market: dict,
    account: dict,
    open_positions: list[dict] | None = None,
    cooldown_until_ms: int | None = None,
    blocklist_symbols: set[str] | None = None,
    blocklist_available: bool = True,
    onchain_raw: dict | None = None,
    historical: bool = False,
) -> list[dict]:
    """Return zero or more packets whose trigger is exactly the 1H bar at i_1h.

    Frames may include later rows. Only rows closed at this bar are read.
    """
    if i_1h < 0 or i_1h >= len(df1h):
        return []
    df1h_now = _ready(df1h.iloc[: i_1h + 1], settings)
    bar = df1h_now.iloc[-1]
    now_ms = int(bar["close_time"])
    df4h_now = _ready(df4h[df4h["close_time"] <= now_ms], settings)
    if df4h_now.empty:
        return []
    df1d_now = None if df1d is None else _ready(df1d[df1d["close_time"] <= now_ms], settings)
    btc_now = None if btc_1d is None else _ready(btc_1d[btc_1d["close_time"] <= now_ms], settings)
    min_bars = int(settings["indicators"]["sma_slow"])
    regime = regime_from_frame(btc_now, min_bars)
    market = dict(market)
    market.setdefault("symbol", symbol)
    if market.get("quote_volume_24h") is None:
        market["quote_volume_24h"] = quote_volume_24h(df1h_now)
    market["last_1h_quote_volume"] = float(bar["quote_volume"]) if pd.notna(bar["quote_volume"]) else None

    packets: list[dict] = []
    positions = list(open_positions or [])
    blocked = set(blocklist_symbols or [])
    for j in recent_setup_bars(df4h_now, now_ms):
        candidates = detect_setups_on_bar(symbol, df4h_now, j, df1d_now, regime, settings)
        for candidate in candidates:
            trigger = first_trigger(candidate, df1h_now, df4h_now, settings)
            if trigger is None:
                continue
            if int(trigger["open_time"]) != int(bar["open_time"]):
                continue
            if not reclaim_volume_ok(candidate, bar, df4h_now):
                continue
            entry = float(bar["close"])
            atr = latest_atr(df4h_now, now_ms, candidate.atr)
            candidate.atr = atr
            stop = stop_for_trigger(candidate, bar, entry, atr, settings)
            stop_ok = stop is not None
            stop_reason = "stop computed" if stop_ok else "stop could not be placed beyond the signal extreme"
            if stop_ok and abs(entry - stop) > float(settings["setups"][candidate.setup]["max_stop_atr"]) * atr:
                stop_ok = False
                stop_reason = "stop is farther than the setup ATR cap"
            if stop_ok and not range_room_ok(candidate, entry, stop, settings):
                stop_ok = False
                stop_reason = "opposite side of the range is closer than the partial target"
            bias_required, bias_bars = _bias_required(candidate.setup, df1d_now, settings)
            bias = daily_bias_ok(
                df1d_now,
                candidate.direction,
                required=bias_required,
                min_bars=bias_bars,
                ema_column=ema_column(settings, "slow"),
            )
            packet = compose_signal(
                candidate=candidate,
                trigger_bar=bar,
                entry=entry,
                stop=stop,
                stop_ok=stop_ok,
                stop_reason=stop_reason,
                market=market,
                account=account,
                open_positions=positions,
                settings=settings,
                now_ms=now_ms,
                cooldown_until_ms=cooldown_until_ms,
                blocklist_symbols=blocked,
                blocklist_available=blocklist_available,
                onchain_raw=onchain_raw,
                regime_ok=direction_allowed(regime, candidate.setup, candidate.direction),
                regime_name=regime,
                bias_ok=bias,
                historical=historical,
            )
            packets.append(packet)
    return packets


def _bias_required(setup: str, daily: pd.DataFrame | None, settings: dict) -> tuple[bool, int]:
    from desk.rules.regime import SETUP_MEME, SETUP_RECLAIM

    if setup == SETUP_RECLAIM:
        return False, int(settings["indicators"]["sma_slow"])
    bars = 0 if daily is None else len(daily)
    if setup == SETUP_MEME:
        need = int(settings["setups"]["meme_momentum"]["min_daily_bars_for_bias"])
        return bars >= need, need
    return True, int(settings["setups"][setup]["min_daily_bars"])


def _ready(df: pd.DataFrame, settings: dict) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    fast = ema_column(settings, "fast")
    if fast in df.columns and "atr_14" in df.columns and "sma_slow" in df.columns:
        return df
    return add_indicators(df, settings)
