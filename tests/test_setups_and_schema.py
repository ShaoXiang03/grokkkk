"""Setup conditions on synthetic bars, and the signal schema after the Market Bot change."""

import numpy as np
import pandas as pd

from desk.config import load_settings
from desk.rules.regime import LONG_FAVORED
from desk.rules.setups import meme_at, meme_stop, pullback_at, reclaim_at, squeeze_at
from desk.rules.risk_gates import evaluate_gates, first_failure
from desk.scanner.signals import empty_market_context, empty_master_decision, validate_packet
from desk.timeutils import HOUR_MS

H4 = 4 * HOUR_MS


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_squeeze_requires_prior_bandwidth_low_and_a_close_outside():
    settings = load_settings(overrides={"setups": {"squeeze_breakout": {"bandwidth_lookback": 5, "volume_mult": 1.5}}})
    rows = []
    start = 1_700_000_000_000
    for i in range(12):
        rows.append(
            {
                "open_time": start + i * H4,
                "close_time": start + (i + 1) * H4 - 1,
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 100,
                "bandwidth": 0.2,
                "bb_upper": 102,
                "bb_lower": 98,
                "bb_mid": 100,
                "atr_14": 1.0,
                "vol_avg_20": 100,
            }
        )
    rows[-2]["bandwidth"] = 0.05
    # Prior 5 bandwidths before the measurement bar are higher, so this is a squeeze.
    for offset in range(3, 8):
        rows[-offset]["bandwidth"] = 0.2
    breakout = rows[-1]
    breakout["close"] = 103
    breakout["high"] = 104
    breakout["low"] = 100
    breakout["volume"] = 200
    breakout["bb_upper"] = 102
    frame = _frame(rows)
    assert squeeze_at(frame, len(frame) - 1, "long", settings) is not None
    quiet = frame.copy()
    quiet.loc[quiet.index[-1], "close"] = 101
    assert squeeze_at(quiet, len(quiet) - 1, "long", settings) is None
    thin = frame.copy()
    thin.loc[thin.index[-1], "volume"] = 100
    assert squeeze_at(thin, len(thin) - 1, "long", settings) is None


def test_pullback_tag_and_rsi_band():
    settings = load_settings()
    start = 1_700_000_000_000
    row = {
        "open_time": start,
        "close_time": start + H4 - 1,
        "open": 101,
        "high": 102,
        "low": 99,
        "close": 101,
        "volume": 10,
        "ema_20": 100,
        "ema_50": 95,
        "ema_200": 90,
        "rsi_14": 48,
        "atr_14": 2,
        "vol_avg_20": 10,
    }
    lead = []
    for i in range(3):
        blank = dict(row)
        blank["open_time"] = start + i * H4
        blank["close_time"] = blank["open_time"] + H4 - 1
        blank["low"] = 101
        blank["rsi_14"] = 70
        lead.append(blank)
    lead.append(row | {"open_time": start + 3 * H4, "close_time": start + 4 * H4 - 1})
    frame = _frame(lead)
    hit = pullback_at(frame, len(frame) - 1, "long", settings)
    assert hit is not None
    assert hit["provisional_stop"] < row["low"]
    too_hot = frame.copy()
    too_hot.loc[too_hot.index[-1], "rsi_14"] = 70
    assert pullback_at(too_hot, len(too_hot) - 1, "long", settings) is None


def test_reclaim_fakeout_then_close_back_inside():
    settings = load_settings(
        overrides={"setups": {"range_reclaim": {"range_bars": 8, "fakeout_lookback": 4, "range_atr_mult": 1.5}}}
    )
    start = 1_700_000_000_000
    rows = []
    for i in range(8):
        rows.append(
            {
                "open_time": start + i * H4,
                "close_time": start + (i + 1) * H4 - 1,
                "open": 100,
                "high": 110,
                "low": 100,
                "close": 105,
                "volume": 100,
                "atr_14": 2,
                "vol_avg_20": 100,
            }
        )
    fake = {
        "open_time": start + 8 * H4,
        "close_time": start + 9 * H4 - 1,
        "open": 102,
        "high": 106,
        "low": 97,
        "close": 101,
        "volume": 100,
        "atr_14": 2,
        "vol_avg_20": 100,
    }
    reclaim = {
        "open_time": start + 9 * H4,
        "close_time": start + 10 * H4 - 1,
        "open": 101,
        "high": 106,
        "low": 99,
        "close": 104,
        "volume": 180,
        "atr_14": 2,
        "vol_avg_20": 100,
    }
    frame = _frame(rows + [fake, reclaim])
    hit = reclaim_at(frame, len(frame) - 1, "long", settings)
    assert hit is not None
    assert hit["level"] == 100
    assert hit["meta"]["range_high"] == 110


def test_meme_breakout_and_tighter_stop():
    settings = load_settings(overrides={"setups": {"meme_momentum": {"breakout_lookback": 5, "volume_mult": 2}}})
    start = 1_700_000_000_000
    rows = []
    for i in range(6):
        rows.append(
            {
                "open_time": start + i * H4,
                "close_time": start + (i + 1) * H4 - 1,
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
                "volume": 100 if i < 5 else 400,
                "atr_14": 1,
                "vol_avg_20": 100,
            }
        )
    rows[-1]["close"] = 12
    rows[-1]["high"] = 12
    rows[-1]["low"] = 10.5
    frame = _frame(rows)
    hit = meme_at(frame, len(frame) - 1, "long", settings)
    assert hit is not None
    assert hit["level"] == 11
    # Entry is between 1.25 and 1.5 ATR above the signal low, so the ATR cap is the tighter stop.
    stop = meme_stop("long", entry=113, signal_extreme=100, atr=10, buffer=0.25, cap_mult=1.5)
    assert stop == 98
    # The cap sits inside the signal bar, so the structural stop is used.
    assert meme_stop("long", entry=110, signal_extreme=100, atr=2, buffer=0.25, cap_mult=1.5) == 99.5


def test_open_risk_and_cluster_caps():
    settings = load_settings()
    market = {
        "status": "TRADING",
        "contract_type": "PERPETUAL",
        "quote_asset": "USDT",
        "base_asset": "SOL",
        "onboard_date_ms": 1,
        "quote_volume_24h": 20_000_000,
        "funding_rate": 0.0001,
        "predicted_funding_rate": 0.0001,
    }
    account = {"equity_usdt": 800, "daily_pnl_pct": 0.0, "weekly_pnl_pct": 0.0, "news_status": "ALLOW"}
    positions = [
        {"symbol": "ADAUSDT", "setup": "trend_pullback", "cluster_key": "alts-long", "risk_usdt": 20, "direction": "long"}
    ]
    gates = evaluate_gates(
        setup="squeeze_breakout",
        direction="long",
        symbol="SOLUSDT",
        market=market,
        account=account,
        open_positions=positions,
        size_ok=True,
        size_reason="ok",
        risk_usdt=16,
        spread_frac=0.0004,
        now_ms=40 * 86_400_000,
        settings=settings,
        cooldown_until_ms=None,
        blocklist_symbols=set(),
        blocklist_available=True,
        onchain_raw=None,
        regime_ok=True,
        regime_name=LONG_FAVORED,
        bias_ok=True,
        stop_ok=True,
        stop_reason="ok",
    )
    assert gates["cluster"]["result"] == "fail"
    assert gates["open_risk"]["result"] == "pass"
    assert first_failure(gates) == "cluster"


def test_scanner_packet_shape_has_no_market_bot_vote():
    packet = {
        "signal_id": "abc",
        "ts_utc": "2026-11-10T00:00:00+00:00",
        "ts_myt": "2026-11-10T08:00:00+08:00",
        "symbol": "SOLUSDT",
        "venue": "Binance USDT-M",
        "margin_mode": "isolated",
        "setup": "squeeze_breakout",
        "setup_timeframe": "4h",
        "entry_timeframe": "1h",
        "direction": "long",
        "entry": 1,
        "stop": 0.9,
        "tp1": 1.2,
        "tp2": None,
        "atr_4h": 0.1,
        "size_coins": 1,
        "notional_usdt": 1,
        "leverage": 1,
        "risk_pct": 0.02,
        "risk_scale": 1,
        "risk_usdt": 0.1,
        "reward_risk": 2,
        "equity_usdt": 800,
        "funding_rate": 0.0001,
        "liq_price": 0.5,
        "listing_age_days": 30,
        "cluster": "alts",
        "cluster_key": "alts-long",
        "time_stop_bars": 8,
        "time_stop_at": "2026-11-11T08:00:00+00:00",
        "invalidation": "4h_close_inside_bands_within_2 OR 4h_close_below_middle_within_3",
        "checks_passed": ["product"],
        "on_blocklist": False,
        "hard_gates": {},
        "votes": {"scanner": {"result": "pass", "reason": "ok", "ts": "2026-11-10T00:00:00+00:00"}},
        "market_context": empty_market_context(),
        "decision_path": "pending_master",
        "master_decision": empty_master_decision(),
        "mode": "paper",
        "rules_followed": None,
        "status": "proposed",
        "rules_version": "0.3",
    }
    validate_packet(packet, scanner_output=True)
    packet["votes"]["market_bot"] = {"result": "pass"}
    try:
        validate_packet(packet, scanner_output=True)
    except ValueError as exc:
        assert "market_bot" in str(exc)
    else:
        raise AssertionError("market_bot vote should be rejected")
