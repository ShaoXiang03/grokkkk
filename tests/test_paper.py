"""Paper cycle: same cost model as the backtester, fail closed everywhere else."""

import pytest

from desk.backtest.costs import adverse_price, slippage_fraction, taker_fee
from desk.data.store import Store
from desk.delivery.eligibility import submit_review
from desk.journal.decision import DecisionJournal
from desk.paper.clock import paper_clock
from desk.paper.engine import run_cycle
from desk.paper.scorecard import build_scorecard
from desk.paper.state import load_position
from desk.timeutils import DAY_MS, HOUR_MS
from desk_helpers import account_view, make_packet, make_settings, market_context, seed_hour

NOW = 1_700_000_000_000


def _prepare(tmp_path, packet, now_ms=NOW, news="ALLOW"):
    settings = make_settings(tmp_path)
    settings["account"]["news_status"] = news
    store = Store(tmp_path / "desk.sqlite")
    store.save_signal(packet)
    DecisionJournal(store, settings).record_signal(packet, packet["ts_ms"])
    seed_hour(store, packet["symbol"], now_ms, price=float(packet["entry"]))
    return settings, store


def _accept(store, settings, packet, now_ms=NOW):
    return submit_review(
        store,
        settings,
        packet["signal_id"],
        market_context(now_ms),
        {"decision": "open", "reasoning": "closed bar held", "ts": None},
        now_ms=now_ms,
        account=account_view(news=settings["account"]["news_status"]),
    )


def test_paper_fill_matches_backtest_costs_and_then_funds(tmp_path):
    packet = make_packet(now_ms=NOW)
    settings, store = _prepare(tmp_path, packet)
    assert _accept(store, settings, packet)["effective_decision"] == "open"
    result = run_cycle(settings, store, now_ms=NOW, symbols=["BTCUSDT"])
    assert result["block"] is None
    assert len(result["filled"]) == 1
    slip = slippage_fraction(settings, 0.0005)
    expected = adverse_price(100.0, "long", is_entry=True, slip=slip)
    assert result["filled"][0]["fill_price"] == pytest.approx(expected)
    assert result["filled"][0]["fee"] == pytest.approx(taker_fee(expected, 0.0005))
    card = result["scorecard"]
    assert card["clock"]["timezone"] == "Asia/Kuala_Lumpur"
    assert card["clock"]["duration_days"] == 14
    assert card["clock"]["complete"] is False
    assert card["clock"]["calendar_day"] == 1
    assert card["reviewer"]["opened"] == 1
    assert card["reviewer"]["skipped"] == 0
    assert card["rule_violations"] == 0
    assert card["trades"] == 0

    later = NOW + HOUR_MS
    store.upsert_klines(
        "BTCUSDT",
        "1h",
        [
            {
                "open_time": later - HOUR_MS + 1,
                "close_time": later,
                "open": 100,
                "high": 101,
                "low": 96,
                "close": 100,
                "volume": 10,
                "quote_volume": 1000,
            }
        ],
    )
    store.upsert_funding("BTCUSDT", [{"funding_time": NOW + 1_000, "funding_rate": 0.0001}])
    funded = run_cycle(settings, store, now_ms=later, symbols=["BTCUSDT"])
    assert funded["block"] is None
    pos = load_position(store.paper_position_rows()[0]["payload"])
    assert pos.funding < 0
    assert pos.fees > 0
    store.close()


def test_stop_exit_is_a_scored_trade(tmp_path):
    packet = make_packet(now_ms=NOW)
    settings, store = _prepare(tmp_path, packet)
    _accept(store, settings, packet)
    run_cycle(settings, store, now_ms=NOW, symbols=["BTCUSDT"])
    later = NOW + HOUR_MS
    store.upsert_klines(
        "BTCUSDT",
        "1h",
        [
            {
                "open_time": later - HOUR_MS + 1,
                "close_time": later,
                "open": 100,
                "high": 100.5,
                "low": 90,
                "close": 94,
                "volume": 10,
                "quote_volume": 1000,
            }
        ],
    )
    result = run_cycle(settings, store, now_ms=later, symbols=["BTCUSDT"])
    assert result["closed"] == ["sig-1"]
    trade = store.list_paper_trades()[0]
    assert trade["exit_reason"] == "hard_stop"
    assert trade["fees"] > 0
    assert trade["r_multiple"] is not None
    card = result["scorecard"]
    assert card["trades"] == 1
    assert card["win_rate"] == 0
    assert card["expectancy_r"] == pytest.approx(trade["r_multiple"])
    assert card["max_drawdown"] is not None
    store.close()


def test_second_fill_is_blocked_by_the_open_risk_cap(tmp_path):
    settings = make_settings(tmp_path, risk={"max_cluster_risk_pct": 0.06})
    settings["data"]["symbols"] = ["BTCUSDT", "ETHUSDT"]
    store = Store(tmp_path / "desk.sqlite")
    first = make_packet(
        signal_id="sig-btc",
        symbol="BTCUSDT",
        now_ms=NOW,
        entry=100,
        stop=90,
        size=3,
        risk_usdt=30,
        cluster_key="btc-long",
    )
    second = make_packet(
        signal_id="sig-eth",
        symbol="ETHUSDT",
        now_ms=NOW + 1,
        entry=100,
        stop=90,
        size=3,
        risk_usdt=30,
        cluster_key="eth-long",
    )
    journal = DecisionJournal(store, settings)
    for packet in (first, second):
        store.save_signal(packet)
        journal.record_signal(packet, packet["ts_ms"])
        seed_hour(store, packet["symbol"], NOW, price=100)
        submit_review(
            store,
            settings,
            packet["signal_id"],
            market_context(NOW),
            {"decision": "open", "reasoning": "both passed the scan", "ts": None},
            now_ms=NOW,
            account=account_view(),
        )
    result = run_cycle(settings, store, now_ms=NOW, symbols=["BTCUSDT", "ETHUSDT"])
    assert [item["signal_id"] for item in result["filled"]] == ["sig-btc"]
    assert store.resolution("sig-eth")["outcome"] == "refused"
    assert "open_risk" in store.resolution("sig-eth")["detail"]
    assert result["scorecard"]["rule_violations"] >= 1
    assert {row["symbol"] for row in store.paper_position_rows()} == {"BTCUSDT"}
    store.close()


def test_stale_data_does_not_fill_and_counts_once(tmp_path):
    packet = make_packet(now_ms=NOW - 10 * HOUR_MS)
    settings, store = _prepare(tmp_path, packet, now_ms=NOW - 10 * HOUR_MS)
    _accept(store, settings, packet, now_ms=NOW - 10 * HOUR_MS)
    first = run_cycle(settings, store, now_ms=NOW, symbols=["BTCUSDT"])
    second = run_cycle(settings, store, now_ms=NOW + 1_000, symbols=["BTCUSDT"])
    assert first["block"] == "stale_data"
    assert second["block"] == "stale_data"
    assert store.paper_position_rows() == []
    assert first["scorecard"]["safety_blocks"] == 1
    assert second["scorecard"]["safety_blocks"] == 1
    assert first["scorecard"]["trades"] == 0
    store.close()


def test_unreadable_equity_does_not_fill(tmp_path):
    packet = make_packet(now_ms=NOW)
    settings, store = _prepare(tmp_path, packet)
    settings["paper"]["initial_equity"] = None
    _accept(store, settings, packet)
    result = run_cycle(settings, store, now_ms=NOW, symbols=["BTCUSDT"])
    assert result["block"] == "equity_unreadable"
    assert store.paper_position_rows() == []
    assert result["scorecard"]["safety_blocks"] == 1
    store.close()


def test_kill_switch_refuses_an_open_decision(tmp_path):
    packet = make_packet(now_ms=NOW)
    settings, store = _prepare(tmp_path, packet)
    _accept(store, settings, packet)
    kill = tmp_path / "KILL"
    kill.write_text("halt\n", encoding="utf-8")
    result = run_cycle(settings, store, now_ms=NOW, symbols=["BTCUSDT"])
    assert result["block"] == "kill_switch"
    assert store.paper_position_rows() == []
    assert store.resolution(packet["signal_id"])["outcome"] == "refused"
    assert result["scorecard"]["rule_violations"] == 0
    assert result["scorecard"]["safety_blocks"] == 1
    store.close()


def test_open_after_the_window_does_not_fill(tmp_path):
    packet = make_packet(now_ms=NOW - 2 * HOUR_MS)
    settings = make_settings(tmp_path, decision={"timeout_seconds": 60, "timeout_action": "open"})
    store = Store(tmp_path / "desk.sqlite")
    store.save_signal(packet)
    DecisionJournal(store, settings).record_signal(packet, packet["ts_ms"])
    seed_hour(store, "BTCUSDT", NOW, price=100)
    result = submit_review(
        store,
        settings,
        packet["signal_id"],
        market_context(NOW),
        {"decision": "open", "reasoning": "late", "ts": None},
        now_ms=NOW,
        account=account_view(),
    )
    assert result["effective_decision"] == "skip"
    assert result["reason"] == "decision_timeout"
    cycle = run_cycle(settings, store, now_ms=NOW, symbols=["BTCUSDT"])
    assert cycle["filled"] == []
    assert store.paper_position_rows() == []
    store.close()


def test_decision_timeout_skips_and_does_not_fill(tmp_path):
    packet = make_packet(now_ms=NOW - 2 * HOUR_MS)
    settings = make_settings(tmp_path, decision={"timeout_seconds": 60, "timeout_action": "open"})
    store = Store(tmp_path / "desk.sqlite")
    store.save_signal(packet)
    DecisionJournal(store, settings).record_signal(packet, packet["ts_ms"])
    seed_hour(store, "BTCUSDT", NOW, price=100)
    result = run_cycle(settings, store, now_ms=NOW, symbols=["BTCUSDT"])
    assert packet["signal_id"] in result["expired"]
    assert store.latest_review(packet["signal_id"])["reason"] == "decision_timeout"
    assert store.paper_position_rows() == []
    card = result["scorecard"]
    assert card["reviewer"]["skipped"] == 1
    assert card["reviewer"]["timeout_skip"] == 1
    assert card["reviewer"]["opened"] == 0
    store.close()


def test_live_news_veto_blocks_a_previously_accepted_open(tmp_path):
    packet = make_packet(now_ms=NOW)
    settings, store = _prepare(tmp_path, packet, news="ALLOW")
    assert _accept(store, settings, packet)["effective_decision"] == "open"
    settings["account"]["news_status"] = None
    result = run_cycle(settings, store, now_ms=NOW, symbols=["BTCUSDT"])
    assert result["filled"] == []
    assert store.resolution(packet["signal_id"])["outcome"] == "refused"
    assert "news" in store.resolution(packet["signal_id"])["detail"]
    assert result["scorecard"]["rule_violations"] >= 1
    assert store.paper_position_rows() == []
    store.close()


def test_live_mode_is_refused_before_a_cycle(tmp_path):
    settings = make_settings(tmp_path)
    settings["mode"] = "live"
    store = Store(tmp_path / "desk.sqlite")
    with pytest.raises(RuntimeError):
        run_cycle(settings, store, now_ms=NOW, symbols=["BTCUSDT"])
    assert store.paper_account() is None
    store.close()


def test_fourteen_day_clock_completes_on_the_duration():
    started = NOW
    assert paper_clock(started, started, 14)["complete"] is False
    finished = paper_clock(started, started + 14 * DAY_MS, 14)
    assert finished["complete"] is True
    assert finished["remaining_ms"] == 0
    assert finished["timezone"] == "Asia/Kuala_Lumpur"


def test_scorecard_clock_uses_the_persisted_start(tmp_path):
    packet = make_packet(now_ms=NOW)
    settings, store = _prepare(tmp_path, packet)
    _accept(store, settings, packet)
    run_cycle(settings, store, now_ms=NOW, symbols=["BTCUSDT"])
    row = store.paper_account()
    row["started_at_ms"] = NOW - 14 * DAY_MS
    store.save_paper_account(row)
    card = build_scorecard(store, settings, NOW)
    assert card["clock"]["complete"] is True
    assert card["reviewer"]["opened"] == 1
    store.close()


def test_new_modules_have_no_order_endpoint():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    for relative in ("src/desk/paper", "src/desk/delivery", "src/desk/journal"):
        for path in (root / relative).rglob("*.py"):
            text = path.read_text()
            assert "/fapi/v1/order" not in text
            assert "/sapi/" not in text
            assert "apiKey" not in text
