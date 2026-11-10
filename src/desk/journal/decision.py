"""Append-only decision journal.

SQLite is the durable record. Each row is inserted and never updated.
A JSONL file mirrors the same events for export off the box. The mirror is
not a second source of truth: export reads SQLite.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from desk.data.store import Store


class DecisionJournal:
    def __init__(self, store: Store, settings: dict):
        self.store = store
        configured = (settings.get("journal") or {}).get("jsonl_path")
        self.jsonl_path = None if not configured else Path(configured)

    def record_signal(self, packet: dict, ts_ms: int) -> None:
        signal_id = packet["signal_id"]
        self.record_event(ts_ms, signal_id, "signal", {"packet": packet})
        self.record_event(
            ts_ms,
            signal_id,
            "gates",
            {
                "hard_gates": packet.get("hard_gates") or {},
                "decision_path": packet.get("decision_path"),
                "status": packet.get("status"),
            },
        )

    def record_event(self, ts_ms: int, signal_id: str, event: str, payload: dict) -> int:
        row_id = self.store.append_decision_journal(ts_ms, signal_id, event, payload)
        self._mirror(row_id, ts_ms, signal_id, event, payload)
        return row_id

    def _mirror(self, row_id: int, ts_ms: int, signal_id: str, event: str, payload: dict) -> None:
        if self.jsonl_path is None:
            return
        line = {
            "id": row_id,
            "ts_ms": int(ts_ms),
            "signal_id": signal_id,
            "event": event,
            "payload": payload,
        }
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True, default=str) + "\n")


def export_portfolio(store: Store) -> list[dict]:
    """One Portfolio Journal row per signal, built only from the append-only log."""
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in store.read_decision_journal():
        signal_id = row["signal_id"]
        if signal_id not in grouped:
            grouped[signal_id] = []
            order.append(signal_id)
        grouped[signal_id].append(row)
    rows = []
    for signal_id in order:
        events = grouped[signal_id]
        if not any(row["event"] == "signal" for row in events):
            continue
        rows.append(_row_for(signal_id, events))
    return rows


def write_portfolio_json(records: list[dict], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(records, indent=2, default=str) + "\n")


def write_portfolio_csv(records: list[dict], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(PORTFOLIO_COLUMNS)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            flat = {key: record.get(key) for key in fieldnames}
            writer.writerow(flat)


PORTFOLIO_COLUMNS = (
    "signal_id",
    "ts_utc",
    "symbol",
    "setup",
    "direction",
    "status",
    "decision_path",
    "gate_failures",
    "context_summary",
    "requested_decision",
    "effective_decision",
    "decision_reason",
    "reasoning",
    "decision_ts",
    "override_refused",
    "entry",
    "stop",
    "tp1",
    "size_coins",
    "risk_usdt",
    "fill_price",
    "fill_fee",
    "exit_price",
    "exit_reason",
    "pnl",
    "r_multiple",
    "fees",
    "funding",
    "rule_violations",
    "resolution",
)


def _latest(events: list[dict], name: str) -> dict | None:
    found = [row for row in events if row["event"] == name]
    if not found:
        return None
    return found[-1]["payload"]


def _row_for(signal_id: str, events: list[dict]) -> dict:
    signal = _latest(events, "signal") or {}
    packet = signal.get("packet") or {}
    gates_event = _latest(events, "gates") or {}
    gates = gates_event.get("hard_gates") or packet.get("hard_gates") or {}
    failures = [
        name
        for name, gate in gates.items()
        if not isinstance(gate, dict) or gate.get("result") != "pass"
    ]
    context = (_latest(events, "context") or {}).get("market_context") or {}
    decision = _latest(events, "decision") or {}
    fill = _latest(events, "fill") or {}
    exit_event = _latest(events, "exit") or {}
    resolution = _latest(events, "resolution") or {}
    violations = [row for row in events if row["event"] == "violation"]
    return {
        "signal_id": signal_id,
        "ts_utc": packet.get("ts_utc"),
        "symbol": packet.get("symbol"),
        "setup": packet.get("setup"),
        "direction": packet.get("direction"),
        "status": packet.get("status") or gates_event.get("status"),
        "decision_path": packet.get("decision_path") or gates_event.get("decision_path"),
        "gate_failures": ",".join(failures),
        "context_summary": context.get("summary"),
        "requested_decision": decision.get("requested"),
        "effective_decision": decision.get("effective"),
        "decision_reason": decision.get("reason"),
        "reasoning": decision.get("reasoning"),
        "decision_ts": decision.get("ts"),
        "override_refused": decision.get("override_refused"),
        "entry": packet.get("entry"),
        "stop": packet.get("stop"),
        "tp1": packet.get("tp1"),
        "size_coins": packet.get("size_coins"),
        "risk_usdt": packet.get("risk_usdt"),
        "fill_price": fill.get("fill_price"),
        "fill_fee": fill.get("fee"),
        "exit_price": exit_event.get("exit_price"),
        "exit_reason": exit_event.get("reason"),
        "pnl": exit_event.get("pnl"),
        "r_multiple": exit_event.get("r_multiple"),
        "fees": exit_event.get("fees"),
        "funding": exit_event.get("funding"),
        "rule_violations": len(violations),
        "resolution": resolution.get("outcome"),
    }
