"""E2 weekly rebalance and E3 daily trail on the shared paper ledger."""

import json
from datetime import datetime, timezone

import numpy as np
import pytest

from desk.data.store import Store
from desk.delivery.eligibility import submit_review
from desk.journal.decision import DecisionJournal
from desk.paper.books import (
    E2_WEEKDAY,
    BookPosition,
    Leg,
    diff_book,
    dump_book_position,
    enforce_caps,
    features_as_of,
    last_closed_index,
)
from desk.paper.engine import run_cycle
from desk.paper.scorecard import build_scorecard, note_drift
from desk.strategies.panel import Features
from desk.timeutils import DAY_MS, HOUR_MS
from desk_helpers import make_settings, market_context

NOW = 1_700_000_000_000


def _days(n: int, start: str = "2021-01-03") -> np.ndarray:
    start_ms = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    start_ms -= start_ms % DAY_MS
    return start_ms + np.arange(n, dtype=np.int64) * DAY_MS


def _settings(tmp_path, **overrides):
    settings = make_settings(tmp_path)
    settings["strategies"]["e1_cs_momentum"]["enabled"] = False
    settings["strategies"]["e4_funding_carry"]["enabled"] = False
    settings["strategies"]["e5_positioning_trend"]["enabled"] = False
    settings["strategies"]["e6_liquidation_proxy"]["enabled"] = False
    settings["setups"]["squeeze_breakout"]["enabled"] = False
    settings["setups"]["trend_pullback"]["enabled"] = False
    settings["setups"]["range_reclaim"]["enabled"] = False
    settings["setups"]["meme_momentum"]["enabled"] = False
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(settings.get(key), dict):
            settings[key].update(value)
        else:
            settings[key] = value
    return settings


def _write_daily(store, symbol, day_ms, close, volume=30_000_000.0):
    rows = []
    for day, price in zip(day_ms, close, strict=False):
        price = float(price)
        rows.append(
            {
                "open_time": int(day),
                "close_time": int(day) + DAY_MS - 1,
                "open": price,
                "high": price + 0.2,
                "low": price - 0.2,
                "close": price,
                "volume": 10.0,
                "quote_volume": volume,
            }
        )
    store.upsert_klines(symbol, "1d", rows)


def _panel(store, day_ms):
    """Sixteen names. BTC wanders. Alts drift apart over the last three weeks."""
    rng = np.random.default_rng(7)
    btc = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.004, day_ms.size))
    _write_daily(store, "BTCUSDT", day_ms, btc, volume=80_000_000.0)
    alts = [f"N{i:02d}USDT" for i in range(1, 16)]
    for index, symbol in enumerate(alts):
        shock = rng.normal(0.0, 0.01, day_ms.size)
        drift = np.zeros(day_ms.size)
        drift[-18:] = (index - 7) * 0.006
        close = 100.0 * np.cumprod(1.0 + shock + drift)
        _write_daily(store, symbol, day_ms, close)
    # A name the residual book will not select. It can sit in the ledger.
    _write_daily(store, "ZZZUSDT", day_ms, np.full(day_ms.size, 50.0), volume=1_000.0)
    return alts


def _fresh(store, now_ms):
    store.upsert_klines(
        "BTCUSDT",
        "1h",
        [
            {
                "open_time": int(now_ms) - HOUR_MS,
                "close_time": int(now_ms) - 1_000,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1.0,
                "quote_volume": 1_000_000.0,
            }
        ],
    )


def _sunday_index(day_ms, minimum: int) -> int:
    features_day = (day_ms // DAY_MS + 3) % 7
    for index in range(minimum, day_ms.size - 2):
        if int(features_day[index]) == E2_WEEKDAY:
            return index
    raise AssertionError("no Sunday in the window")


def _open_review(store, settings, signal_id, now_ms, decision="open"):
    return submit_review(
        store,
        settings,
        signal_id,
        market_context(now_ms),
        {"decision": decision, "reasoning": "closed daily bar", "ts": None},
        now_ms=now_ms,
        account={
            "equity_usdt": 800.0,
            "daily_pnl_pct": 0.0,
            "weekly_pnl_pct": 0.0,
            "news_status": "ALLOW",
        },
        open_positions=[],
    )


def _leg(symbol, side, notional, cluster, risk, book):
    return Leg(
        symbol=symbol,
        side=side,
        notional=notional,
        cluster=cluster,
        stop_pct=risk / notional,
        risk_usdt=risk,
        book=book,
    )


def test_rebalance_diff_lists_opens_closes_flips_and_resizes():
    current = {
        "A": _leg("A", 1, 100, "alts", 2, "e2"),
        "B": _leg("B", 1, 50, "alts", 1, "e2"),
        "C": _leg("C", 1, 40, "btc", 1, "e2"),
    }
    target = {
        "A": _leg("A", 1, 130, "alts", 2.6, "e2"),
        "B": _leg("B", -1, 50, "alts", 1, "e2"),
        "D": _leg("D", 1, 10, "eth", 0.2, "e2"),
    }
    diffs = diff_book(current, target)
    assert [row["symbol"] for row in diffs["increase"]] == ["A"]
    assert [row["symbol"] for row in diffs["flip"]] == ["B"]
    assert [row["symbol"] for row in diffs["close"]] == ["C"]
    assert [row["symbol"] for row in diffs["open"]] == ["D"]
    assert diffs["decrease"] == []
    assert diffs["unchanged"] == []


def test_caps_bind_across_books_before_they_bind_inside_one():
    settings = {"risk": {"max_open_risk_pct": 0.06, "max_cluster_risk_pct": 0.03, "max_notional_equity_mult": 3.0}}
    equity = 1_000.0
    total = enforce_caps(
        [],
        [_leg("A", 1, 100, "alts", 50, "e2"), _leg("B", 1, 100, "btc", 50, "e3")],
        equity,
        settings,
    )
    assert sum(leg.risk_usdt for leg in total) == pytest.approx(60.0)

    cluster = enforce_caps(
        [],
        [_leg("A", 1, 100, "alts", 40, "e2"), _leg("B", 1, 100, "alts", 40, "e3")],
        equity,
        settings,
    )
    assert sum(leg.risk_usdt for leg in cluster) == pytest.approx(30.0)
    assert {leg.cluster for leg in cluster} == {"alts"}

    shared_name = enforce_caps(
        [],
        [_leg("A", 1, 2_000, "btc", 20, "e2"), _leg("A", 1, 2_000, "btc", 20, "e3")],
        equity,
        settings,
    )
    assert sum(leg.notional for leg in shared_name) == pytest.approx(3_000.0)

    beside = enforce_caps(
        [_leg("Z", 1, 100, "alts", 25, "e2")],
        [_leg("A", 1, 100, "alts", 40, "e3")],
        equity,
        settings,
    )
    assert beside[0].symbol == "A"
    assert beside[0].risk_usdt == pytest.approx(5.0)
    # The existing E2 leg is not scaled down by a later book's proposal.
    assert beside[0].book == "e3"


def test_skip_keeps_the_current_book_and_approve_applies_the_diff(tmp_path):
    day_ms = _days(80)
    decision_i = _sunday_index(day_ms, 63)
    settings = _settings(tmp_path)
    settings["strategies"]["e3_slow_trend"]["enabled"] = False
    settings["webhook"]["enabled"] = True
    store = Store(tmp_path / "desk.sqlite")
    _panel(store, day_ms)
    # Next day's open is known. Its close is not an input.
    nxt = int(day_ms[decision_i] + DAY_MS)
    _write_daily(store, "BTCUSDT", np.array([nxt]), np.array([100.0]), volume=80_000_000.0)
    for index in range(1, 16):
        _write_daily(store, f"N{index:02d}USDT", np.array([nxt]), np.array([100.0]))
    now = int(day_ms[decision_i] + DAY_MS + 60_000)
    _fresh(store, now)
    held = BookPosition(
        book="e2",
        symbol="ZZZUSDT",
        side=1,
        qty=1.0,
        entry_price=50.0,
        stop_price=10.0,
        trail_mult=5.0,
        exit_mode="rebalance",
        cluster="alts",
        risk_usdt=2.0,
        stop_pct=0.04,
        decision_atr=1.0,
        last_mark=50.0,
        entry_time=int(day_ms[decision_i - 1]),
        opened_day_ms=int(day_ms[decision_i - 1]),
        cash_impact=-50.0,
        fees=0.0,
        slippage=0.0,
        funding=0.0,
        turnover=50.0,
        notional=50.0,
        initial_risk=2.0,
        signal_id="seed",
        sticky=False,
        setup="e2_residual_momentum",
        last_day_ms=int(day_ms[decision_i - 1]),
    )
    store.save_book_position("e2", "ZZZUSDT", dump_book_position(held), now)
    first = run_cycle(settings, store, now_ms=now, symbols=["BTCUSDT"])
    assert first["block"] is None
    assert len(first["books"]["proposed"]) == 1
    packet = store.get_signal(first["books"]["proposed"][0])
    assert packet["packet_kind"] == "rebalance"
    assert packet["book"] == "e2"
    assert packet["diffs"]["close"]
    assert any(row["symbol"] == "ZZZUSDT" for row in packet["diffs"]["close"])
    assert packet["targets"]
    assert store.outbox_by_key(packet["signal_id"])["status"] == "pending"
    skipped = _open_review(store, settings, packet["signal_id"], now, decision="skip")
    assert skipped["effective_decision"] == "skip"
    kept = run_cycle(settings, store, now_ms=now + 1_000, symbols=["BTCUSDT"])
    assert kept["books"]["filled"] == []
    still = store.book_position_rows("e2")
    assert [row["symbol"] for row in still] == ["ZZZUSDT"]

    # A later Sunday is a new packet. Approving it replaces the book.
    later_i = _sunday_index(day_ms, decision_i + 1)
    later = int(day_ms[later_i] + DAY_MS + 60_000)
    _fresh(store, later)
    _write_daily(store, "BTCUSDT", np.array([int(day_ms[later_i] + DAY_MS)]), np.array([100.0]), volume=80_000_000.0)
    proposed = run_cycle(settings, store, now_ms=later, symbols=["BTCUSDT"])
    assert proposed["books"]["proposed"]
    second = store.get_signal(proposed["books"]["proposed"][0])
    opened = _open_review(store, settings, second["signal_id"], later, decision="open")
    assert opened["effective_decision"] == "open"
    applied = run_cycle(settings, store, now_ms=later + 1_000, symbols=["BTCUSDT"])
    assert second["signal_id"] in applied["books"]["filled"]
    names = {row["symbol"] for row in store.book_position_rows("e2")}
    assert "ZZZUSDT" not in names
    assert names == {row["symbol"] for row in second["targets"]}
    for row in store.book_position_rows("e2"):
        pos = __import__("json").loads(row["payload"])
        assert pos["fees"] > 0
        assert pos["slippage"] > 0
    store.close()


def test_risk_reducing_exit_is_not_blocked_by_a_gate_a_skip_or_the_cap(tmp_path):
    day_ms = _days(80)
    decision_i = _sunday_index(day_ms, 63)
    settings = _settings(tmp_path)
    settings["account"]["news_status"] = None
    store = Store(tmp_path / "desk.sqlite")
    _panel(store, day_ms)
    now = int(day_ms[decision_i] + DAY_MS + 60_000)
    # No 1h bar: the entry path is stale. The stop still has its daily close.
    held = BookPosition(
        book="e2",
        symbol="N01USDT",
        side=1,
        qty=2.0,
        entry_price=100.0,
        stop_price=150.0,
        trail_mult=5.0,
        exit_mode="rebalance",
        cluster="alts",
        risk_usdt=400.0,
        stop_pct=0.5,
        decision_atr=1.0,
        last_mark=100.0,
        entry_time=int(day_ms[decision_i - 2]),
        opened_day_ms=int(day_ms[decision_i - 2]),
        cash_impact=-200.0,
        fees=0.1,
        slippage=0.0,
        funding=0.0,
        turnover=200.0,
        notional=200.0,
        initial_risk=400.0,
        signal_id="seed-stop",
        sticky=False,
        setup="e2_residual_momentum",
        last_day_ms=int(day_ms[decision_i - 2]),
    )
    store.save_book_position("e2", "N01USDT", dump_book_position(held), now)
    result = run_cycle(settings, store, now_ms=now, symbols=["BTCUSDT"])
    assert result["block"] == "stale_data"
    assert result["books"]["proposed"] == []
    trades = store.list_paper_trades()
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "trail_stop"
    assert trades[0]["book"] == "e2"
    assert store.book_position_rows("e2") == []
    assert store.latest_review(trades[0]["signal_id"]) is None
    exit_events = [row for row in store.read_decision_journal(trades[0]["signal_id"]) if row["event"] == "exit"]
    assert exit_events[0]["payload"]["approval"] == "not_required"
    store.close()


def test_hard_gate_cannot_force_a_rebalance_open(tmp_path):
    day_ms = _days(80)
    decision_i = _sunday_index(day_ms, 63)
    settings = _settings(tmp_path)
    settings["strategies"]["e3_slow_trend"]["enabled"] = False
    store = Store(tmp_path / "desk.sqlite")
    _panel(store, day_ms)
    now = int(day_ms[decision_i] + DAY_MS + 60_000)
    _fresh(store, now)
    first = run_cycle(settings, store, now_ms=now, symbols=["BTCUSDT"])
    packet = store.get_signal(first["books"]["proposed"][0])
    packet["hard_gates"]["open_risk"] = {"result": "fail", "reason": "over the book", "ts": packet["ts_utc"]}
    packet["decision_path"] = "pending_master"
    packet["status"] = "proposed"
    store.save_signal(packet)
    reviewed = _open_review(store, settings, packet["signal_id"], now, decision="open")
    assert reviewed["effective_decision"] == "skip"
    assert reviewed["override_refused"] is True
    assert "open_risk" in reviewed["blocking"]
    again = run_cycle(settings, store, now_ms=now + 1_000, symbols=["BTCUSDT"])
    assert again["books"]["filled"] == []
    assert store.book_position_rows() == []
    store.close()


def test_targets_do_not_read_a_bar_that_has_not_closed(tmp_path):
    day_ms = _days(80)
    decision_i = _sunday_index(day_ms, 63)
    now = int(day_ms[decision_i] + DAY_MS + 60_000)
    before = int(day_ms[decision_i] + DAY_MS - 1_000)

    def _run(path, poison: bool):
        settings = _settings(path)
        settings["strategies"]["e3_slow_trend"]["enabled"] = False
        store = Store(path / "desk.sqlite")
        _panel(store, day_ms)
        if poison:
            future = day_ms[decision_i + 1 :]
            crazy = np.full(future.size, 1_000_000.0)
            _write_daily(store, "N01USDT", future, crazy, volume=1e15)
            _write_daily(store, "N15USDT", future, np.full(future.size, 0.01), volume=1e15)
        _fresh(store, now)
        result = run_cycle(settings, store, now_ms=now, symbols=["BTCUSDT"])
        packet = store.get_signal(result["books"]["proposed"][0])
        symbols = [row["symbol"] for row in packet["targets"]]
        features = features_as_of(store, now)
        assert isinstance(features, Features)
        assert last_closed_index(features, now) == decision_i
        assert int(features.weekday[decision_i]) == E2_WEEKDAY
        store.close()
        return symbols

    clean = _run(tmp_path / "clean", poison=False)
    poisoned = _run(tmp_path / "poison", poison=True)
    assert clean == poisoned
    assert clean

    early = _settings(tmp_path)
    early["strategies"]["e3_slow_trend"]["enabled"] = False
    store = Store(tmp_path / "early.sqlite")
    _panel(store, day_ms)
    _fresh(store, before)
    result = run_cycle(early, store, now_ms=before, symbols=["BTCUSDT"])
    assert result["books"]["proposed"] == []
    store.close()


def test_second_book_fill_cannot_push_the_shared_cap_through(tmp_path):
    """An approved E3 entry beside a full E2 book is refused, not stacked."""
    settings = _settings(tmp_path)
    store = Store(tmp_path / "desk.sqlite")
    equity = 800.0
    # E2 already uses the whole 6% open-risk budget.
    held = BookPosition(
        book="e2",
        symbol="N01USDT",
        side=1,
        qty=1.0,
        entry_price=100.0,
        stop_price=50.0,
        trail_mult=5.0,
        exit_mode="rebalance",
        cluster="alts",
        risk_usdt=0.06 * equity,
        stop_pct=0.48,
        decision_atr=1.0,
        last_mark=100.0,
        entry_time=NOW,
        opened_day_ms=NOW - DAY_MS,
        cash_impact=-100.0,
        fees=0.0,
        slippage=0.0,
        funding=0.0,
        turnover=100.0,
        notional=100.0,
        initial_risk=0.06 * equity,
        signal_id="e2-full",
        sticky=False,
        setup="e2_residual_momentum",
        last_day_ms=NOW - DAY_MS,
    )
    store.save_book_position("e2", "N01USDT", dump_book_position(held), NOW)
    from desk.paper.books import _build_packet, enabled_specs

    spec = next(row for row in enabled_specs(settings) if row["book"] == "e3")
    leg = _leg("N02USDT", 1, 200.0, "alts", 0.02 * equity, "e3")
    leg.trail_mult = 3.0
    leg.exit_mode = "trail"
    leg.decision_atr = 1.0
    leg.stop_pct = 0.08
    snapshot = {
        "equity_usdt": equity,
        "daily_pnl_pct": 0.0,
        "weekly_pnl_pct": 0.0,
        "news_status": "ALLOW",
        "open_positions": [
            {
                "symbol": "N01USDT",
                "book": "e2",
                "direction": "long",
                "cluster": "alts",
                "cluster_key": "alts",
                "risk_usdt": 0.06 * equity,
                "notional_usdt": 100.0,
            }
        ],
    }
    packet = _build_packet(
        settings,
        spec,
        kind="book_entry",
        symbol="N02USDT",
        direction="long",
        day=NOW,
        ts=NOW,
        now_ms=NOW,
        snapshot=snapshot,
        targets=[leg],
        diffs={"open": [{"symbol": "N02USDT"}], "close": [], "increase": [], "decrease": [], "flip": [], "unchanged": []},
        risk_usdt=leg.risk_usdt,
        notional=leg.notional,
        cluster="alts",
        stop_pct=leg.stop_pct,
    )
    assert packet["status"] == "rejected"
    assert packet["hard_gates"]["open_risk"]["result"] == "fail"
    store.save_signal(packet)
    packet["status"] = "proposed"
    packet["decision_path"] = "pending_master"
    store.save_signal(packet)
    reviewed = _open_review(store, settings, packet["signal_id"], NOW, decision="open")
    assert reviewed["effective_decision"] == "skip"
    assert "open_risk" in reviewed["blocking"]
    assert store.book_position_rows("e3") == []
    store.close()


def test_e3_emits_one_packet_per_entry_and_the_trail_does_not_wait(tmp_path):
    day_ms = _days(80)
    decision_i = _sunday_index(day_ms, 63)
    settings = _settings(tmp_path)
    settings["strategies"]["e2_residual_momentum"]["enabled"] = False
    store = Store(tmp_path / "desk.sqlite")
    _panel(store, day_ms)
    now = int(day_ms[decision_i] + DAY_MS + 60_000)
    _fresh(store, now)
    first = run_cycle(settings, store, now_ms=now, symbols=["BTCUSDT"])
    packets = [store.get_signal(signal_id) for signal_id in first["books"]["proposed"]]
    assert packets
    assert {packet["packet_kind"] for packet in packets} == {"book_entry"}
    assert len({packet["symbol"] for packet in packets}) == len(packets)
    assert all(len(packet["targets"]) == 1 for packet in packets)
    chosen = packets[0]
    assert _open_review(store, settings, chosen["signal_id"], now)["effective_decision"] == "open"
    filled = run_cycle(settings, store, now_ms=now + 1_000, symbols=["BTCUSDT"])
    assert filled["books"]["filled"] == [chosen["signal_id"]]
    assert [row["symbol"] for row in store.book_position_rows("e3")] == [chosen["symbol"]]
    # A stop through the next close is an exit. It does not need its own packet.
    row = store.book_position_rows("e3")[0]
    pos = json.loads(row["payload"])
    pos["stop_price"] = pos["last_mark"] * (10.0 if pos["side"] > 0 else 0.1)
    store.save_book_position("e3", pos["symbol"], json.dumps(pos), now)
    later = int(day_ms[decision_i + 1] + DAY_MS + 60_000)
    _fresh(store, later)
    stopped = run_cycle(settings, store, now_ms=later, symbols=["BTCUSDT"])
    assert store.book_position_rows("e3") == []
    assert stopped["books"]["exited"]
    trade = store.list_paper_trades()[-1]
    assert trade["exit_reason"] == "trail_stop"
    # The entry review is the only decision. The stop did not open a second one.
    assert store.latest_review(chosen["signal_id"])["effective_decision"] == "open"
    exit_events = [row for row in store.read_decision_journal(trade["signal_id"]) if row["event"] == "exit"]
    assert exit_events[-1]["payload"]["approval"] == "not_required"
    assert exit_events[-1]["payload"]["reason"] == "trail_stop"
    store.close()


def test_rebalance_fills_at_the_decision_close_when_the_next_open_is_absent(tmp_path):
    day_ms = _days(80)
    decision_i = _sunday_index(day_ms, 63)
    settings = _settings(tmp_path)
    settings["strategies"]["e3_slow_trend"]["enabled"] = False
    store = Store(tmp_path / "desk.sqlite")
    _panel(store, day_ms[: decision_i + 1])
    now = int(day_ms[decision_i] + DAY_MS + 60_000)
    _fresh(store, now)
    first = run_cycle(settings, store, now_ms=now, symbols=["BTCUSDT"])
    packet = store.get_signal(first["books"]["proposed"][0])
    assert _open_review(store, settings, packet["signal_id"], now)["effective_decision"] == "open"
    applied = run_cycle(settings, store, now_ms=now + 1_000, symbols=["BTCUSDT"])
    assert packet["signal_id"] in applied["books"]["filled"]
    assert store.book_position_rows("e2")
    store.close()


def test_drift_alert_is_journalled_once_when_paper_leaves_the_band(tmp_path):
    settings = _settings(tmp_path)
    store = Store(tmp_path / "desk.sqlite")
    from desk.paper.engine import ensure_paper_account

    ensure_paper_account(store, settings, NOW)
    row = store.paper_account()
    row["started_at_ms"] = NOW - 30 * DAY_MS
    store.save_paper_account(row)
    card = build_scorecard(store, settings, NOW)
    assert "e2" in card["books"] and "e3" in card["books"] and "combined" in card["books"]
    assert "e2 turnover" in card["books"]["combined"]["alerts"]
    assert card["books"]["e2"]["drift"]["expected_turnover"] > 0
    journal = DecisionJournal(store, settings)
    note_drift(store, settings, journal, NOW, card)
    note_drift(store, settings, journal, NOW + 1, card)
    events = [item for item in store.read_decision_journal("book-drift") if item["event"] == "drift"]
    assert len(events) == 1
    assert "e2 turnover" in events[0]["payload"]["alerts"]
    store.close()
