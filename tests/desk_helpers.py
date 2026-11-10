"""Shared builders for the delivery and paper tests."""

from __future__ import annotations

from desk.config import load_settings
from desk.scanner.signals import empty_market_context, empty_master_decision
from desk.timeutils import HOUR_MS, ms_to_myt_iso, ms_to_utc_iso


def make_settings(tmp_path, **overrides):
    base = {
        "journal": {"jsonl_path": str(tmp_path / "journal.jsonl")},
        "paper": {
            "initial_equity": 800,
            "clock_days": 14,
            "max_data_age_seconds": 5400,
            "kill_switch_path": str(tmp_path / "KILL"),
            "loop_seconds": 60,
        },
        "data": {"sqlite_path": str(tmp_path / "desk.sqlite"), "symbols": ["BTCUSDT"]},
        "account": {
            "news_status": "ALLOW",
            "use_config_snapshot": True,
            "equity_usdt": 800,
            "daily_pnl_pct": 0.0,
            "weekly_pnl_pct": 0.0,
        },
        "webhook": {"enabled": False},
        "decision": {"timeout_seconds": 900, "timeout_action": "skip"},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged = dict(base[key])
            merged.update(value)
            base[key] = merged
        else:
            base[key] = value
    return load_settings(overrides=base)


def account_view(equity=800.0, news="ALLOW"):
    return {
        "equity_usdt": equity,
        "daily_pnl_pct": 0.0,
        "weekly_pnl_pct": 0.0,
        "news_status": news,
    }


def market_context(now_ms: int, **extra):
    context = {
        "summary": "levels are intact",
        "strengtheners": [],
        "weakeners": [],
        "onchain_checks": {},
        "sources": ["reviewer"],
        "ts": ms_to_utc_iso(now_ms),
    }
    context.update(extra)
    return context


def make_packet(
    *,
    signal_id="sig-1",
    symbol="BTCUSDT",
    now_ms=1_700_000_000_000,
    setup="setup_z",
    gates=None,
    entry=100.0,
    stop=95.0,
    size=1.0,
    risk_usdt=5.0,
    cluster_key="alts-long",
):
    stamp = ms_to_utc_iso(now_ms)
    hard_gates = {
        "news": {"result": "pass", "reason": "ok", "ts": stamp},
        "on_chain": {"result": "pass", "reason": "ok", "ts": stamp},
        "open_risk": {"result": "pass", "reason": "ok", "ts": stamp},
        "cluster": {"result": "pass", "reason": "ok", "ts": stamp},
        "daily_loss": {"result": "pass", "reason": "ok", "ts": stamp},
        "weekly_loss": {"result": "pass", "reason": "ok", "ts": stamp},
    }
    if gates:
        hard_gates.update(gates)
    return {
        "signal_id": signal_id,
        "dedupe_key": f"{symbol}|{setup}|long|{now_ms}",
        "ts_utc": stamp,
        "ts_myt": ms_to_myt_iso(now_ms),
        "ts_ms": now_ms,
        "symbol": symbol,
        "venue": "Binance USDT-M",
        "margin_mode": "isolated",
        "setup": setup,
        "setup_timeframe": "4h",
        "entry_timeframe": "1h",
        "direction": "long",
        "entry": entry,
        "stop": stop,
        "tp1": entry + 2 * (entry - stop),
        "tp2": None,
        "atr_4h": 1.0,
        "size_coins": size,
        "notional_usdt": entry * size,
        "leverage": 1,
        "risk_pct": 0.01,
        "risk_scale": 1,
        "risk_usdt": risk_usdt,
        "reward_risk": 2,
        "equity_usdt": 800,
        "funding_rate": 0.0001,
        "liq_price": entry * 0.5,
        "listing_age_days": 30,
        "cluster": cluster_key.split("-")[0],
        "cluster_key": cluster_key,
        "time_stop_bars": 8,
        "time_stop_at": stamp,
        "invalidation": "test",
        "checks_passed": [name for name, gate in hard_gates.items() if gate["result"] == "pass"],
        "on_blocklist": False,
        "hard_gates": hard_gates,
        "votes": {"scanner": {"result": "pass", "reason": "ok", "ts": stamp}},
        "market_context": empty_market_context(),
        "decision_path": "pending_master",
        "master_decision": empty_master_decision(),
        "mode": "paper",
        "rules_followed": None,
        "status": "proposed",
        "rules_version": "0.3",
        "spread_frac": 0.0005,
        "partial_fraction": 0.5,
        "hard_max_bars": None,
        "meta": {},
    }


def seed_hour(store, symbol: str, close_ms: int, price: float = 100.0) -> None:
    store.upsert_klines(
        symbol,
        "1h",
        [
            {
                "open_time": close_ms - HOUR_MS + 1,
                "close_time": close_ms,
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": 10,
                "quote_volume": 1000,
            }
        ],
    )
