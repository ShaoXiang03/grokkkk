"""Paper scorecard. Expectancy is mean R, the same definition as the backtester."""

from __future__ import annotations

from desk.backtest.metrics import summarize_trades
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
    }
