"""Paper scorecard. Expectancy is mean R, the same definition as the backtester.

The book section reports each live book and the pair. Drift compares that
path with the hold-out rates stored on the strategy: turnover, slippage, and
return. A gap wider than the configured band is written to the journal once
per change, not on every cycle.
"""

from __future__ import annotations

from desk.backtest.metrics import summarize_trades
from desk.paper.books import book_actuals, enabled_specs
from desk.paper.clock import paper_clock


def build_scorecard(store, settings: dict, now_ms: int) -> dict:
    account = store.paper_account()
    started = None if account is None else account.get("started_at_ms")
    days = int(settings["paper"].get("clock_days") or 14)
    trades = store.list_paper_trades()
    curve = store.equity_curve()
    summary = summarize_trades(trades, curve)
    reviews = store.list_reviews()
    opened = sum(1 for row in reviews if row["effective_decision"] == "open")
    skipped = sum(1 for row in reviews if row["effective_decision"] == "skip")
    timeout_skip = sum(1 for row in reviews if row["reason"] == "decision_timeout")
    reviewer_skip = sum(1 for row in reviews if row["requested_decision"] == "skip")
    reviewer_open = sum(
        1
        for row in reviews
        if row["requested_decision"] == "open" and row["effective_decision"] == "open"
    )
    forced_skip = sum(
        1
        for row in reviews
        if row["requested_decision"] == "open" and row["effective_decision"] == "skip"
    )
    violations = store.list_paper_events("violation")
    safety = store.list_paper_events("safety")
    books = _book_section(store, settings, now_ms)
    return {
        "clock": paper_clock(started, now_ms, days),
        "trades": summary["trade_count"],
        "win_rate": summary["win_rate"],
        "expectancy_r": summary["expectancy"],
        "max_drawdown": summary["max_drawdown"],
        "rule_violations": len(violations),
        "safety_blocks": len(safety),
        "reviewer": {
            "opened": opened,
            "skipped": skipped,
            "reviewer_open": reviewer_open,
            "reviewer_skip": reviewer_skip,
            "forced_skip": forced_skip,
            "timeout_skip": timeout_skip,
        },
        "net_pnl": summary["net_pnl"],
        "books": books,
    }


def compare_drift(actual: dict, expected: dict, bands: dict, elapsed_days: float) -> dict:
    """Absolute and relative gaps versus the hold-out rates, scaled to the paper window."""
    years = max(0.0, float(elapsed_days)) / 365.0
    expected_days = float(expected.get("days") or 0.0)
    expected_turnover = float(expected.get("turnover_per_year") or 0.0) * years
    start = float(expected.get("start_equity") or 0.0)
    slip = float(expected.get("slippage_usdt") or 0.0)
    if start > 0 and expected_days > 0:
        expected_slippage = (slip / start) * (float(elapsed_days) / expected_days)
    else:
        expected_slippage = 0.0
    cagr = expected.get("cagr")
    if cagr is not None and years > 0:
        expected_return = (1.0 + float(cagr)) ** years - 1.0
    else:
        expected_return = 0.0
    turnover_gap = _relative(float(actual.get("turnover") or 0.0), expected_turnover)
    slippage_gap = _relative(float(actual.get("slippage_frac") or 0.0), expected_slippage)
    return_gap = abs(float(actual.get("return") or 0.0) - expected_return)
    ready = float(elapsed_days) >= float(bands.get("min_days") or 0.0) and float(elapsed_days) > 0
    alerts = []
    if ready and expected_turnover > 0 and turnover_gap > float(bands.get("turnover_rel") or 0.5):
        alerts.append("turnover")
    if ready and expected_slippage > 0 and slippage_gap > float(bands.get("slippage_rel") or 0.5):
        alerts.append("slippage")
    if ready and return_gap > float(bands.get("return_abs") or 0.05):
        alerts.append("return")
    return {
        "actual_turnover": float(actual.get("turnover") or 0.0),
        "expected_turnover": expected_turnover,
        "turnover_gap": turnover_gap,
        "actual_slippage_frac": float(actual.get("slippage_frac") or 0.0),
        "expected_slippage_frac": expected_slippage,
        "slippage_gap": slippage_gap,
        "actual_return": float(actual.get("return") or 0.0),
        "expected_return": expected_return,
        "return_gap": return_gap,
        "alerts": alerts,
    }


def note_drift(store, settings: dict, journal, now_ms: int, card: dict) -> list[str]:
    """Journal a drift alert when the set of breaches changes."""
    books = card.get("books") or {}
    alerts = []
    for book, row in books.items():
        if book == "combined" or not isinstance(row, dict):
            continue
        drift = row.get("drift") or {}
        for name in drift.get("alerts") or []:
            alerts.append(f"{book} {name}")
    signature = sorted(alerts)
    meta = store.book_meta("__drift__") or {}
    if meta.get("signature") == signature:
        return signature
    previous = meta.get("signature") or []
    if signature or previous:
        journal.record_event(now_ms, "book-drift", "drift", {"alerts": signature})
    store.save_book_meta("__drift__", {"signature": signature}, now_ms)
    return signature


def _book_section(store, settings: dict, now_ms: int) -> dict:
    actual = book_actuals(store, settings, now_ms)
    bands = (settings.get("paper") or {}).get("drift") or {}
    section = {}
    combined_alerts = []
    for spec in enabled_specs(settings):
        book = spec["book"]
        row = dict(actual["books"].get(book) or {})
        drift = compare_drift(row, spec.get("expected") or {}, bands, actual["elapsed_days"])
        row["drift"] = drift
        section[book] = row
        combined_alerts.extend(f"{book} {name}" for name in drift["alerts"])
    combined = dict(actual["combined"])
    combined["alerts"] = combined_alerts
    section["combined"] = combined
    section["elapsed_days"] = actual["elapsed_days"]
    return section


def _relative(actual: float, expected: float) -> float:
    if expected == 0:
        return 0.0 if actual == 0 else 1.0
    return abs(actual / expected - 1.0)
