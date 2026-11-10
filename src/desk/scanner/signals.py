"""Build and validate the v0.3 signal packet.

Market Bot does not vote. votes contains only the scanner pass/fail.
market_context is an empty placeholder for Market Bot to fill later.
master_decision is an empty placeholder; this process never sets open or skip.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np

from desk.rules.exits import invalidation_text
from desk.rules.risk_gates import all_passed, evaluate_gates, first_failure, passed_ids, resolve_spread
from desk.rules.sizing import plan_position
from desk.rules.universe import cluster_for, cluster_key
from desk.timeutils import HOUR_MS, ms_to_myt_iso, ms_to_utc_iso

REQUIRED_FIELDS = (
    "signal_id",
    "ts_utc",
    "ts_myt",
    "symbol",
    "venue",
    "margin_mode",
    "setup",
    "setup_timeframe",
    "entry_timeframe",
    "direction",
    "entry",
    "stop",
    "tp1",
    "tp2",
    "atr_4h",
    "size_coins",
    "notional_usdt",
    "leverage",
    "risk_pct",
    "risk_scale",
    "risk_usdt",
    "reward_risk",
    "equity_usdt",
    "funding_rate",
    "liq_price",
    "listing_age_days",
    "cluster",
    "cluster_key",
    "time_stop_bars",
    "time_stop_at",
    "invalidation",
    "checks_passed",
    "on_blocklist",
    "hard_gates",
    "votes",
    "market_context",
    "decision_path",
    "master_decision",
    "mode",
    "rules_followed",
    "status",
    "rules_version",
)


class SchemaError(ValueError):
    pass


def empty_market_context() -> dict:
    return {
        "summary": None,
        "strengtheners": [],
        "weakeners": [],
        "onchain_checks": {},
        "sources": [],
        "ts": None,
    }


def empty_master_decision() -> dict:
    return {"decision": None, "reasoning": None, "ts": None}


def signal_id_for(symbol: str, setup: str, direction: str, setup_open_time: int, trigger_open_time: int) -> str:
    raw = f"{symbol}|{setup}|{direction}|{setup_open_time}|{trigger_open_time}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def dedupe_key(symbol: str, setup: str, direction: str, setup_open_time: int) -> str:
    return f"{symbol}|{setup}|{direction}|{setup_open_time}"


def validate_packet(packet: dict, *, scanner_output: bool = False) -> None:
    missing = [name for name in REQUIRED_FIELDS if name not in packet]
    if missing:
        raise SchemaError(f"missing fields: {', '.join(missing)}")
    votes = packet["votes"]
    if "market_bot" in votes:
        raise SchemaError("votes.market_bot was removed; Market Bot does not cast a yes/no vote")
    if "scanner" not in votes:
        raise SchemaError("votes.scanner is required")
    scanner = votes["scanner"]
    if scanner.get("result") not in ("pass", "fail"):
        raise SchemaError("votes.scanner.result must be pass or fail")
    context = packet["market_context"]
    for key in ("summary", "strengtheners", "weakeners", "onchain_checks", "sources", "ts"):
        if key not in context:
            raise SchemaError(f"market_context.{key} is required")
    if scanner_output:
        if context != empty_market_context():
            raise SchemaError("scanner must leave market_context empty")
    decision = packet["master_decision"]
    for key in ("decision", "reasoning", "ts"):
        if key not in decision:
            raise SchemaError(f"master_decision.{key} is required")
    if decision["decision"] not in (None, "open", "skip"):
        raise SchemaError("master_decision.decision must be open, skip, or null")
    if scanner_output and decision != empty_master_decision():
        raise SchemaError("scanner must leave master_decision empty")
    if packet["margin_mode"] != "isolated":
        raise SchemaError("margin_mode must be isolated")
    if packet["mode"] not in ("paper", "live"):
        raise SchemaError("mode must be paper or live")


def compose_signal(
    *,
    candidate,
    trigger_bar,
    entry: float,
    stop: float | None,
    stop_ok: bool,
    stop_reason: str,
    market: dict,
    account: dict,
    open_positions: list[dict],
    settings: dict,
    now_ms: int,
    cooldown_until_ms: int | None,
    blocklist_symbols: set[str],
    blocklist_available: bool,
    onchain_raw: dict | None,
    regime_ok: bool,
    regime_name: str | None,
    bias_ok: bool | None,
    historical: bool,
) -> dict:
    spread, spread_source = resolve_spread(market, settings, historical=historical)
    market = dict(market)
    market["spread_source"] = spread_source
    atr = float(candidate.atr)
    plan = None
    if stop is None or not stop_ok:
        size_ok = False
        size_reason = stop_reason or "stop could not be computed"
        risk_usdt = None
    else:
        plan = plan_position(
            equity=float(account.get("equity_usdt") or 0),
            entry=entry,
            stop=stop,
            direction=candidate.direction,
            setup=candidate.setup,
            atr=atr,
            spread_frac=0.0 if spread is None else spread,
            settings=settings,
            last_1h_quote_volume=market.get("last_1h_quote_volume"),
        )
        size_ok = plan.ok
        size_reason = plan.reason
        risk_usdt = plan.risk_usdt
        stop_ok = True
        stop_reason = "stop inside the ATR cap"
        if plan.reason.startswith("stop is farther"):
            size_ok = False
            stop_ok = False
            stop_reason = plan.reason
    if plan is not None and plan.notional_usdt is not None:
        market["_planned_notional"] = plan.notional_usdt

    gates = evaluate_gates(
        setup=candidate.setup,
        direction=candidate.direction,
        symbol=candidate.symbol,
        market=market,
        account=account,
        open_positions=open_positions,
        size_ok=size_ok and stop_ok,
        size_reason=size_reason,
        risk_usdt=risk_usdt,
        spread_frac=spread,
        now_ms=now_ms,
        settings=settings,
        cooldown_until_ms=cooldown_until_ms,
        blocklist_symbols=blocklist_symbols,
        blocklist_available=blocklist_available,
        onchain_raw=onchain_raw,
        regime_ok=regime_ok,
        regime_name=regime_name,
        bias_ok=bias_ok,
        stop_ok=stop_ok,
        stop_reason=stop_reason,
    )
    passed = all_passed(gates)
    cluster = cluster_for(candidate.symbol, market.get("base_asset"))
    time_stop_at = ms_to_utc_iso(now_ms + candidate.time_stop_bars * 4 * HOUR_MS)
    age = None
    if market.get("onboard_date_ms") is not None:
        age = (now_ms - int(market["onboard_date_ms"])) / 86_400_000
    funding_rate = market.get("predicted_funding_rate")
    if funding_rate is None:
        funding_rate = market.get("funding_rate")
    packet = {
        "signal_id": signal_id_for(
            candidate.symbol,
            candidate.setup,
            candidate.direction,
            candidate.setup_open_time,
            int(trigger_bar["open_time"]),
        ),
        "dedupe_key": dedupe_key(
            candidate.symbol, candidate.setup, candidate.direction, candidate.setup_open_time
        ),
        "ts_utc": ms_to_utc_iso(now_ms),
        "ts_myt": ms_to_myt_iso(now_ms),
        "ts_ms": now_ms,
        "symbol": candidate.symbol,
        "venue": "Binance USDT-M",
        "margin_mode": "isolated",
        "setup": candidate.setup,
        "setup_timeframe": "4h",
        "entry_timeframe": "1h",
        "direction": candidate.direction,
        "entry": entry,
        "stop": None if stop is None else stop,
        "tp1": None if plan is None else plan.tp1,
        "tp2": None,
        "atr_4h": atr,
        "size_coins": None if plan is None else plan.size_coins,
        "notional_usdt": None if plan is None else plan.notional_usdt,
        "leverage": None if plan is None else plan.leverage,
        "risk_pct": None if plan is None else plan.risk_pct,
        "risk_scale": float(settings["risk"]["scale"]),
        "risk_usdt": None if plan is None else plan.risk_usdt,
        "reward_risk": None if plan is None else plan.reward_risk,
        "equity_usdt": account.get("equity_usdt"),
        "funding_rate": funding_rate,
        "liq_price": None if plan is None else plan.liq_price,
        "listing_age_days": age,
        "cluster": cluster,
        "cluster_key": cluster_key(cluster, candidate.direction),
        "time_stop_bars": candidate.time_stop_bars,
        "time_stop_at": time_stop_at,
        "invalidation": candidate.invalidation or invalidation_text(candidate.setup, candidate.direction),
        "checks_passed": passed_ids(gates),
        "on_blocklist": candidate.symbol in blocklist_symbols,
        "hard_gates": gates,
        "votes": {
            "scanner": {
                "result": "pass" if passed else "fail",
                "reason": "setup, trigger, and hard gates passed" if passed else f"failed {first_failure(gates)}",
                "ts": ms_to_utc_iso(now_ms),
            }
        },
        "market_context": empty_market_context(),
        "decision_path": "pending_master" if passed else "hard_gate_reject",
        "master_decision": empty_master_decision(),
        "mode": "paper" if settings.get("mode") != "live" else "live",
        "rules_followed": None,
        "status": "proposed" if passed else "rejected",
        "rules_version": settings.get("rules_version", "0.3"),
        "setup_open_time": candidate.setup_open_time,
        "trigger_open_time": int(trigger_bar["open_time"]),
        "meta": candidate.meta,
        "spread_frac": spread,
        "spread_source": spread_source,
        "hard_max_bars": candidate.hard_max_bars,
        "partial_at_r": float(settings["exits"]["partial_at_r"]),
        "partial_fraction": float(settings["exits"]["partial_fraction"]),
    }
    validate_packet(packet, scanner_output=True)
    return packet


def packet_json(packet: dict) -> str:
    return json.dumps(packet, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(value: Any):
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    raise TypeError(type(value))
