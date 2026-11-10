"""Exit variants, Setup B filters, walk-forward selection, and vision resume rules."""

import queue
import threading
from datetime import datetime, timezone

import pandas as pd
import pytest

from desk.backtest.replay import Position, manage_bar
from desk.backtest.selection import adoption_decision, majority_variant, tally_fold_winners
from desk.config import load_settings
from desk.data.backfill_vision import _emit_months, classify_listing
from desk.data.store import Store
from desk.data.vision import is_um_usdt_perpetual_symbol
from desk.rules.exits import closed_structure_window, trail_is_armed
from desk.rules.regime import CAUTION, LONG_FAVORED
from desk.rules.setups import detect_setups_on_bar, pullback_at
from desk.timeutils import DAY_MS, HOUR_MS

H4 = 4 * HOUR_MS


def _position(**overrides) -> Position:
    base = dict(
        symbol="SOLUSDT",
        setup="trend_pullback",
        direction="long",
        cluster_key="alts-long",
        signal_id="x",
        entry_time=0,
        entry_index=0,
        fill=100.0,
        initial_stop=90.0,
        stop=90.0,
        tp1=120.0,
        size=2.0,
        remaining=2.0,
        partial_done=False,
        reached_1r=False,
        bars_4h=0,
        fees=0.0,
        funding=0.0,
        realized=0.0,
        time_stop_bars=30,
        partial_fraction=0.5,
    )
    base.update(overrides)
    return Position(**base)


def _bar(high, low, close, close_time, open_=None) -> pd.Series:
    return pd.Series(
        {
            "open": close if open_ is None else open_,
            "high": high,
            "low": low,
            "close": close,
            "close_time": close_time,
        }
    )


def _row4h(high, low, close, atr, ema) -> pd.Series:
    return pd.Series(
        {
            "high": high,
            "low": low,
            "close": close,
            "atr_14": atr,
            "ema_20": ema,
            "ema_50": 10,
            "bb_upper": close + 1,
            "bb_lower": close - 1,
            "bb_mid": close,
        }
    )


def test_chandelier_and_structure_never_widen_and_ignore_future_bars():
    settings = load_settings(
        overrides={"exits": {"trail_mode": "chandelier", "trail_arm": "immediate", "chandelier_atr_mult": 2}}
    )
    pos = _position()
    assert manage_bar(pos, _bar(110, 104, 108, 1), _row4h(110, 100, 108, 2, 100), None, settings, "ema_20", "ema_200") is None
    # 110 - 2*2 = 106. The initial stop was 90, so this only tightens.
    assert pos.stop == pytest.approx(106)
    frozen = pos.stop
    # A later bar with a lower extreme and a larger ATR would loosen the stop if it were allowed to.
    assert manage_bar(pos, _bar(109, 107, 108, 2), _row4h(100, 95, 99, 20, 90), None, settings, "ema_20", "ema_200") is None
    assert pos.stop == pytest.approx(frozen)
    # Changing a bar that has not been passed in does not move the stop.
    future = _row4h(200, 190, 198, 2, 150)
    assert pos.stop == pytest.approx(frozen)
    assert future["high"] == 200

    structure = load_settings(
        overrides={"exits": {"trail_mode": "structure", "trail_arm": "immediate", "structure_atr_buffer": 0.25, "structure_lookback": 3}}
    )
    held = _position(stop=90)
    highs = [101, 102, 103, 130]
    lows = [97, 98, 99, 80]
    window_h, window_l = closed_structure_window(highs, lows, 2, 3)
    assert window_h == [101, 102, 103]
    assert 80 not in window_l
    assert manage_bar(
        held,
        _bar(104, 100, 103, 1),
        _row4h(103, 99, 103, 2, 100),
        None,
        structure,
        "ema_20",
        "ema_200",
        window_h,
        window_l,
    ) is None
    # min low 97 - 0.25*2 = 96.5, which is above the hard stop, so the stop tightens.
    assert held.stop == pytest.approx(96.5)
    # A window whose low is under the hard stop must not widen it.
    again = manage_bar(
        held,
        _bar(104, 100, 103, 2),
        _row4h(103, 90, 100, 2, 100),
        None,
        structure,
        "ema_20",
        "ema_200",
        [100],
        [80],
    )
    assert again is None
    assert held.stop == pytest.approx(96.5)


def test_trail_arm_holds_the_hard_stop_until_the_condition():
    assert trail_is_armed("immediate", partial_done=False, reached_1r=False)
    assert not trail_is_armed("after_partial", partial_done=False, reached_1r=True)
    assert trail_is_armed("after_1r", partial_done=False, reached_1r=True)

    held = load_settings(overrides={"exits": {"trail_mode": "ema", "trail_arm": "after_partial"}})
    pos = _position()
    # EMA would propose about 119. The partial at 120 is not touched.
    assert manage_bar(pos, _bar(112, 101, 111, 1), _row4h(112, 101, 111, 1, 120), None, held, "ema_20", "ema_200") is None
    assert pos.stop == pytest.approx(90)
    assert not pos.partial_done

    armed = load_settings(overrides={"exits": {"trail_mode": "ema", "trail_arm": "after_1r", "trail_ema_atr_mult": 0.25, "trail_close_atr_mult": 1.5}})
    pos1 = _position()
    # 1R is 110. High 112 reaches it and does not reach the 120 partial. The trail may then tighten.
    assert manage_bar(pos1, _bar(112, 101, 111, 1), _row4h(112, 101, 111, 1, 120), None, armed, "ema_20", "ema_200") is None
    assert pos1.reached_1r
    assert not pos1.partial_done
    assert pos1.stop > 90


def test_pullback_variants_and_no_lookahead_swing():
    settings = load_settings()
    start = 1_700_000_000_000
    rows = []
    for i in range(4):
        rows.append(
            {
                "open_time": start + i * H4,
                "close_time": start + (i + 1) * H4 - 1,
                "open": 110,
                "high": 112,
                "low": 100 - i,
                "close": 108,
                "volume": 10,
                "ema_20": 110,
                "ema_50": 100,
                "ema_200": 90,
                "rsi_14": 48,
                "atr_14": 4,
                "vol_avg_20": 10,
            }
        )
    # Bar 3 tags both averages. The prior bar's low is deeper, so the 3-bar stop is wider.
    rows[-1]["low"] = 99
    rows[-1]["high"] = 111
    rows[-2]["low"] = 90
    frame = pd.DataFrame(rows)
    hit = pullback_at(frame, 3, "long", settings)
    assert hit is not None
    assert hit["provisional_stop"] == pytest.approx(99 - 0.25 * 4)
    assert hit["meta"]["stop_3bar"] == pytest.approx(90 - 1)
    assert hit["meta"]["tagged_fast"] is True
    # A future lower low must not change the stop at index 3.
    future = frame.copy()
    future.loc[future.index[-1], "low"] = 50
    assert pullback_at(future, 2, "long", settings)["provisional_stop"] == pullback_at(frame, 2, "long", settings)["provisional_stop"]

    wide = load_settings(overrides={"setups": {"trend_pullback": {"stop_swing_bars": 3}}})
    swung = pullback_at(frame, 3, "long", wide)
    assert swung["provisional_stop"] == pytest.approx(hit["meta"]["stop_3bar"])

    gapped = load_settings(overrides={"setups": {"trend_pullback": {"min_ema_gap_atr": 3}}})
    # (110-100)/4 = 2.5, which is below 3.
    assert pullback_at(frame, 3, "long", gapped) is None

    deep = frame.copy()
    deep.loc[deep.index[-1], "low"] = 100
    deep.loc[deep.index[-1], "high"] = 105
    # Tags ema50 at 100, not ema20 at 110.
    assert pullback_at(deep, 3, "long", settings)["meta"]["tagged_fast"] is False
    fast_only = load_settings(overrides={"setups": {"trend_pullback": {"require_fast_ema_tag": True}}})
    assert pullback_at(deep, 3, "long", fast_only) is None

    settings_off = load_settings(
        overrides={
            "setups": {
                "squeeze_breakout": {"enabled": False},
                "range_reclaim": {"enabled": False},
                "meme_momentum": {"enabled": False},
                "trend_pullback": {"min_daily_bars": 1, "allow_caution": False},
            }
        }
    )
    daily = pd.DataFrame(
        [{"open_time": start, "close_time": start + DAY_MS - 1, "close": 120, "ema_200": 80, "open": 1, "high": 1, "low": 1, "volume": 1}]
    )
    allowed = load_settings(
        overrides={
            "setups": {
                "squeeze_breakout": {"enabled": False},
                "range_reclaim": {"enabled": False},
                "meme_momentum": {"enabled": False},
                "trend_pullback": {"min_daily_bars": 1, "allow_caution": True},
            }
        }
    )
    assert detect_setups_on_bar("SOLUSDT", frame, 3, daily, CAUTION, allowed)
    assert detect_setups_on_bar("SOLUSDT", frame, 3, daily, CAUTION, settings_off) == []
    assert detect_setups_on_bar("SOLUSDT", frame, 3, daily, LONG_FAVORED, settings_off)


def test_selection_keeps_baseline_unless_oos_agrees():
    baseline = "ema_immediate_2r"
    rows = []
    for fold, winner_exp, base_exp in ((0, 0.2, 0.05), (1, 0.2, 0.0), (2, 0.1, 0.1)):
        rows.append({"variant": "chandelier_3_after_1r_2r", "fold": fold, "sample": "in_sample", "trade_count": 20, "expectancy": winner_exp})
        rows.append({"variant": baseline, "fold": fold, "sample": "in_sample", "trade_count": 20, "expectancy": base_exp})
    # Fold 2 is a tie, so the baseline gets that vote. Winner has 2 of 3.
    tally = tally_fold_winners(rows, baseline, min_trades=15)
    assert tally["votes"]["chandelier_3_after_1r_2r"] == 2
    assert tally["votes"][baseline] == 1
    assert majority_variant(tally["votes"], tally["voting_folds"]) == "chandelier_3_after_1r_2r"
    adopt = adoption_decision(
        winner="chandelier_3_after_1r_2r",
        baseline=baseline,
        oos={
            "chandelier_3_after_1r_2r": {"trade_count": 40, "expectancy": 0.08, "max_drawdown": 0.2},
            baseline: {"trade_count": 40, "expectancy": -0.05, "max_drawdown": 0.22},
        },
        min_oos_trades=30,
    )
    assert adopt["adopt"] is True
    blocked = adoption_decision(
        winner="chandelier_3_after_1r_2r",
        baseline=baseline,
        oos={
            "chandelier_3_after_1r_2r": {"trade_count": 40, "expectancy": -0.02, "max_drawdown": 0.2},
            baseline: {"trade_count": 40, "expectancy": -0.2, "max_drawdown": 0.3},
        },
        min_oos_trades=30,
    )
    assert blocked["adopt"] is False
    assert blocked["variant"] == baseline
    thin = []
    for fold in range(4):
        thin.append({"variant": "B1_ema_gap_0.5", "fold": fold, "sample": "in_sample", "trade_count": 3, "expectancy": 1})
        thin.append({"variant": "B0_baseline", "fold": fold, "sample": "in_sample", "trade_count": 3, "expectancy": -1})
    empty = tally_fold_winners(thin, "B0_baseline", min_trades=10)
    assert empty["voting_folds"] == 0
    assert majority_variant(empty["votes"], empty["voting_folds"]) is None


def test_vision_symbol_filter_listing_inference_and_resume(tmp_path):
    assert is_um_usdt_perpetual_symbol("BTCUSDT")
    assert is_um_usdt_perpetual_symbol("1000BONKUSDT")
    assert not is_um_usdt_perpetual_symbol("BTCUSDT_240329")
    assert not is_um_usdt_perpetual_symbol("BTCUSDC")
    assert not is_um_usdt_perpetual_symbol("BTCUSDT_PERP")
    end = int(datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp() * 1000)
    assert classify_listing(end, end) == "TRADING"
    assert classify_listing(end - 2 * DAY_MS, end) == "TRADING"
    assert classify_listing(end - 10 * DAY_MS, end) == "DELISTED"

    store = Store(tmp_path / "desk.sqlite")
    store.write_vision_klines(
        "OLDUSDT",
        "1d",
        "2024-05",
        [{"open_time": 1, "close_time": 2, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "quote_volume": 10}],
    )
    assert store.vision_month_rows("OLDUSDT", "1d", "2024-05") == 1
    calls = []

    def loader(month):
        calls.append(month)
        return None

    found = queue.Queue()
    months = ["2024-04", "2024-05", "2024-06", "2024-07", "2024-08"]
    _emit_months(
        None,
        {"2024-05": 1},
        "OLDUSDT",
        "1d",
        months,
        loader,
        found,
        threading.Lock(),
    )
    assert "2024-05" not in calls
    assert calls[0] == "2024-04"
    store.close()


def test_research_replay_matches_bar_by_bar_on_the_squeeze_book():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).with_name("test_backtest_and_io.py")
    spec = importlib.util.spec_from_file_location("squeeze_book_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _settings = module._settings
    _squeeze_book = module._squeeze_book

    from desk.backtest.replay import run_backtest
    from desk.backtest.research import scan_frames, simulate

    book = _squeeze_book()
    settings = _settings()
    symbol = book["SOLUSDT"]
    report = run_backtest(symbols={"SOLUSDT": symbol}, btc_daily=book["btc"], settings=settings)
    scanned = scan_frames(
        "SOLUSDT",
        symbol["1h"],
        symbol["4h"],
        symbol["1d"],
        book["btc"],
        symbol["funding"],
        symbol["meta"],
        settings,
    )
    assert scanned["signals"], report["reject_counts"]
    result = simulate(scanned["signals"], {"SOLUSDT": scanned["book"]}, settings)
    assert len(result["trades"]) == report["overall"]["trade_count"]
    left = result["trades"][0]
    right = report["trades"][0]
    assert left["exit_reason"] == right["exit_reason"]
    assert left["pnl"] == pytest.approx(right["pnl"])
    assert left["r_multiple"] == pytest.approx(right["r_multiple"])
    assert left["fees"] == pytest.approx(right["fees"])


def test_delisted_symbol_still_trades_on_bars_that_exist():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).with_name("test_backtest_and_io.py")
    spec = importlib.util.spec_from_file_location("squeeze_book_fixture_delisted", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    book = module._squeeze_book()
    settings = module._settings()
    symbol = book["SOLUSDT"]
    symbol["meta"]["status"] = "DELISTED"
    from desk.backtest.replay import run_backtest

    report = run_backtest(symbols={"SOLUSDT": symbol}, btc_daily=book["btc"], settings=settings, delisted_symbols_included=1)
    assert report["delisted_symbols_included"] == 1
    assert report["overall"]["trade_count"] >= 1
