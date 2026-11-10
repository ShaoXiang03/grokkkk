"""Backtest path, no-lookahead, storage, and the public-only client."""

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from desk.backtest.replay import manage_bar, run_backtest
from desk.backtest.walkforward import walk_forward_folds
from desk.config import load_settings
from desk.data.binance import ALLOWED_PATHS, BinancePublicClient, BinancePublicError, parse_klines
from desk.data.store import Store
from desk.rules.exits import breakeven_stop
from desk.rules.onchain import PASS
from desk.scanner.engine import evaluate_frame_at
from desk.scanner.signals import validate_packet
from desk.scanner.webhook import send_packet
from desk.timeutils import DAY_MS, HOUR_MS

H4 = 4 * HOUR_MS
ROOT = Path(__file__).resolve().parents[1]


def _settings():
    settings = load_settings(
        overrides={
            "indicators": {
                "bb_period": 10,
                "rsi_period": 5,
                "atr_period": 5,
                "volume_avg_period": 10,
                "ema_periods": [5, 8, 12],
                "sma_fast": 5,
                "sma_slow": 15,
            },
            "setups": {
                "squeeze_breakout": {"bandwidth_lookback": 10, "min_daily_bars": 15, "enabled": True},
                "trend_pullback": {"enabled": False},
                "range_reclaim": {"enabled": False},
                "meme_momentum": {"enabled": False},
            },
            "costs": {"slippage_rate": 0.0, "cost_bump_tp1": True},
            "backtest": {"assumed_spread_pct": 0.0002, "initial_equity": 800},
        }
    )
    return settings


def _rising_daily(start_ms: int, n: int, step: int = DAY_MS) -> pd.DataFrame:
    rows = []
    for i in range(n):
        close = 50 + i
        op = start_ms + i * step
        rows.append(
            {
                "open_time": op,
                "close_time": op + step - 1,
                "open": close - 0.5,
                "high": close + 0.5,
                "low": close - 1,
                "close": close,
                "volume": 1000,
                "quote_volume": 1_000_000,
            }
        )
    return pd.DataFrame(rows)


def _squeeze_book():
    start = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    four = []
    for i in range(48):
        op = start + i * H4
        if i == 40:
            o, h, l, c, v = 100.0, 101.2, 99.8, 101.0, 8_000.0
        else:
            o, h, l, c, v = 100.0, 100.3, 99.7, 100.0, 1_000.0
        four.append(
            {
                "open_time": op,
                "close_time": op + H4 - 1,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
                "quote_volume": 2_000_000,
            }
        )
    one = []
    for bar in four:
        for hour in range(4):
            op = bar["open_time"] + hour * HOUR_MS
            # The trigger window opens on the first hour after the breakout 4H close.
            if bar["open_time"] == start + 40 * H4 and hour == 3:
                o, h, l, c = 100.4, 101.2, 100.2, 101.0
            elif bar["open_time"] == start + 41 * H4 and hour == 0:
                o, h, l, c = 101.0, 101.4, 100.6, 101.2
            else:
                o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
            one.append(
                {
                    "open_time": op,
                    "close_time": op + HOUR_MS - 1,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": 100,
                    "quote_volume": 1_000_000,
                }
            )
    # Extra quiet hours so the trigger bar is not the final row.
    last = one[-1]["open_time"]
    for extra in range(1, 9):
        op = last + extra * HOUR_MS
        one.append(
            {
                "open_time": op,
                "close_time": op + HOUR_MS - 1,
                "open": 101.2,
                "high": 101.5,
                "low": 100.9,
                "close": 101.3,
                "volume": 100,
                "quote_volume": 1_000_000,
            }
        )
    daily_start = start - 30 * DAY_MS
    daily = _rising_daily(daily_start, 50)
    funding = pd.DataFrame(
        {"funding_time": [start - 8 * HOUR_MS, start + 8 * HOUR_MS], "funding_rate": [0.0001, 0.0001]}
    )
    meta = {
        "base_asset": "SOL",
        "onboard_date_ms": start - 60 * DAY_MS,
        "status": "TRADING",
        "contract_type": "PERPETUAL",
        "quote_asset": "USDT",
    }
    return {
        "SOLUSDT": {"1h": pd.DataFrame(one), "4h": pd.DataFrame(four), "1d": daily, "funding": funding, "meta": meta},
        "btc": daily.copy(),
        "trigger_open": start + 41 * H4,
    }


def test_backtest_uses_scanner_path_and_reports_real_metrics():
    book = _squeeze_book()
    settings = _settings()
    report = run_backtest(
        symbols={"SOLUSDT": book["SOLUSDT"]},
        btc_daily=book["btc"],
        settings=settings,
        delisted_symbols_included=0,
    )
    assert report["delisted_symbols_included"] == 0
    assert "survived" in report["survivorship_note"]
    assert report["partial_at_r"] == 2.0
    assert report["overall"]["trade_count"] >= 1, report["reject_counts"]
    trade = report["trades"][0]
    assert trade["fees"] > 0
    assert trade["r_multiple"] is not None
    assert report["walk_forward"]["out_of_sample"] is None
    assert report["per_setup"]["squeeze_breakout"]["trade_count"] >= 1
    assert report["per_setup"]["meme_momentum"]["trade_count"] == 0


def test_partial_at_1r_is_configurable():
    book = _squeeze_book()
    base = _settings()
    base["risk"]["enforce_min_reward_risk"] = False
    at_2 = load_settings(overrides={"exits": {"partial_at_r": 2.0}, "risk": {"enforce_min_reward_risk": False}})
    # Keep the short-indicator overrides from _settings by merging.
    settings_2 = _settings()
    settings_2["risk"]["enforce_min_reward_risk"] = False
    settings_2["exits"]["partial_at_r"] = 2.0
    settings_1 = _settings()
    settings_1["risk"]["enforce_min_reward_risk"] = False
    settings_1["exits"]["partial_at_r"] = 1.0
    report_2 = run_backtest(symbols={"SOLUSDT": book["SOLUSDT"]}, btc_daily=book["btc"], settings=settings_2)
    report_1 = run_backtest(symbols={"SOLUSDT": book["SOLUSDT"]}, btc_daily=book["btc"], settings=settings_1)
    assert report_2["partial_at_r"] == 2.0
    assert report_1["partial_at_r"] == 1.0
    assert report_1["overall"]["trade_count"] == report_2["overall"]["trade_count"]
    assert base["exits"]["partial_at_r"] == 2.0 or settings_2["exits"]["partial_at_r"] == 2.0


def test_no_lookahead_signal_ignores_future_bars():
    book = _squeeze_book()
    settings = _settings()
    symbol = book["SOLUSDT"]
    df1h = symbol["1h"]
    trigger_open = book["trigger_open"]
    index = int(df1h.index[df1h["open_time"] == trigger_open][0])
    market = {
        "symbol": "SOLUSDT",
        "status": "TRADING",
        "contract_type": "PERPETUAL",
        "quote_asset": "USDT",
        "base_asset": "SOL",
        "onboard_date_ms": symbol["meta"]["onboard_date_ms"],
        "funding_rate": 0.0001,
        "predicted_funding_rate": 0.0001,
        "funding_predicted_source": "test",
    }
    account = {"equity_usdt": 800, "daily_pnl_pct": 0.0, "weekly_pnl_pct": 0.0, "news_status": "ALLOW"}
    kwargs = dict(
        symbol="SOLUSDT",
        df4h=symbol["4h"],
        df1d=symbol["1d"],
        btc_1d=book["btc"],
        i_1h=index,
        settings=settings,
        market=market,
        account=account,
        historical=True,
    )
    original = evaluate_frame_at(df1h=df1h, **kwargs)
    assert original, "expected a scanner packet on the trigger bar"
    validate_packet(original[0], scanner_output=True)
    assert "market_bot" not in original[0]["votes"]
    assert original[0]["market_context"]["strengtheners"] == []
    assert original[0]["market_context"]["onchain_checks"] == {}
    assert original[0]["master_decision"]["decision"] is None
    corrupted = df1h.copy()
    corrupted.loc[corrupted.index > index, "close"] = 50_000
    corrupted.loc[corrupted.index > index, "high"] = 50_001
    corrupted.loc[corrupted.index > index, "low"] = 49_000
    again = evaluate_frame_at(df1h=corrupted, **kwargs)
    assert again[0]["signal_id"] == original[0]["signal_id"]
    assert again[0]["entry"] == original[0]["entry"]
    assert again[0]["stop"] == original[0]["stop"]
    # A bar before the breakout must not see it.
    early = evaluate_frame_at(df1h=df1h, **{**kwargs, "i_1h": index - 8})
    assert all(packet["trigger_open_time"] != trigger_open for packet in early)


def test_setup_d_missing_onchain_fails_closed():
    from desk.rules.onchain import onchain_gate

    assert not onchain_gate(None)["pass"]
    names = onchain_gate(None)["checks"]
    assert onchain_gate(names)["pass"] is False
    supplied = {name: PASS for name in names}
    assert onchain_gate(supplied)["pass"]


def test_manage_bar_partial_then_stop_never_widens():
    settings = load_settings(overrides={"costs": {"slippage_rate": 0.0}})
    from desk.backtest.replay import Position

    pos = Position(
        symbol="SOLUSDT",
        setup="squeeze_breakout",
        direction="long",
        cluster_key="alts-long",
        signal_id="x",
        entry_time=0,
        entry_index=0,
        fill=100,
        initial_stop=95,
        stop=95,
        tp1=110,
        size=2,
        remaining=2,
        partial_done=False,
        reached_1r=False,
        bars_4h=0,
        fees=0,
        funding=0,
        realized=0,
        time_stop_bars=8,
        partial_fraction=0.5,
    )
    bar = pd.Series({"open": 100, "high": 111, "low": 99, "close": 110, "close_time": 10})
    reason = manage_bar(pos, bar, None, None, settings, "ema_20", "ema_200")
    assert reason is None
    assert pos.partial_done
    assert pos.remaining == pytest.approx(1)
    assert pos.stop == breakeven_stop(95, 100, "long")
    # A later bar cannot push the stop back down.
    later = pd.Series({"open": 100, "high": 101, "low": 90, "close": 91, "close_time": 20})
    exit_reason = manage_bar(pos, later, None, None, settings, "ema_20", "ema_200")
    assert exit_reason == "trail_stop"
    assert pos.stop == 100


def test_walk_forward_needs_a_long_enough_sample():
    start = 0
    end = 400 * DAY_MS
    folds = walk_forward_folds(start, end, 182, 91)
    assert folds
    assert folds[0]["out_of_sample"]["start_ms"] == folds[0]["in_sample"]["end_ms"]
    assert walk_forward_folds(0, 100 * DAY_MS, 182, 91) == []


def test_store_incremental_klines_and_dedupe(tmp_path):
    store = Store(tmp_path / "desk.sqlite")
    rows = parse_klines([[0, "1", "2", "0.5", "1.5", "10", 3_599_999, "15", 1, "0", "0", "0"]])
    assert store.upsert_klines("BTCUSDT", "1h", rows) == 1
    rows[0]["close"] = 1.6
    store.upsert_klines("BTCUSDT", "1h", rows)
    frame = store.klines("BTCUSDT", "1h")
    assert len(frame) == 1
    assert frame.iloc[0]["close"] == 1.6
    assert store.latest_open_time("BTCUSDT", "1h") == 0
    packet = {
        "signal_id": "s1",
        "dedupe_key": "BTCUSDT|squeeze_breakout|long|0",
        "symbol": "BTCUSDT",
        "setup": "squeeze_breakout",
        "direction": "long",
        "ts_ms": 1,
        "status": "proposed",
    }
    store.save_signal(packet)
    assert store.has_dedupe(packet["dedupe_key"])
    assert store.has_signal("s1")
    store.close()


def test_client_refuses_order_endpoints():
    client = BinancePublicClient()
    with pytest.raises(BinancePublicError):
        client.get("/fapi/v1/order", {"symbol": "BTCUSDT"})
    with pytest.raises(BinancePublicError):
        client.get("/sapi/v1/capital/withdraw/apply", {})
    source = Path(ROOT / "src" / "desk" / "data" / "binance.py").read_text()
    assert "/fapi/v1/order" not in source
    assert "/sapi/" not in source
    assert "apiKey" not in source
    assert "/fapi/v1/klines" in ALLOWED_PATHS
    assert all(path.startswith("/fapi/v1/") for path in ALLOWED_PATHS)


def test_webhook_is_off_by_default_and_url_is_configurable(monkeypatch):
    settings = load_settings(ROOT / "config" / "settings.yaml")
    assert settings["webhook"]["enabled"] is False
    assert settings["webhook"]["url"] == ""
    assert settings["webhook"]["require_signature"] is True
    assert settings["mode"] == "paper"
    text = (ROOT / "config" / "settings.yaml").read_text().lower()
    assert "api_key" not in text
    assert "secret_key" not in text
    assert "webhook_secret:" not in text
    captured = {}

    class Session:
        def post(self, url, data, headers, timeout):
            captured["url"] = url
            captured["data"] = data
            captured["headers"] = dict(headers)

            class Response:
                status_code = 200
                text = "ok"

            return Response()

    silent = send_packet({"status": "proposed"}, settings, session=Session())
    assert silent["reason"] == "disabled"
    assert "url" not in captured
    enabled = load_settings(
        overrides={"webhook": {"enabled": True, "url": "https://market-bot.example/hook", "max_retries": 1}}
    )
    blocked = send_packet({"status": "proposed", "signal_id": "abc"}, enabled, session=Session())
    assert blocked["sent"] is False
    assert blocked["reason"] == "missing_secret"
    assert "url" not in captured
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")
    stamped = 1_700_000_000_000
    sent = send_packet(
        {"status": "proposed", "signal_id": "abc"},
        enabled,
        session=Session(),
        now_ms=stamped,
    )
    assert sent["sent"] is True
    assert captured["url"] == "https://market-bot.example/hook"
    assert captured["headers"]["Idempotency-Key"] == "abc"
    from desk.delivery.signing import verify_signature

    assert verify_signature(
        captured["data"],
        "test-secret",
        captured["headers"]["X-Desk-Timestamp"],
        captured["headers"]["X-Desk-Signature"],
        now_ms=stamped,
        max_age_ms=5_000,
    )


def test_vision_csv_parsers_and_month_span():
    from desk.data.vision import iter_months, parse_funding_csv, parse_kline_csv

    kline = parse_kline_csv(
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n"
        "1700000000000,1,2,0.5,1.5,10,1700003599999,15,1,0,0,0\n"
    )
    assert kline[0]["close"] == 1.5
    assert kline[0]["quote_volume"] == 15
    funding = parse_funding_csv("calc_time,funding_interval_hours,last_funding_rate\n1700000000001,8,0.0001\n")
    assert funding[0]["funding_rate"] == pytest.approx(0.0001)
    months = iter_months(int(datetime(2026, 6, 15, tzinfo=timezone.utc).timestamp() * 1000), int(datetime(2026, 8, 2, tzinfo=timezone.utc).timestamp() * 1000))
    assert months == ["2026-06", "2026-07", "2026-08"]


def test_live_mode_is_refused():
    from desk.config import assert_no_live_orders

    with pytest.raises(RuntimeError):
        assert_no_live_orders({"mode": "live"})
