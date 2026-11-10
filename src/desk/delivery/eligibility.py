"""Decide whether a reviewer may open a signal.

Hard gates are applied before a signal is eligible. ``open`` is rewritten to
``skip`` when any gate fails, when reasoning is missing, or when equity cannot
be read. A configured timeout always skips. Nothing in this module places an
order, and nothing here lets ``master_decision`` overrule a gate.
"""

from __future__ import annotations

from typing import Any

from desk.journal.decision import DecisionJournal
from desk.rules.risk_gates import fresh_account_blocks
from desk.scanner.signals import empty_market_context
from desk.timeutils import ms_to_utc_iso

CONTEXT_KEYS = ("summary", "strengtheners", "weakeners", "onchain_checks", "sources", "ts")


class ReviewError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def blocking_gates(packet: dict) -> list[str]:
    """Every gate that is not an explicit pass blocks eligibility.

    A missing result fails closed. New setups add gates on the packet; this
    function does not know their names in advance.
    """
    gates = packet.get("hard_gates") or {}
    if not isinstance(gates, dict):
        return ["hard_gates"]
    blocked = []
    for name, gate in gates.items():
        if not isinstance(gate, dict) or gate.get("result") != "pass":
            blocked.append(str(name))
    return blocked


def entry_blocks(
    packet: dict,
    account: dict | None,
    open_positions: list[dict],
    settings: dict,
    now_ms: int,
) -> list[str]:
    names = blocking_gates(packet)
    if account is None:
        if "equity" not in names:
            names.append("equity")
        return names
    for name in fresh_account_blocks(packet, account, open_positions, settings, now_ms):
        if name not in names:
            names.append(name)
    return names


def normalize_context(raw: dict | None, now_ms: int) -> dict:
    if not isinstance(raw, dict):
        raise ReviewError("market_context must be an object")
    missing = [key for key in CONTEXT_KEYS if key not in raw]
    if missing:
        raise ReviewError("market_context missing " + ", ".join(missing))
    if not isinstance(raw["strengtheners"], list) or not isinstance(raw["weakeners"], list):
        raise ReviewError("market_context strengtheners and weakeners must be arrays")
    if not isinstance(raw["sources"], list):
        raise ReviewError("market_context.sources must be an array")
    if not isinstance(raw["onchain_checks"], dict):
        raise ReviewError("market_context.onchain_checks must be an object")
    context = {key: raw[key] for key in CONTEXT_KEYS}
    if context["ts"] is None:
        context["ts"] = ms_to_utc_iso(now_ms)
    return context


def submit_review(
    store,
    settings: dict,
    signal_id: str,
    market_context: dict | None,
    master_decision: dict | None,
    *,
    now_ms: int,
    journal: DecisionJournal | None = None,
    account: dict | None = None,
    open_positions: list[dict] | None = None,
) -> dict:
    """Append one review. A second review for the same signal is refused.

    The returned ``effective_decision`` is what the paper loop may act on.
    Requested ``open`` becomes ``skip`` when a gate, the equity read, or the
    reasoning check fails. ``market_context.onchain_checks`` is stored and is
    not copied onto the scanner gate.
    """
    packet = store.get_signal(signal_id)
    if packet is None:
        raise ReviewError("unknown signal", status=404)
    if store.latest_review(signal_id) is not None:
        raise ReviewError("signal already has a decision", status=409)
    if not isinstance(master_decision, dict):
        raise ReviewError("master_decision must be an object")
    requested = master_decision.get("decision")
    if requested not in ("open", "skip"):
        raise ReviewError("master_decision.decision must be open or skip")
    reasoning = str(master_decision.get("reasoning") or "").strip()
    decision_ts = master_decision.get("ts") or ms_to_utc_iso(now_ms)
    context = normalize_context(market_context, now_ms)
    positions = list(open_positions or [])
    blocks: list[str] = []
    violations: list[tuple[str, str]] = []
    if requested == "open":
        if packet.get("mode") == "live" or packet.get("status") != "proposed":
            blocks.append("not_eligible")
        elif packet.get("decision_path") != "pending_master":
            blocks.append("not_eligible")
        blocks.extend(
            name
            for name in entry_blocks(packet, account, positions, settings, now_ms)
            if name not in blocks
        )
        if not reasoning:
            blocks.append("missing_reasoning")
            violations.append(("missing_reasoning", "open requires reasoning"))
        timeout_ms = int(settings["decision"]["timeout_seconds"]) * 1000
        signal_ts = int(packet.get("ts_ms") or 0)
        if int(now_ms) - signal_ts > timeout_ms:
            blocks.append("decision_timeout")
        gate_blocks = [name for name in blocks if name not in ("missing_reasoning", "decision_timeout")]
        if gate_blocks:
            violations.append(("hard_gate_override", "open blocked by " + ", ".join(gate_blocks)))
    elif not reasoning:
        violations.append(("missing_reasoning", "skip was recorded without reasoning"))

    if requested == "open" and blocks:
        effective = "skip"
        if blocks == ["missing_reasoning"]:
            reason = "missing_reasoning"
        elif "decision_timeout" in blocks and not any(
            name not in ("missing_reasoning", "decision_timeout") for name in blocks
        ):
            reason = "decision_timeout"
        else:
            reason = "hard_gate_blocked"
        override_refused = True
    elif requested == "skip":
        effective = "skip"
        reason = "reviewer_skip" if reasoning else "reviewer_skip_without_reasoning"
        override_refused = False
    else:
        effective = "open"
        reason = "reviewer_open"
        override_refused = False

    record = {
        "signal_id": signal_id,
        "ts_ms": int(now_ms),
        "market_context": context,
        "requested_decision": requested,
        "effective_decision": effective,
        "reasoning": reasoning or None,
        "decision_ts": decision_ts,
        "override_refused": override_refused,
        "reason": reason,
        "blocking": blocks,
    }
    store.append_review(record)
    writer = journal or DecisionJournal(store, settings)
    writer.record_event(now_ms, signal_id, "context", {"market_context": context})
    writer.record_event(
        now_ms,
        signal_id,
        "decision",
        {
            "requested": requested,
            "effective": effective,
            "reasoning": reasoning or None,
            "ts": decision_ts,
            "override_refused": override_refused,
            "reason": reason,
            "blocking": blocks,
        },
    )
    for kind, detail in violations:
        _violation(store, writer, now_ms, signal_id, kind, detail)
    return {
        "ok": True,
        "signal_id": signal_id,
        "requested_decision": requested,
        "effective_decision": effective,
        "override_refused": override_refused,
        "reason": reason,
        "blocking": blocks,
        "market_context": context,
    }


def expire_pending(store, settings: dict, now_ms: int, journal: DecisionJournal | None = None) -> list[dict]:
    """Skip proposed signals that are still undecided after the timeout.

    ``decision.timeout_action`` is ignored. The timeout is always skip.
    """
    timeout_ms = int(settings["decision"]["timeout_seconds"]) * 1000
    writer = journal or DecisionJournal(store, settings)
    expired = []
    for packet in store.list_signal_packets(status="proposed"):
        signal_id = packet["signal_id"]
        if store.latest_review(signal_id) is not None:
            continue
        age = int(now_ms) - int(packet["ts_ms"])
        if age < timeout_ms:
            continue
        decision_ts = ms_to_utc_iso(now_ms)
        record = {
            "signal_id": signal_id,
            "ts_ms": int(now_ms),
            "market_context": empty_market_context(),
            "requested_decision": "timeout",
            "effective_decision": "skip",
            "reasoning": "decision timeout; fail closed",
            "decision_ts": decision_ts,
            "override_refused": False,
            "reason": "decision_timeout",
            "blocking": [],
        }
        store.append_review(record)
        writer.record_event(now_ms, signal_id, "context", {"market_context": record["market_context"]})
        writer.record_event(
            now_ms,
            signal_id,
            "decision",
            {
                "requested": None,
                "effective": "skip",
                "reasoning": record["reasoning"],
                "ts": decision_ts,
                "override_refused": False,
                "reason": "decision_timeout",
                "blocking": [],
            },
        )
        expired.append(record)
    return expired


def _violation(store, journal: DecisionJournal, now_ms: int, signal_id: str, kind: str, detail: str) -> None:
    store.append_paper_event(now_ms, "violation", kind, signal_id, detail)
    journal.record_event(now_ms, signal_id, "violation", {"kind": kind, "detail": detail, "class": "violation"})


def timeout_is_skip(settings: dict) -> bool:
    """The timeout path is skip even if a config file says otherwise."""
    _ = settings
    return True


def public_review(result: dict) -> dict[str, Any]:
    return {
        "ok": result["ok"],
        "signal_id": result["signal_id"],
        "requested_decision": result["requested_decision"],
        "effective_decision": result["effective_decision"],
        "override_refused": result["override_refused"],
        "reason": result["reason"],
        "blocking": result["blocking"],
    }
