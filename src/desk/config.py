"""Load settings.yaml over in-code defaults. No secrets are read here."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def default_settings() -> dict[str, Any]:
    """Defaults used when a test passes a partial override."""
    return {
        "mode": "paper",
        "rules_version": "0.3",
        "indicators": {
            "bb_period": 20,
            "bb_std": 2.0,
            "bb_ddof": 0,
            "ema_periods": [20, 50, 200],
            "rsi_period": 14,
            "atr_period": 14,
            "volume_avg_period": 20,
            "sma_fast": 50,
            "sma_slow": 200,
        },
        "universe": {
            "quote_asset": "USDT",
            "contract_type": "PERPETUAL",
            "min_listing_age_days": 14,
            "min_quote_volume": 5_000_000,
            "max_spread": 0.0015,
            "funding_long_max": 0.0005,
            "funding_short_min": -0.0005,
        },
        "setups": {
            "squeeze_breakout": {
                "enabled": True,
                "bandwidth_lookback": 20,
                "volume_mult": 1.5,
                "trigger_bars": 4,
                "stop_atr_buffer": 0.25,
                "max_stop_atr": 3.0,
                "time_stop_bars": 8,
                "min_daily_bars": 200,
                "risk_pct": 0.02,
                "max_leverage": 3,
            },
            "trend_pullback": {
                "enabled": True,
                "rsi_long": [40, 55],
                "rsi_short": [45, 60],
                "trigger_bars": 8,
                "stop_atr_buffer": 0.25,
                "max_stop_atr": 3.0,
                "time_stop_bars": 12,
                "min_daily_bars": 200,
                "risk_pct": 0.02,
                "max_leverage": 3,
                # Research switches. Defaults reproduce v0.3. See reports/ for the test.
                "min_ema_gap_atr": 0.0,
                "allow_caution": True,
                "stop_swing_bars": 1,
                "require_fast_ema_tag": False,
            },
            "range_reclaim": {
                "enabled": True,
                "range_bars": 12,
                "min_range_bars": 8,
                "range_atr_mult": 1.5,
                "fakeout_lookback": 6,
                "volume_mult": 1.2,
                "trigger_bars": 12,
                "stop_atr_buffer": 0.25,
                "max_stop_atr": 3.0,
                "time_stop_bars": 10,
                "risk_pct": 0.02,
                "max_leverage": 3,
            },
            "meme_momentum": {
                "enabled": True,
                "breakout_lookback": 20,
                "volume_mult": 2.0,
                "trigger_bars": 4,
                "stop_atr_buffer": 0.25,
                "stop_cap_atr": 1.5,
                "max_stop_atr": 3.0,
                "time_stop_bars": 8,
                "hard_max_bars": 16,
                "min_daily_bars_for_bias": 200,
                "risk_pct": 0.01,
                "max_leverage": 1,
                "min_quote_volume": 1_000_000,
                "max_spread": 0.005,
                "max_notional_of_1h_volume": 0.01,
            },
        },
        "exits": {
            "partial_at_r": 2.0,
            "partial_fraction": 0.5,
            "trail_mode": "ema",
            "trail_arm": "immediate",
            "trail_ema_atr_mult": 0.25,
            "trail_close_atr_mult": 1.5,
            "chandelier_atr_mult": 2.0,
            "structure_lookback": 3,
            "structure_atr_buffer": 0.25,
        },
        "risk": {
            "scale": 1.0,
            "max_leverage": 3,
            "max_open_risk_pct": 0.06,
            "max_cluster_risk_pct": 0.03,
            "max_setup_d_risk_pct": 0.02,
            "min_risk_after_cap_pct": 0.005,
            "max_notional_equity_mult": 3.0,
            "max_notional_equity_mult_setup_d": 1.0,
            "maintenance_margin_rate": 0.004,
            "liquidation_atr_buffer": 1.0,
            "daily_loss_pause_pct": -0.04,
            "weekly_loss_pause_pct": -0.08,
            "cooldown_days": 7,
            "min_reward_risk": 2.0,
            "enforce_min_reward_risk": True,
        },
        "costs": {
            "taker_fee_rate": 0.0005,
            "slippage_rate": 0.0001,
            "cost_bump_tp1": True,
        },
        "account": {
            "equity_usdt": 800,
            "use_config_snapshot": True,
            "daily_pnl_pct": 0.0,
            "weekly_pnl_pct": 0.0,
            "news_status": None,
        },
        "blocklist": {"available": True, "symbols": []},
        "webhook": {
            "enabled": False,
            "url": "",
            "timeout_seconds": 5,
            "max_retries": 3,
            "secret_env": "WEBHOOK_SECRET",
            "send_rejects": False,
        },
        "data": {
            "sqlite_path": "data/desk.sqlite",
            "rest_base": "https://fapi.binance.com",
            "request_timeout_seconds": 20,
            "request_pause_seconds": 0.25,
            "kline_limit": 1500,
            "symbols": [],
        },
        "backtest": {
            "initial_equity": 800,
            "allow_assumed_spread": True,
            "assumed_spread_pct": 0.0005,
            "funding_predicted_model": "persist_last_settled",
            "news_status": "ALLOW",
            "entry_on": "close",
            "walk_forward_in_sample_days": 182,
            "walk_forward_out_sample_days": 91,
            "close_at_end": True,
        },
    }


def load_settings(path: str | Path | None = None, overrides: dict | None = None) -> dict[str, Any]:
    """Load YAML, then apply overrides. Missing files fall back to defaults."""
    cfg = default_settings()
    if path is not None:
        file_path = Path(path)
        if file_path.exists():
            loaded = yaml.safe_load(file_path.read_text()) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"settings file must be a mapping: {file_path}")
            cfg = _deep_merge(cfg, loaded)
    if overrides:
        cfg = _deep_merge(cfg, overrides)
    if cfg.get("mode") not in ("paper", "live"):
        raise ValueError("mode must be paper or live")
    return cfg


def assert_no_live_orders(settings: dict) -> None:
    """This build refuses live mode because it has no order path."""
    if settings.get("mode") == "live":
        raise RuntimeError(
            "mode=live is refused. This build has no order placement and no trading keys. "
            "Set mode: paper."
        )
