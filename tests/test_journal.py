"""Append-only decision journal and the Portfolio Journal export."""

import json

from desk.data.store import Store
from desk.journal.decision import (
    DecisionJournal,
    export_portfolio,
    write_portfolio_csv,
    write_portfolio_json,
)
from desk_helpers import make_packet, make_settings

NOW = 1_700_000_000_000


def test_journal_is_append_only_and_exports_csv_and_json(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(tmp_path / "desk.sqlite")
    journal = DecisionJournal(store, settings)
    packet = make_packet(now_ms=NOW)
    store.save_signal(packet)
    journal.record_signal(packet, NOW)
    journal.record_event(
        NOW + 1,
        packet["signal_id"],
        "context",
        {"market_context": {"summary": "held the level", "strengtheners": [], "weakeners": [], "onchain_checks": {}, "sources": [], "ts": "t"}},
    )
    journal.record_event(
        NOW + 2,
        packet["signal_id"],
        "decision",
        {
            "requested": "open",
            "effective": "open",
            "reasoning": "closed bar held",
            "ts": "t",
            "override_refused": False,
            "reason": "reviewer_open",
            "blocking": [],
        },
    )
    journal.record_event(
        NOW + 3,
        packet["signal_id"],
        "fill",
        {"fill_price": 100.035, "size": 1, "fee": 0.05, "stop": 95, "ts_ms": NOW},
    )
    journal.record_event(
        NOW + 4,
        packet["signal_id"],
        "exit",
        {"exit_price": 95, "reason": "hard_stop", "pnl": -6.2, "r_multiple": -1.1, "fees": 0.1, "funding": -0.01, "ts_ms": NOW + 4},
    )
    journal.record_event(
        NOW + 5,
        packet["signal_id"],
        "violation",
        {"kind": "hard_gate_override", "detail": "blocked", "class": "violation"},
    )
    rows = store.read_decision_journal()
    assert [row["event"] for row in rows] == ["signal", "gates", "context", "decision", "fill", "exit", "violation"]
    assert rows[0]["payload"]["packet"]["hard_gates"]["news"]["result"] == "pass"
    assert not hasattr(journal, "update")
    assert not hasattr(journal, "delete")
    lines = (tmp_path / "journal.jsonl").read_text().splitlines()
    assert len(lines) == len(rows)
    assert json.loads(lines[0])["event"] == "signal"

    exported = export_portfolio(store)
    assert len(exported) == 1
    record = exported[0]
    assert record["signal_id"] == packet["signal_id"]
    assert record["setup"] == "setup_z"
    assert record["effective_decision"] == "open"
    assert record["reasoning"] == "closed bar held"
    assert record["context_summary"] == "held the level"
    assert record["fill_price"] == 100.035
    assert record["exit_reason"] == "hard_stop"
    assert record["pnl"] == -6.2
    assert record["r_multiple"] == -1.1
    assert record["rule_violations"] == 1
    assert record["gate_failures"] == ""

    csv_path = tmp_path / "portfolio.csv"
    json_path = tmp_path / "portfolio.json"
    write_portfolio_csv(exported, csv_path)
    write_portfolio_json(exported, json_path)
    header = csv_path.read_text().splitlines()[0]
    assert "signal_id" in header
    assert "expectancy" not in header
    assert "reasoning" in header
    loaded = json.loads(json_path.read_text())
    assert loaded[0]["exit_reason"] == "hard_stop"
    store.close()
