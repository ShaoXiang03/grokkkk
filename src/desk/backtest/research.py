"""Signal scan and portfolio simulation for the exit and Setup B study.

Chart detection runs once. Exit and Setup B variants are applied when the
portfolio is simulated, so the same signals are reused. Management goes through
`manage_bar`, the same function the bar-by-bar replayer uses.

The scan resets Setup B's research switches to the v0.3 baseline before
detection. Variants can only remove a baseline pullback or widen its initial
stop to the stored 3-bar swing. They do not invent new chart patterns.
"""

from __future__ import annotations

from collections import Counter, defaultdict

import numpy as np
import pandas as pd

from desk.backtest.costs import adverse_price
from desk.backtest.replay import (
    _apply_due_funding,
    _close_qty,
    _finished_trade,
    _historical_status,
    _live_risk,
    _open_from_packet,
    _slip,
    _week_key,
    manage_bar,
)
from desk.indicators.core import add_indicators, ema_column
from desk.rules.exits import closed_structure_window
from desk.rules.regime import (
    SETUP_MEME,
    SETUP_PULLBACK,
    SETUP_RECLAIM,
    SETUP_SQUEEZE,
    classify_regime,
    direction_allowed,
)
from desk.rules.risk_gates import evaluate_gates, first_failure, resolve_spread
from desk.rules.setups import detect_setups_on_bar, first_trigger, range_room_ok, reclaim_volume_ok, stop_for_trigger
from desk.rules.sizing import plan_position
from desk.rules.universe import cluster_for, cluster_key, funding_blocks, volume_and_spread_limits
from desk.scanner.signals import signal_id_for
from desk.timeutils import DAY_MS, H4_MS, HOUR_MS, myt_date

SETUP_RANK = {
    SETUP_SQUEEZE: 0,
    SETUP_PULLBACK: 1,
    SETUP_RECLAIM: 2,
    SETUP_MEME: 3,
}

BASELINE_PULLBACK = {
    "min_ema_gap_atr": 0.0,
    "allow_caution": True,
    "stop_swing_bars": 1,
    "require_fast_ema_tag": False,
}

EXIT_GRID = (
    {"name": "ema_immediate_2r", "trail_mode": "ema", "trail_arm": "immediate", "partial_at_r": 2.0, "chandelier_atr_mult": 2.0, "enforce_min_reward_risk": True},
    {"name": "ema_after_partial_2r", "trail_mode": "ema", "trail_arm": "after_partial", "partial_at_r": 2.0, "chandelier_atr_mult": 2.0, "enforce_min_reward_risk": True},
    {"name": "ema_after_1r_2r", "trail_mode": "ema", "trail_arm": "after_1r", "partial_at_r": 2.0, "chandelier_atr_mult": 2.0, "enforce_min_reward_risk": True},
    {"name": "chandelier_2_after_1r_2r", "trail_mode": "chandelier", "trail_arm": "after_1r", "partial_at_r": 2.0, "chandelier_atr_mult": 2.0, "enforce_min_reward_risk": True},
    {"name": "chandelier_3_after_1r_2r", "trail_mode": "chandelier", "trail_arm": "after_1r", "partial_at_r": 2.0, "chandelier_atr_mult": 3.0, "enforce_min_reward_risk": True},
    {"name": "structure_after_partial_2r", "trail_mode": "structure", "trail_arm": "after_partial", "partial_at_r": 2.0, "chandelier_atr_mult": 2.0, "enforce_min_reward_risk": True},
    {"name": "ema_immediate_1r", "trail_mode": "ema", "trail_arm": "immediate", "partial_at_r": 1.0, "chandelier_atr_mult": 2.0, "enforce_min_reward_risk": False},
    {"name": "ema_after_partial_1r", "trail_mode": "ema", "trail_arm": "after_partial", "partial_at_r": 1.0, "chandelier_atr_mult": 2.0, "enforce_min_reward_risk": False},
    {"name": "ema_after_1r_1r", "trail_mode": "ema", "trail_arm": "after_1r", "partial_at_r": 1.0, "chandelier_atr_mult": 2.0, "enforce_min_reward_risk": False},
    {"name": "chandelier_2_after_1r_1r", "trail_mode": "chandelier", "trail_arm": "after_1r", "partial_at_r": 1.0, "chandelier_atr_mult": 2.0, "enforce_min_reward_risk": False},
    {"name": "chandelier_3_after_1r_1r", "trail_mode": "chandelier", "trail_arm": "after_1r", "partial_at_r": 1.0, "chandelier_atr_mult": 3.0, "enforce_min_reward_risk": False},
    {"name": "structure_after_partial_1r", "trail_mode": "structure", "trail_arm": "after_partial", "partial_at_r": 1.0, "chandelier_atr_mult": 2.0, "enforce_min_reward_risk": False},
)

SETUP_B_GRID = (
    {"name": "B0_baseline", "min_ema_gap_atr": 0.0, "allow_caution": True, "stop_swing_bars": 1, "require_fast_ema_tag": False},
    {"name": "B1_ema_gap_0.5", "min_ema_gap_atr": 0.5, "allow_caution": True, "stop_swing_bars": 1, "require_fast_ema_tag": False},
    {"name": "B2_no_caution", "min_ema_gap_atr": 0.0, "allow_caution": False, "stop_swing_bars": 1, "require_fast_ema_tag": False},
    {"name": "B3_swing_stop_3", "min_ema_gap_atr": 0.0, "allow_caution": True, "stop_swing_bars": 3, "require_fast_ema_tag": False},
    {"name": "B4_fast_ema_tag", "min_ema_gap_atr": 0.0, "allow_caution": True, "stop_swing_bars": 1, "require_fast_ema_tag": True},
)

EXIT_MIN_IS_TRADES = 15
PULLBACK_MIN_IS_TRADES = 10
OOS_MIN_TRADES = 30


def baseline_scan_settings(settings: dict) -> dict:
    """Copy settings with Setup B's research switches forced back to v0.3."""
    import copy

    out = copy.deepcopy(settings)
    out["setups"][SETUP_PULLBACK].update(BASELINE_PULLBACK)
    out["exits"]["trail_mode"] = "ema"
    out["exits"]["trail_arm"] = "immediate"
    out["exits"]["partial_at_r"] = 2.0
    out["risk"]["enforce_min_reward_risk"] = True
    return out


def settings_for_exit(settings: dict, spec: dict) -> dict:
    import copy

    out = copy.deepcopy(settings)
    out["exits"]["trail_mode"] = spec["trail_mode"]
    out["exits"]["trail_arm"] = spec["trail_arm"]
    out["exits"]["partial_at_r"] = float(spec["partial_at_r"])
    out["exits"]["chandelier_atr_mult"] = float(spec["chandelier_atr_mult"])
    out["risk"]["enforce_min_reward_risk"] = bool(spec["enforce_min_reward_risk"])
    out["setups"][SETUP_PULLBACK].update(BASELINE_PULLBACK)
    return out


def settings_for_pullback(settings: dict, spec: dict) -> dict:
    import copy

    out = copy.deepcopy(settings_for_exit(settings, EXIT_GRID[0]))
    out["setups"][SETUP_PULLBACK].update(
        {
            "min_ema_gap_atr": spec["min_ema_gap_atr"],
            "allow_caution": spec["allow_caution"],
            "stop_swing_bars": spec["stop_swing_bars"],
            "require_fast_ema_tag": spec["require_fast_ema_tag"],
        }
    )
    return out


def _asof(times: np.ndarray, ts: int) -> int:
    if times is None or len(times) == 0:
        return -1
    return int(np.searchsorted(times, ts, side="right") - 1)


def _quote_volume_24h(close_times: np.ndarray, quote_vols: np.ndarray, index: int) -> float | None:
    last_close = int(close_times[index])
    left = int(np.searchsorted(close_times, last_close - 24 * HOUR_MS, side="right"))
    window = quote_vols[left : index + 1]
    if len(window) < 24:
        return None
    return float(window[-24:].sum())


def _bias_value(df1d: pd.DataFrame, idx: int, direction: str, required: bool, min_bars: int, ema_col: str):
    """Same result as daily_bias_ok without copying the daily frame."""
    if not required:
        return None
    if idx < 0 or idx + 1 < min_bars or ema_col not in df1d.columns:
        return False
    close = float(df1d["close"].iloc[idx])
    ema_slow = float(df1d[ema_col].iloc[idx])
    if not np.isfinite(close) or not np.isfinite(ema_slow):
        return False
    if direction == "long":
        return close > ema_slow
    return close < ema_slow


def _bias_required(setup: str, daily_count: int, settings: dict) -> tuple[bool, int]:
    if setup == SETUP_RECLAIM:
        return False, int(settings["indicators"]["sma_slow"])
    if setup == SETUP_MEME:
        need = int(settings["setups"][SETUP_MEME]["min_daily_bars_for_bias"])
        return daily_count >= need, need
    return True, int(settings["setups"][setup]["min_daily_bars"])


def _passes_static_gates(signal: dict, settings: dict) -> bool:
    min_vol, _max_spread = volume_and_spread_limits(signal["setup"], settings)
    volume = signal.get("quote_volume_24h")
    if volume is None or float(volume) < min_vol:
        return False
    if funding_blocks(signal["direction"], signal.get("funding_rate"), signal.get("funding_rate"), settings):
        return False
    onboard = signal.get("onboard_date_ms")
    if onboard is None:
        return False
    age_days = (int(signal["trigger_close_time"]) - int(onboard)) / DAY_MS
    if age_days < float(settings["universe"]["min_listing_age_days"]):
        return False
    return True


def _variant_keeps(signal: dict, settings: dict) -> bool:
    if signal["setup"] != SETUP_PULLBACK:
        return True
    cfg = settings["setups"][SETUP_PULLBACK]
    if not bool(cfg.get("allow_caution", True)) and signal.get("regime") == "caution":
        return False
    meta = signal["meta"]
    if bool(cfg.get("require_fast_ema_tag", False)) and not meta.get("tagged_fast"):
        return False
    min_gap = float(cfg.get("min_ema_gap_atr") or 0)
    if min_gap > 0:
        fast = meta.get("ema_gap_fast")
        slow = meta.get("ema_gap_slow")
        if fast is None or slow is None or float(fast) < min_gap or float(slow) < min_gap:
            return False
    return True


def _stop_for_variant(signal: dict, settings: dict) -> float:
    if signal["setup"] == SETUP_PULLBACK and int(settings["setups"][SETUP_PULLBACK].get("stop_swing_bars", 1)) >= 3:
        return float(signal["meta"]["stop_3bar"])
    return float(signal["stop"])


def _book_from_frames(df1h: pd.DataFrame, df4h: pd.DataFrame, df1d: pd.DataFrame, funding: pd.DataFrame | None, meta: dict, settings: dict) -> dict:
    fast = ema_column(settings, "fast")
    mid = ema_column(settings, "mid")
    slow = ema_column(settings, "slow")
    fund_time = np.array([], dtype=np.int64)
    fund_rate = np.array([], dtype=float)
    if funding is not None and not funding.empty:
        fund_time = funding["funding_time"].to_numpy(dtype=np.int64)
        fund_rate = funding["funding_rate"].to_numpy(dtype=float)
    return {
        "open_time": df1h["open_time"].to_numpy(dtype=np.int64),
        "open": df1h["open"].to_numpy(dtype=float),
        "high": df1h["high"].to_numpy(dtype=float),
        "low": df1h["low"].to_numpy(dtype=float),
        "close": df1h["close"].to_numpy(dtype=float),
        "close_time": df1h["close_time"].to_numpy(dtype=np.int64),
        "h4_open": df4h["open_time"].to_numpy(dtype=np.int64),
        "h4_high": df4h["high"].to_numpy(dtype=float),
        "h4_low": df4h["low"].to_numpy(dtype=float),
        "h4_close": df4h["close"].to_numpy(dtype=float),
        "h4_atr": df4h["atr_14"].to_numpy(dtype=float),
        "h4_ema_fast": df4h[fast].to_numpy(dtype=float),
        "h4_ema_mid": df4h[mid].to_numpy(dtype=float),
        "h4_ema_50": df4h["ema_50"].to_numpy(dtype=float) if "ema_50" in df4h.columns else df4h[mid].to_numpy(dtype=float),
        "h4_bb_upper": df4h["bb_upper"].to_numpy(dtype=float),
        "h4_bb_lower": df4h["bb_lower"].to_numpy(dtype=float),
        "h4_bb_mid": df4h["bb_mid"].to_numpy(dtype=float),
        "d_open": df1d["open_time"].to_numpy(dtype=np.int64),
        "d_close": df1d["close"].to_numpy(dtype=float),
        "d_ema_slow": df1d[slow].to_numpy(dtype=float),
        "d_close_time": df1d["close_time"].to_numpy(dtype=np.int64),
        "fund_time": fund_time,
        "fund_rate": fund_rate,
        "meta": dict(meta),
        "fast_name": fast,
        "mid_name": mid,
        "slow_name": slow,
    }


def _frame_span(df1h: pd.DataFrame) -> tuple[int, int] | None:
    if df1h is None or df1h.empty:
        return None
    return int(df1h["open_time"].iloc[0]), int(df1h["close_time"].iloc[-1])


def scan_frames(symbol: str, df1h: pd.DataFrame, df4h: pd.DataFrame, df1d: pd.DataFrame, btc_daily: pd.DataFrame, funding: pd.DataFrame | None, meta: dict, settings: dict) -> dict:
    """Return baseline signals for one symbol. Frames may be raw klines."""
    settings = baseline_scan_settings(settings)
    empty = {"symbol": symbol, "signals": [], "book": None, "span": _frame_span(df1h), "meme_chart_hits": 0, "signals_emitted": 0, "catalog_status": (meta or {}).get("status")}
    if df1h is None or df4h is None or df1d is None or df1h.empty or df4h.empty or df1d.empty:
        return empty
    df1h = add_indicators(df1h.sort_values("open_time").reset_index(drop=True), settings)
    df4h = add_indicators(df4h.sort_values("open_time").reset_index(drop=True), settings)
    df1d = add_indicators(df1d.sort_values("open_time").reset_index(drop=True), settings)
    btc = add_indicators(btc_daily.sort_values("open_time").reset_index(drop=True), settings) if btc_daily is not None and not btc_daily.empty else None
    meta = dict(meta or {})
    meta.setdefault("contract_type", "PERPETUAL")
    meta.setdefault("quote_asset", "USDT")
    close_1h = df1h["close_time"].to_numpy(dtype=np.int64)
    quote = df1h["quote_volume"].to_numpy(dtype=float)
    open_1h = df1h["open_time"].to_numpy(dtype=np.int64)
    h4_close_time = df4h["close_time"].to_numpy(dtype=np.int64)
    h4_atr = df4h["atr_14"].to_numpy(dtype=float)
    btc_close = np.array([], dtype=np.int64) if btc is None else btc["close_time"].to_numpy(dtype=np.int64)
    daily_close = df1d["close_time"].to_numpy(dtype=np.int64)
    fund_time = np.array([], dtype=np.int64)
    fund_rate = np.array([], dtype=float)
    if funding is not None and not funding.empty:
        ordered = funding.sort_values("funding_time")
        fund_time = ordered["funding_time"].to_numpy(dtype=np.int64)
        fund_rate = ordered["funding_rate"].to_numpy(dtype=float)
    min_regime = int(settings["indicators"]["sma_slow"])
    signals = []
    meme_hits = 0
    for j in range(len(df4h)):
        found = detect_setups_on_bar(symbol, df4h, j, None, None, settings, chart_only=True)
        for candidate in found:
            if candidate.setup == SETUP_MEME:
                meme_hits += 1
                continue
            trigger = first_trigger(candidate, df1h, df4h, settings)
            if trigger is None:
                continue
            trigger_open = int(trigger["open_time"])
            t_idx = int(np.searchsorted(open_1h, trigger_open, side="left"))
            if t_idx >= len(open_1h) or int(open_1h[t_idx]) != trigger_open:
                continue
            now_ms = int(trigger["close_time"])
            d_idx = _asof(daily_close, now_ms)
            daily_count = 0 if d_idx < 0 else d_idx + 1
            if candidate.setup in (SETUP_SQUEEZE, SETUP_PULLBACK):
                if daily_count < int(settings["setups"][candidate.setup]["min_daily_bars"]):
                    continue
            b_idx = _asof(btc_close, now_ms)
            regime = None
            if btc is not None and b_idx >= min_regime - 1:
                brow = btc.iloc[b_idx]
                regime = classify_regime(float(brow["close"]), float(brow["sma_fast"]), float(brow["sma_slow"]))
            if not direction_allowed(regime, candidate.setup, candidate.direction):
                continue
            bias_required, bias_bars = _bias_required(candidate.setup, daily_count, settings)
            bias = _bias_value(df1d, d_idx, candidate.direction, bias_required, bias_bars, ema_column(settings, "slow"))
            if bias is False:
                continue
            if not reclaim_volume_ok(candidate, trigger, df4h):
                continue
            entry = float(trigger["close"])
            atr = candidate.atr
            h4_idx = _asof(h4_close_time, now_ms)
            if h4_idx >= 0 and np.isfinite(h4_atr[h4_idx]):
                atr = float(h4_atr[h4_idx])
            candidate.atr = atr
            stop = stop_for_trigger(candidate, trigger, entry, atr, settings)
            if stop is None:
                continue
            f_idx = _asof(fund_time, now_ms)
            rate = None if f_idx < 0 else float(fund_rate[f_idx])
            volume = _quote_volume_24h(close_1h, quote, t_idx)
            signal = {
                "symbol": symbol,
                "setup": candidate.setup,
                "direction": candidate.direction,
                "setup_open_time": int(candidate.setup_open_time),
                "trigger_open_time": trigger_open,
                "trigger_close_time": now_ms,
                "entry_index": t_idx,
                "entry": entry,
                "stop": float(stop),
                "atr": float(atr),
                "time_stop_bars": int(candidate.time_stop_bars),
                "hard_max_bars": candidate.hard_max_bars,
                "meta": dict(candidate.meta),
                "quote_volume_24h": volume,
                "last_1h_quote_volume": float(trigger["quote_volume"]) if pd.notna(trigger["quote_volume"]) else None,
                "funding_rate": rate,
                "onboard_date_ms": meta.get("onboard_date_ms"),
                "base_asset": meta.get("base_asset"),
                "regime": regime,
                "bias_ok": bias,
                "catalog_status": meta.get("status"),
                "sort_key": (
                    now_ms,
                    symbol,
                    int(candidate.setup_open_time),
                    SETUP_RANK[candidate.setup],
                    0 if candidate.direction == "long" else 1,
                ),
            }
            signal["meta"]["regime"] = regime
            if _passes_static_gates(signal, settings):
                signals.append(signal)
    book = _book_from_frames(df1h, df4h, df1d, funding, meta, settings) if signals else None
    return {
        "symbol": symbol,
        "signals": signals,
        "book": book,
        "span": _frame_span(df1h),
        "meme_chart_hits": meme_hits,
        "signals_emitted": len(signals),
        "catalog_status": meta.get("status"),
    }


def _bar_series(book: dict, idx: int) -> pd.Series:
    return pd.Series(
        {
            "open": float(book["open"][idx]),
            "high": float(book["high"][idx]),
            "low": float(book["low"][idx]),
            "close": float(book["close"][idx]),
            "close_time": int(book["close_time"][idx]),
            "open_time": int(book["open_time"][idx]),
        }
    )


def _row4h(book: dict, h4_open: int) -> tuple[pd.Series | None, list[float] | None, list[float] | None]:
    times = book["h4_open"]
    idx = int(np.searchsorted(times, h4_open, side="left"))
    if idx >= len(times) or int(times[idx]) != h4_open:
        return None, None, None
    row = pd.Series(
        {
            "open_time": int(times[idx]),
            "high": float(book["h4_high"][idx]),
            "low": float(book["h4_low"][idx]),
            "close": float(book["h4_close"][idx]),
            "atr_14": float(book["h4_atr"][idx]),
            book["fast_name"]: float(book["h4_ema_fast"][idx]),
            book["mid_name"]: float(book["h4_ema_mid"][idx]),
            "ema_50": float(book["h4_ema_50"][idx]),
            "bb_upper": float(book["h4_bb_upper"][idx]),
            "bb_lower": float(book["h4_bb_lower"][idx]),
            "bb_mid": float(book["h4_bb_mid"][idx]),
        }
    )
    left = max(0, idx + 1 - 3)
    highs, lows = closed_structure_window(
        book["h4_high"][left : idx + 1].tolist(),
        book["h4_low"][left : idx + 1].tolist(),
        idx - left,
        3,
    )
    return row, highs, lows


def _row1d(book: dict, day_open: int) -> pd.Series | None:
    times = book["d_open"]
    idx = int(np.searchsorted(times, day_open, side="left"))
    if idx >= len(times) or int(times[idx]) != day_open:
        return None
    return pd.Series(
        {
            "open_time": int(times[idx]),
            "close": float(book["d_close"][idx]),
            book["slow_name"]: float(book["d_ema_slow"][idx]),
            "close_time": int(book["d_close_time"][idx]),
        }
    )


def _funding_frame(book: dict) -> pd.DataFrame | None:
    if len(book["fund_time"]) == 0:
        return None
    return pd.DataFrame({"funding_time": book["fund_time"], "funding_rate": book["fund_rate"]})


class _RangeView:
    def __init__(self, setup: str, direction: str, meta: dict, level: float):
        self.setup = setup
        self.direction = direction
        self.meta = meta
        self.level = level


def _try_open(signal: dict, idx: int, equity: float, positions: dict, settings: dict, cooldown_until: int | None, daily_pnl, weekly_pnl, open_view: list[dict]) -> tuple[object | None, str | None]:
    if not _variant_keeps(signal, settings):
        return None, None
    if signal["symbol"] in positions:
        return None, "conflicting_position"
    stop = _stop_for_variant(signal, settings)
    meta = dict(signal["meta"])
    view = _RangeView(signal["setup"], signal["direction"], meta, float(meta.get("level", signal["entry"])))
    if not range_room_ok(view, float(signal["entry"]), stop, settings):
        return None, "range_room"
    market = {
        "symbol": signal["symbol"],
        "status": _historical_status(
            {
                "status": signal.get("catalog_status"),
                "contract_type": "PERPETUAL",
                "quote_asset": "USDT",
            }
        ),
        "contract_type": "PERPETUAL",
        "quote_asset": "USDT",
        "base_asset": signal.get("base_asset"),
        "onboard_date_ms": signal.get("onboard_date_ms"),
        "quote_volume_24h": signal.get("quote_volume_24h"),
        "funding_rate": signal.get("funding_rate"),
        "predicted_funding_rate": signal.get("funding_rate") if settings["backtest"].get("funding_predicted_model") == "persist_last_settled" else None,
        "funding_predicted_source": "persist_last_settled" if settings["backtest"].get("funding_predicted_model") == "persist_last_settled" else "missing",
        "last_1h_quote_volume": signal.get("last_1h_quote_volume"),
        "bid": None,
        "ask": None,
    }
    spread, source = resolve_spread(market, settings, historical=True)
    market["spread_source"] = source
    plan = plan_position(
        equity=equity,
        entry=float(signal["entry"]),
        stop=stop,
        direction=signal["direction"],
        setup=signal["setup"],
        atr=float(signal["atr"]),
        spread_frac=0.0 if spread is None else spread,
        settings=settings,
        last_1h_quote_volume=signal.get("last_1h_quote_volume"),
    )
    if plan.notional_usdt is not None:
        market["_planned_notional"] = plan.notional_usdt
    account = {
        "equity_usdt": equity,
        "daily_pnl_pct": daily_pnl,
        "weekly_pnl_pct": weekly_pnl,
        "news_status": settings["backtest"].get("news_status"),
    }
    gates = evaluate_gates(
        setup=signal["setup"],
        direction=signal["direction"],
        symbol=signal["symbol"],
        market=market,
        account=account,
        open_positions=open_view,
        size_ok=bool(plan.ok),
        size_reason=plan.reason,
        risk_usdt=plan.risk_usdt,
        spread_frac=spread,
        now_ms=int(signal["trigger_close_time"]),
        settings=settings,
        cooldown_until_ms=cooldown_until,
        blocklist_symbols=set(settings["blocklist"].get("symbols") or []),
        blocklist_available=bool(settings["blocklist"].get("available", True)),
        onchain_raw=None,
        regime_ok=True,
        regime_name=signal.get("regime"),
        bias_ok=signal.get("bias_ok"),
        stop_ok=bool(plan.ok) or not str(plan.reason).startswith("stop is farther"),
        stop_reason="stop inside the ATR cap" if plan.ok else plan.reason,
    )
    if any(item.get("result") != "pass" for item in gates.values()):
        return None, first_failure(gates) or "rejected"
    cluster = cluster_for(signal["symbol"], signal.get("base_asset"))
    packet = {
        "symbol": signal["symbol"],
        "setup": signal["setup"],
        "direction": signal["direction"],
        "cluster_key": cluster_key(cluster, signal["direction"]),
        "signal_id": signal_id_for(
            signal["symbol"],
            signal["setup"],
            signal["direction"],
            int(signal["setup_open_time"]),
            int(signal["trigger_open_time"]),
        ),
        "ts_ms": int(signal["trigger_close_time"]),
        "entry": float(signal["entry"]),
        "stop": stop,
        "size_coins": plan.size_coins,
        "time_stop_bars": int(signal["time_stop_bars"]),
        "hard_max_bars": signal.get("hard_max_bars"),
        "meta": meta,
        "partial_fraction": float(settings["exits"]["partial_fraction"]),
        "spread_frac": 0.0 if spread is None else float(spread),
    }
    pos = _open_from_packet(packet, idx, settings)
    if pos is None:
        return None, "fill crossed the stop"
    return pos, None


def _annotate(trade: dict, pos) -> dict:
    meta = pos.meta or {}
    risk = abs(pos.fill - pos.initial_stop)
    trade["regime"] = meta.get("regime")
    trade["ema_gap_fast"] = meta.get("ema_gap_fast")
    trade["ema_gap_slow"] = meta.get("ema_gap_slow")
    trade["tagged_fast"] = meta.get("tagged_fast")
    trade["tagged_mid"] = meta.get("tagged_mid")
    trade["stop_distance_atr"] = None if not pos.meta or risk <= 0 or meta.get("atr") in (None, 0) else risk / float(meta.get("atr") or trade.get("atr") or 1)
    atr = meta.get("atr")
    if atr:
        trade["stop_distance_atr"] = risk / float(atr) if float(atr) else None
    trade["catalog_status"] = meta.get("catalog_status")
    return trade


def scan_stored_symbol(task: tuple) -> dict:
    """Process-pool entry. Opens its own SQLite connection."""
    symbol, db_path, settings = task
    from desk.data.store import Store

    store = Store(db_path)
    try:
        meta = store.symbol_row(symbol) or {
            "symbol": symbol,
            "base_asset": symbol[:-4] if symbol.endswith("USDT") else symbol,
            "status": "UNKNOWN",
            "contract_type": "PERPETUAL",
            "quote_asset": "USDT",
        }
        return scan_frames(
            symbol,
            store.klines(symbol, "1h"),
            store.klines(symbol, "4h"),
            store.klines(symbol, "1d"),
            store.klines("BTCUSDT", "1d"),
            store.funding(symbol),
            meta,
            settings,
        )
    except Exception as exc:
        return {
            "symbol": symbol,
            "signals": [],
            "book": None,
            "span": None,
            "meme_chart_hits": 0,
            "signals_emitted": 0,
            "catalog_status": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        store.close()


def simulate(signals: list[dict], books: dict[str, dict], settings: dict) -> dict:
    """Walk the union of hourly bars. Only symbols with a baseline signal need books."""
    ordered = sorted(signals, key=lambda item: item["sort_key"])
    by_time: dict[int, list[dict]] = defaultdict(list)
    for signal in ordered:
        by_time[int(signal["trigger_open_time"])].append(signal)
    if books:
        timeline = np.unique(np.concatenate([book["open_time"] for book in books.values()]))
    else:
        timeline = np.array([], dtype=np.int64)
    start_equity = float(settings["backtest"]["initial_equity"])
    positions: dict = {}
    trades: list[dict] = []
    rejects: Counter = Counter()
    cooldowns: dict[str, int] = {}
    equity_points: list[tuple[int, float]] = []
    day_key = None
    week_key = None
    day_start = start_equity
    week_start = start_equity
    prev_close = 0
    ema_fast = ema_column(settings, "fast")
    ema_slow = ema_column(settings, "slow")
    funding_cache = {symbol: _funding_frame(book) for symbol, book in books.items()}
    lookback = int(settings["exits"].get("structure_lookback", 3))

    def marked_equity(mark_prices: dict[str, float]) -> float:
        realized = sum(trade["pnl"] for trade in trades) + sum(pos.realized for pos in positions.values())
        unrealized = 0.0
        for sym, pos in positions.items():
            mark = mark_prices.get(sym, pos.fill)
            if pos.direction == "long":
                unrealized += (mark - pos.fill) * pos.remaining
            else:
                unrealized += (pos.fill - mark) * pos.remaining
        return start_equity + realized + unrealized

    for open_time in timeline:
        open_time = int(open_time)
        slot = by_time.get(open_time)
        if not positions and not slot:
            continue
        mark_prices = {}
        any_close = open_time + HOUR_MS - 1
        for symbol, pos in list(positions.items()):
            book = books[symbol]
            idx = int(np.searchsorted(book["open_time"], open_time, side="left"))
            if idx >= len(book["open_time"]) or int(book["open_time"][idx]) != open_time:
                if open_time > int(book["open_time"][-1]) and pos.entry_index < len(book["open_time"]):
                    last = len(book["open_time"]) - 1
                    price_book = book
                    slip = _slip(settings, pos.spread_frac)
                    price = adverse_price(float(price_book["close"][last]), pos.direction, is_entry=False, slip=slip)
                    _close_qty(pos, pos.remaining, price, int(price_book["close_time"][last]), float(settings["costs"]["taker_fee_rate"]))
                    trade = _annotate(_finished_trade(pos, int(price_book["close_time"][last]), "no_more_bars"), pos)
                    trade["atr"] = pos.meta.get("atr")
                    trades.append(trade)
                    del positions[symbol]
                continue
            if pos.entry_index >= idx:
                continue
            bar = _bar_series(book, idx)
            mark_prices[symbol] = float(book["close"][idx])
            any_close = int(book["close_time"][idx])
            row4h = None
            recent_highs = None
            recent_lows = None
            row1d = None
            if open_time % H4_MS == 3 * HOUR_MS:
                row4h, recent_highs, recent_lows = _row4h(book, open_time - 3 * HOUR_MS)
                if str(settings["exits"].get("trail_mode")) == "structure" and row4h is not None:
                    h_idx = int(np.searchsorted(book["h4_open"], open_time - 3 * HOUR_MS, side="left"))
                    left = max(0, h_idx + 1 - lookback)
                    recent_highs, recent_lows = closed_structure_window(
                        book["h4_high"][left : h_idx + 1].tolist(),
                        book["h4_low"][left : h_idx + 1].tolist(),
                        h_idx - left,
                        lookback,
                    )
            if open_time % DAY_MS == 23 * HOUR_MS:
                row1d = _row1d(book, open_time - 23 * HOUR_MS)
            reason = manage_bar(pos, bar, row4h, row1d, settings, ema_fast, ema_slow, recent_highs, recent_lows)
            if reason is None and pos.remaining > 1e-12:
                _apply_due_funding(pos, funding_cache.get(symbol), prev_close, int(bar["close_time"]), float(bar["close"]))
            if reason is not None or pos.remaining <= 1e-12:
                trade = _annotate(_finished_trade(pos, int(bar["close_time"]), reason or "flat"), pos)
                trades.append(trade)
                if reason == "hard_stop":
                    cooldowns[symbol] = int(bar["close_time"]) + int(settings["risk"]["cooldown_days"]) * DAY_MS
                del positions[symbol]
        equity = marked_equity(mark_prices)
        equity_points.append((any_close, equity))
        this_day = myt_date(any_close)
        this_week = _week_key(any_close)
        if day_key is None:
            day_key = this_day
            day_start = start_equity
        elif this_day != day_key:
            day_key = this_day
            day_start = equity
        if week_key is None:
            week_key = this_week
            week_start = start_equity
        elif this_week != week_key:
            week_key = this_week
            week_start = equity
        daily = None if day_start <= 0 else (equity - day_start) / day_start
        weekly = None if week_start <= 0 else (equity - week_start) / week_start
        if not slot:
            prev_close = any_close
            continue
        open_view = [
            {
                "symbol": pos.symbol,
                "setup": pos.setup,
                "direction": pos.direction,
                "cluster_key": pos.cluster_key,
                "risk_usdt": _live_risk(pos),
            }
            for pos in positions.values()
        ]
        for signal in slot:
            if signal["symbol"] not in books:
                continue
            idx = int(signal["entry_index"])
            pos, reason = _try_open(
                signal,
                idx,
                equity,
                positions,
                settings,
                cooldowns.get(signal["symbol"]),
                daily,
                weekly,
                open_view,
            )
            if reason:
                rejects[reason] += 1
            if pos is None:
                continue
            pos.meta["atr"] = signal["atr"]
            pos.meta["regime"] = signal.get("regime")
            pos.meta["catalog_status"] = signal.get("catalog_status")
            positions[pos.symbol] = pos
            open_view.append(
                {
                    "symbol": pos.symbol,
                    "setup": pos.setup,
                    "direction": pos.direction,
                    "cluster_key": pos.cluster_key,
                    "risk_usdt": _live_risk(pos),
                }
            )
        prev_close = any_close

    if settings["backtest"].get("close_at_end", True):
        for symbol, pos in list(positions.items()):
            book = books[symbol]
            last = len(book["open_time"]) - 1
            slip = _slip(settings, pos.spread_frac)
            price = adverse_price(float(book["close"][last]), pos.direction, is_entry=False, slip=slip)
            _close_qty(pos, pos.remaining, price, int(book["close_time"][last]), float(settings["costs"]["taker_fee_rate"]))
            trade = _annotate(_finished_trade(pos, int(book["close_time"][last]), "end_of_data"), pos)
            trades.append(trade)
            equity_points.append((int(book["close_time"][last]), start_equity + sum(item["pnl"] for item in trades)))
            del positions[symbol]
    return {"trades": trades, "equity": equity_points, "rejects": dict(rejects)}
