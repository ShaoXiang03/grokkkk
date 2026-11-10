"""Regime, sizing, exits, universe, and on-chain gates."""

import pandas as pd
import pytest

from desk.config import load_settings
from desk.rules.exits import breakeven_stop, tighten_stop, trail_stop
from desk.rules.onchain import FAIL, MISSING, ONCHAIN_CHECKS, PASS, onchain_gate
from desk.rules.risk_gates import evaluate_gates
from desk.rules.regime import (
    CAUTION,
    LONG_FAVORED,
    SETUP_MEME,
    SETUP_PULLBACK,
    SETUP_RECLAIM,
    SETUP_SQUEEZE,
    SHORT_FAVORED,
    classify_regime,
    direction_allowed,
)
from desk.rules.sizing import liquidation_price, plan_position, raw_size
from desk.rules.universe import cluster_for, funding_blocks


def test_regime_boundaries_and_permissions():
    assert classify_regime(110, 100, 90) == LONG_FAVORED
    assert classify_regime(95, 100, 90) == CAUTION
    assert classify_regime(100, 100, 90) == CAUTION
    assert classify_regime(90, 80, 90) == SHORT_FAVORED
    assert classify_regime(90, 100, 100) == SHORT_FAVORED

    assert direction_allowed(LONG_FAVORED, SETUP_SQUEEZE, "long")
    assert not direction_allowed(LONG_FAVORED, SETUP_SQUEEZE, "short")
    assert direction_allowed(LONG_FAVORED, SETUP_RECLAIM, "short")
    assert not direction_allowed(CAUTION, SETUP_SQUEEZE, "long")
    assert direction_allowed(CAUTION, SETUP_PULLBACK, "short")
    assert direction_allowed(CAUTION, SETUP_MEME, "long")
    assert direction_allowed(SHORT_FAVORED, SETUP_SQUEEZE, "short")
    assert not direction_allowed(SHORT_FAVORED, SETUP_PULLBACK, "long")
    assert direction_allowed(SHORT_FAVORED, SETUP_RECLAIM, "long")
    assert not direction_allowed(None, SETUP_RECLAIM, "long")


def test_formula_matches_worked_example():
    # Equity 800, 2% risk, scale 1, entry 2.00, stop 1.90 -> 160 coins.
    assert raw_size(800, 0.02, 1.0, 2.0, 1.90) == pytest.approx(160)
    settings = load_settings()
    plan = plan_position(
        equity=800,
        entry=2.0,
        stop=1.90,
        direction="long",
        setup="trend_pullback",
        atr=0.05,
        spread_frac=0.0002,
        settings=settings,
    )
    assert plan.ok
    assert plan.size_coins == pytest.approx(160)
    assert plan.notional_usdt == pytest.approx(320)
    assert plan.risk_usdt == pytest.approx(16)
    assert plan.leverage == 3
    # Dollar risk does not change if the liquidation check forces a lower cap.
    tight = load_settings(overrides={"setups": {"trend_pullback": {"max_leverage": 1}}})
    capped = plan_position(
        equity=800,
        entry=2.0,
        stop=1.90,
        direction="long",
        setup="trend_pullback",
        atr=0.05,
        spread_frac=0.0002,
        settings=tight,
    )
    assert capped.leverage == 1
    assert capped.risk_usdt == pytest.approx(plan.risk_usdt)


def test_leverage_is_cut_when_liquidation_is_inside_the_buffer():
    # 3x liq on a $100 entry is about 67.07. A stop at 90 with ATR 25 needs liq <= 65.
    settings = load_settings()
    plan = plan_position(
        equity=800,
        entry=100,
        stop=90,
        direction="long",
        setup="squeeze_breakout",
        atr=25,
        spread_frac=0.0001,
        settings=settings,
    )
    assert plan.ok
    assert plan.leverage == 2
    assert plan.liq_price == pytest.approx(liquidation_price(100, 2, 0.004, "long"))
    assert plan.risk_usdt == pytest.approx(16)


def test_notional_cap_skips_when_it_starves_risk():
    settings = load_settings()
    plan = plan_position(
        equity=800,
        entry=100,
        stop=99.9,
        direction="long",
        setup="squeeze_breakout",
        atr=1,
        spread_frac=0.0001,
        settings=settings,
    )
    assert not plan.ok
    assert "notional cap" in plan.reason


def test_one_x_liquidation_can_still_fail():
    # Stop distance stays inside 3x ATR, but even 1x liquidation sits above stop - ATR.
    settings = load_settings()
    plan = plan_position(
        equity=800,
        entry=1.0,
        stop=0.2,
        direction="long",
        setup="squeeze_breakout",
        atr=0.3,
        spread_frac=0.0001,
        settings=settings,
    )
    assert not plan.ok
    assert "liquidation" in plan.reason


def test_trail_and_breakeven_never_widen_the_stop():
    stop = 100.0
    widened = trail_stop(stop, "long", ema_20=90, atr=10, close=95, ema_mult=0.25, close_mult=1.5)
    assert widened == 100
    tightened = trail_stop(widened, "long", ema_20=110, atr=2, close=120, ema_mult=0.25, close_mult=1.5)
    assert tightened == pytest.approx(117)
    again = trail_stop(tightened, "long", ema_20=90, atr=10, close=100, ema_mult=0.25, close_mult=1.5)
    assert again == tightened
    assert tighten_stop(tightened, 90, "long") == tightened
    assert breakeven_stop(90, 100, "long") == 100
    short = trail_stop(110, "short", ema_20=90, atr=2, close=80, ema_mult=0.25, close_mult=1.5)
    assert short < 110
    assert trail_stop(short, "short", ema_20=130, atr=2, close=140, ema_mult=0.25, close_mult=1.5) == short


def test_setup_d_gate_fails_closed_on_each_missing_check():
    settings = load_settings()
    market = {
        "status": "TRADING",
        "contract_type": "PERPETUAL",
        "quote_asset": "USDT",
        "base_asset": "PEPE",
        "onboard_date_ms": 1,
        "quote_volume_24h": 2_000_000,
        "funding_rate": 0.0,
        "predicted_funding_rate": 0.0,
        "_planned_notional": 100,
        "last_1h_quote_volume": 1_000_000,
    }
    gates = evaluate_gates(
        setup="meme_momentum",
        direction="long",
        symbol="PEPEUSDT",
        market=market,
        account={"equity_usdt": 800, "daily_pnl_pct": 0, "weekly_pnl_pct": 0, "news_status": "ALLOW"},
        open_positions=[],
        size_ok=True,
        size_reason="ok",
        risk_usdt=8,
        spread_frac=0.001,
        now_ms=40 * 86_400_000,
        settings=settings,
        cooldown_until_ms=None,
        blocklist_symbols=set(),
        blocklist_available=True,
        onchain_raw=None,
        regime_ok=True,
        regime_name="long_favored",
        bias_ok=None,
        stop_ok=True,
        stop_reason="ok",
    )
    assert gates["on_chain"]["result"] == "fail"
    assert set(gates["on_chain"]["checks"]) == set(ONCHAIN_CHECKS)
    assert set(gates["on_chain"]["checks"].values()) == {MISSING}


def test_onchain_missing_or_fail_is_closed():
    missing = onchain_gate(None)
    assert not missing["pass"]
    assert set(missing["checks"].values()) == {MISSING}
    failed = onchain_gate({"top10_holders_le_25": FAIL})
    assert not failed["pass"]
    assert failed["checks"]["top10_holders_le_25"] == FAIL
    passed = onchain_gate({name: PASS for name in missing["checks"]})
    assert passed["pass"]


def test_cluster_and_funding_thresholds():
    assert cluster_for("BTCUSDT", "BTC") == "btc"
    assert cluster_for("ETHUSDT", "ETH") == "eth"
    assert cluster_for("ETHFIUSDT", "ETHFI") == "alts"
    settings = load_settings()
    assert funding_blocks("long", 0.0005, 0.0005, settings) is None
    assert funding_blocks("long", 0.00051, 0.0, settings)
    assert funding_blocks("short", -0.00051, 0.0, settings)
    assert funding_blocks("long", None, 0.0, settings)


def test_partial_r_changes_the_target():
    from desk.backtest.costs import tp1_price

    far = tp1_price(100, 95, "long", 2.0, 0.0005, 0.0005, True)
    near = tp1_price(100, 95, "long", 1.0, 0.0005, 0.0005, True)
    assert far > near > 100
    assert near - 100 < far - 100
