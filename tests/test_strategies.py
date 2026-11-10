"""Point-in-time universe, caps, and the portfolio fill. No network."""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from desk.strategies import require_research_mode
from desk.strategies.engine import simulate
from desk.strategies.panel import Features, panel_from_frames
from desk.strategies.select import choose_frozen, fold_winner, holdout_is_positive
from desk.strategies.signals import _residual, decide_e1
from desk.strategies.sizing import greedy_fill, inverse_vol_balanced, notionals_from_risk
from desk.strategies.stats import _norm_ppf, deflated_sharpe
from desk.strategies.universe import gap_ok, is_excluded_symbol, momentum_score, select_universe, volume_rank
from desk.data.vision import metrics_last_snapshot, parse_funding_csv, parse_metrics_csv
from desk.timeutils import DAY_MS


def _days(n: int, start: str = "2021-01-03") -> np.ndarray:
    start_ms = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    return start_ms + np.arange(n, dtype=np.int64) * DAY_MS


def _frame(symbol: str, day_ms: np.ndarray, close: np.ndarray, volume: float = 30_000_000.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": symbol,
            "open_time": day_ms,
            "open": close.copy(),
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "quote_volume": np.full(day_ms.shape, volume),
        }
    )


def test_metrics_keep_the_last_complete_row_and_funding_interval():
    text = (
        "create_time,symbol,sum_open_interest,sum_open_interest_value,"
        "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
        "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
        "2020-09-01 00:00:00,BTCUSDT,1,10,1,1,1,1\n"
        "2020-09-01 00:00:00,BTCUSDT,,,1,1,1,1\n"
        "2020-09-01 00:05:00,BTCUSDT,2,20,1,1,1,1\n"
        "2020-09-01 00:05:00,BTCUSDT,3,30,1,1,1,1\n"
    )
    rows = parse_metrics_csv(text)
    assert [row["sum_open_interest"] for row in rows] == [1.0, 3.0]
    snap = metrics_last_snapshot(rows)
    assert snap["sum_open_interest"] == 3.0
    assert snap["day_ms"] == int(datetime(2020, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
    funding = parse_funding_csv("calc_time,funding_interval_hours,last_funding_rate\n1577836800000,8,-0.00012359\n")
    assert funding[0]["interval_hours"] == 8
    assert funding[0]["funding_rate"] == pytest.approx(-0.00012359)


def test_universe_ignores_the_future_and_ranks_the_trailing_window():
    day_ms = _days(80)
    close = np.full(80, 100.0)
    quiet = _frame("QUIETUSDT", day_ms, close, volume=25_000_000.0)
    loud = _frame("LOUDUSDT", day_ms, close, volume=40_000_000.0)
    # A spike before the 30-day window must not outrank the recent book.
    stale = _frame("STALEUSDT", day_ms, close, volume=1_000_000.0)
    stale.loc[stale.index < 10, "quote_volume"] = 500_000_000.0
    future = _frame("FUTUREUSDT", day_ms, close, volume=1_000_000.0)
    panel = panel_from_frames(pd.concat([quiet, loud, stale, future], ignore_index=True))
    i = 50
    before = select_universe(
        panel.symbols,
        panel.close,
        panel.quote_volume,
        i,
        top_n=2,
        min_history=20,
        min_avg_quote=20_000_000.0,
        min_oi_value=0.0,
    )
    names = [panel.symbols[index] for index in before]
    assert names == ["LOUDUSDT", "QUIETUSDT"]
    panel.quote_volume[panel.symbols.index("FUTUREUSDT"), i + 1 :] = 1e12
    panel.close[panel.symbols.index("FUTUREUSDT"), i + 1 :] = 500.0
    after = select_universe(
        panel.symbols,
        panel.close,
        panel.quote_volume,
        i,
        top_n=2,
        min_history=20,
        min_avg_quote=20_000_000.0,
        min_oi_value=0.0,
    )
    assert after == before
    ranks = volume_rank(panel.symbols, panel.close, panel.quote_volume, i)
    assert ranks[panel.symbols.index("LOUDUSDT")] < ranks[panel.symbols.index("QUIETUSDT")]
    assert ranks[panel.symbols.index("FUTUREUSDT")] > ranks[panel.symbols.index("QUIETUSDT")]


def test_delisted_name_is_eligible_on_its_last_bar_only():
    day_ms = _days(50)
    alive = _frame("ALIVEUSDT", day_ms, np.full(50, 100.0))
    dead_days = day_ms[:40]
    dead = _frame("DEADUSDT", dead_days, np.full(40, 100.0))
    panel = panel_from_frames(pd.concat([alive, dead], ignore_index=True))
    last = 39
    picked = select_universe(
        panel.symbols,
        panel.close,
        panel.quote_volume,
        last,
        top_n=5,
        min_history=10,
        min_avg_quote=1.0,
        min_oi_value=0.0,
    )
    assert "DEADUSDT" in [panel.symbols[index] for index in picked]
    later = select_universe(
        panel.symbols,
        panel.close,
        panel.quote_volume,
        45,
        top_n=5,
        min_history=10,
        min_avg_quote=1.0,
        min_oi_value=0.0,
    )
    assert "DEADUSDT" not in [panel.symbols[index] for index in later]
    assert is_excluded_symbol("USDCUSDT")
    assert is_excluded_symbol("BTCDOMUSDT")
    stable = _frame("USDCUSDT", day_ms, np.full(50, 1.0), volume=1e12)
    panel2 = panel_from_frames(pd.concat([alive, stable], ignore_index=True))
    picked2 = select_universe(
        panel2.symbols,
        panel2.close,
        panel2.quote_volume,
        30,
        top_n=5,
        min_history=10,
        min_avg_quote=1.0,
        min_oi_value=0.0,
    )
    assert "USDCUSDT" not in [panel2.symbols[index] for index in picked2]


def test_short_history_and_gaps_are_excluded_without_reading_ahead():
    day_ms = _days(30)
    young = _frame("NEWUSDT", day_ms[-8:], np.full(8, 100.0))
    old = _frame("OLDUSDT", day_ms, np.full(30, 100.0))
    panel = panel_from_frames(pd.concat([young, old], ignore_index=True))
    picked = select_universe(
        panel.symbols,
        panel.close,
        panel.quote_volume,
        29,
        top_n=5,
        min_history=20,
        min_avg_quote=1.0,
        min_oi_value=0.0,
    )
    assert [panel.symbols[index] for index in picked] == ["OLDUSDT"]
    close = np.arange(1, 40, dtype=float)
    assert np.isfinite(momentum_score(close, 30, 10))
    gappy = close.copy()
    gappy[25:28] = np.nan
    assert not gap_ok(gappy, 30, 10, max_missing=2)
    assert not np.isfinite(momentum_score(gappy, 30, 10))
    future = close.copy()
    score = momentum_score(future, 30, 10)
    future[31:] = 10_000
    assert momentum_score(future, 30, 10) == pytest.approx(score)


def test_residual_score_does_not_use_the_next_close():
    day_ms = _days(120)
    btc = 100 * np.exp(np.cumsum(np.full(120, 0.001)))
    alt = btc * (1 + np.linspace(0, 0.2, 120))
    other = btc.copy()
    panel = panel_from_frames(
        pd.concat(
            [
                _frame("BTCUSDT", day_ms, btc),
                _frame("ALTUSDT", day_ms, alt),
                _frame("FLATUSDT", day_ms, other),
            ],
            ignore_index=True,
        )
    )
    features = Features(panel)
    index = features.sym["ALTUSDT"]
    params = {"beta_days": 40, "resid_days": 10, "vol_days": 30}
    score = _residual(features, index, 80, params)
    assert np.isfinite(score)
    features.close[index, 81:] *= 3
    # Returns are precomputed. Recompute them the way a fresh panel would, up to day 80 only.
    fresh = panel_from_frames(
        pd.concat(
            [
                _frame("BTCUSDT", day_ms, btc),
                _frame("ALTUSDT", day_ms, alt),
                _frame("FLATUSDT", day_ms, other),
            ],
            ignore_index=True,
        )
    )
    fresh.close[fresh.symbols.index("ALTUSDT"), 81:] *= 3
    # Rebuild returns on a features object whose future was shocked before indicators.
    shocked = panel_from_frames(
        {
            "BTCUSDT": _frame("BTCUSDT", day_ms, btc).drop(columns=["symbol"]),
            "ALTUSDT": _frame("ALTUSDT", day_ms, np.where(np.arange(120) <= 80, alt, alt * 3)).drop(columns=["symbol"]),
            "FLATUSDT": _frame("FLATUSDT", day_ms, other).drop(columns=["symbol"]),
        }
    )
    shocked_features = Features(shocked)
    assert _residual(shocked_features, shocked_features.sym["ALTUSDT"], 80, params) == pytest.approx(score, rel=1e-9)


def test_equal_risk_cluster_cap_and_leverage_cap():
    equity = 10_000.0
    rows = [
        {"symbol": f"A{i}USDT", "side": 1, "stop_pct": 0.10, "cluster": "alts", "risk_fraction": 0.02, "score": 1}
        for i in range(4)
    ]
    sized = notionals_from_risk(rows, equity)
    # 4 alts at 2% request 8%. The alt cluster caps that at 3% before the 6% total.
    assert sum(item.notional * item.stop_pct for item in sized) == pytest.approx(0.03 * equity, rel=1e-6)
    # 3x times a 0.2% stop is 0.6% of equity, above the 0.05% portfolio floor.
    tiny = [{"symbol": "TINYUSDT", "side": 1, "stop_pct": 0.002, "cluster": "btc", "risk_fraction": 0.02, "score": 1}]
    capped = notionals_from_risk(tiny, equity)
    assert capped[0].notional == pytest.approx(3 * equity)
    balanced = [
        {"symbol": "L1", "side": 1, "stop_pct": 0.10, "cluster": "alts", "raw_weight": 1.0, "risk_fraction": 0.02},
        {"symbol": "L2", "side": 1, "stop_pct": 0.10, "cluster": "alts", "raw_weight": 1.0, "risk_fraction": 0.02},
        {"symbol": "S1", "side": -1, "stop_pct": 0.10, "cluster": "alts", "raw_weight": 1.0, "risk_fraction": 0.02},
        {"symbol": "S2", "side": -1, "stop_pct": 0.10, "cluster": "alts", "raw_weight": 1.0, "risk_fraction": 0.02},
    ]
    legs = inverse_vol_balanced(balanced, equity)
    long_notional = sum(item.notional for item in legs if item.side > 0)
    short_notional = sum(item.notional for item in legs if item.side < 0)
    assert long_notional == pytest.approx(short_notional)
    assert sum(item.notional * item.stop_pct for item in legs) <= 0.03 * equity + 1e-6
    greedy = greedy_fill(
        [
            {"symbol": "H", "side": 1, "stop_pct": 0.10, "cluster": "btc", "risk_fraction": 0.02, "score": 3},
            {"symbol": "M", "side": -1, "stop_pct": 0.10, "cluster": "eth", "risk_fraction": 0.02, "score": 2},
            {"symbol": "L", "side": 1, "stop_pct": 0.10, "cluster": "alts", "risk_fraction": 0.02, "score": 1},
        ],
        equity,
    )
    assert {item.symbol for item in greedy} == {"H", "M", "L"}
    assert sum(item.notional * item.stop_pct for item in greedy) == pytest.approx(0.06 * equity)


def test_sunday_signal_fills_monday_open_not_sunday_close():
    n = 40
    day_ms = _days(n)
    close_a = np.array([80.0 + i for i in range(n)], dtype=float)
    close_c = np.array([160.0 - i for i in range(n)], dtype=float)
    close_b = np.full(n, 100.0)
    # After the Sunday decision the path is flat, and Monday opens at the flat price,
    # not at Sunday's close. Buying Sunday's close would book that gap.
    for series, sunday in ((close_a, 21),):
        series[sunday + 1 :] = 90.0
    close_c[22:] = 110.0
    frames = [
        _frame("AAAUSDT", day_ms, close_a),
        _frame("BBBUSDT", day_ms, close_b),
        _frame("CCCUSDT", day_ms, close_c),
    ]
    frames[0].loc[frames[0].index >= 22, "open"] = 90.0
    frames[2].loc[frames[2].index >= 22, "open"] = 110.0
    panel = panel_from_frames(pd.concat(frames, ignore_index=True))
    features = Features(panel)
    params = {
        "top_n": 3,
        "min_history": 15,
        "min_avg_quote": 0.0,
        "min_oi_value": 0.0,
        "lookback": 10,
        "size_mode": "equal_risk",
        "side_fraction": 0.5,
        "min_side": 1,
        "fee_rate": 0.0005,
        "slip_top": 0.0004,
        "slip_rest": 0.0008,
        "vol_window": 10,
    }
    # The decision itself must not change if Monday's prices are shocked.
    intents = decide_e1(features, 21, {}, params)
    shocked = panel_from_frames(pd.concat(frames, ignore_index=True))
    shocked.close[:, 22:] *= 5
    shocked_features = Features(shocked)
    shocked_intents = decide_e1(shocked_features, 21, {}, params)
    assert [(item.symbol, item.side) for item in intents] == [(item.symbol, item.side) for item in shocked_intents]
    result = simulate(features, "e1", params, int(day_ms[21]), int(day_ms[30]), 10_000.0)
    longs = [trade for trade in result.trades if trade["side"] == "long"]
    assert longs
    # Entered at 90 and marked near 90. Entering at Sunday's close (~101) would lose hundreds.
    assert longs[0]["pnl"] > -150


def test_long_pays_positive_funding():
    n = 20
    day_ms = _days(n)
    close = np.full(n, 100.0)
    frame = _frame("BTCUSDT", day_ms, close)
    funding = pd.DataFrame(
        {
            "symbol": ["BTCUSDT"],
            "funding_time": [int(day_ms[8] + 8 * 3_600_000)],
            "funding_rate": [0.01],
            "interval_hours": [8.0],
        }
    )
    panel = panel_from_frames(frame, funding=funding)
    features = Features(panel)

    def enter_once(features, i, held, params):
        if held or i != 7:
            return None
        from desk.strategies.signals import Intent

        return [
            Intent(
                symbol="BTCUSDT",
                side=1,
                size_mode="fixed",
                raw_weight=1.0,
                exit_mode="rebalance",
                trail_mult=None,
                cluster="btc",
                reset_entry=True,
            )
        ]

    from desk.strategies.signals import DECISIONS

    DECISIONS["enter_once"] = enter_once
    try:
        result = simulate(features, "enter_once", {"fee_rate": 0.0, "slip_top": 0.0, "slip_rest": 0.0}, int(day_ms[0]), int(day_ms[12]), 10_000.0)
    finally:
        DECISIONS.pop("enter_once", None)
    # 1x notional, 1% funding, long pays.
    assert result.funding_pnl == pytest.approx(-100.0, abs=1.0)
    assert result.end_equity < 10_000


def test_selection_does_not_promote_an_in_sample_winner_with_negative_oos():
    votes = ["challenger"] * 4 + ["primary"]
    pooled = {
        "challenger": {"total_return": -0.02, "sharpe": -0.4},
        "primary": {"total_return": -0.01, "sharpe": -0.1},
    }
    frozen = choose_frozen(votes, pooled, "primary")
    assert frozen["frozen"] == "primary"
    assert "OOS" in frozen["reason"]
    positive = choose_frozen(
        votes,
        {"challenger": {"total_return": 0.05, "sharpe": 0.4}, "primary": {"total_return": 0.01, "sharpe": 0.1}},
        "primary",
    )
    assert positive["frozen"] == "challenger"
    assert fold_winner(
        [
            {"variant": "primary", "sharpe": 0.2, "invested_days": 40},
            {"variant": "other", "sharpe": 0.2, "invested_days": 40},
        ],
        "primary",
    ) == "primary"
    assert not holdout_is_positive(0.0)
    assert holdout_is_positive(0.01)
    assert not holdout_is_positive(None)


def test_deflated_sharpe_and_live_mode():
    assert _norm_ppf(0.975) == pytest.approx(1.95996, abs=1e-4)
    rng = np.random.default_rng(0)
    strong = rng.normal(0.01, 0.01, size=400)
    weak = rng.normal(0.0, 0.01, size=400)
    high = deflated_sharpe(strong, [float(np.mean(strong) / np.std(strong, ddof=1) * np.sqrt(365))])
    flat = deflated_sharpe(weak, [0.0])
    assert high is not None and high > 0.95
    assert flat is not None and 0.2 < flat < 0.8
    with pytest.raises(RuntimeError):
        require_research_mode({"mode": "live"})
