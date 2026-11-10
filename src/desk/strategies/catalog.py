"""Pre-declared variants. The hold-out is not used to add or drop one of these.

Each family has at most three variants. Axes that the research note listed
but that are not in this table were not searched. That choice is part of
the multiple-testing count: only rows in ``VARIANTS`` are trials.
"""

from __future__ import annotations

# Shared book defaults. A variant overrides the keys it actually changes.
_BOOK = {
    "top_n": 40,
    "min_history": 60,
    "vol_window": 30,
    "min_avg_quote": 20_000_000.0,
    "min_oi_value": 5_000_000.0,
    "fee_rate": 0.0005,
    "slip_top": 0.0004,
    "slip_rest": 0.0008,
    "top_liquid": 10,
}

VARIANTS: list[dict] = [
    {
        "family": "E1",
        "variant": "e1_lb21_equal",
        "primary": True,
        "decide": "e1",
        "params": {**_BOOK, "lookback": 21, "size_mode": "equal_risk", "side_fraction": 0.20, "min_side": 3},
    },
    {
        "family": "E1",
        "variant": "e1_lb30_equal",
        "primary": False,
        "decide": "e1",
        "params": {**_BOOK, "lookback": 30, "size_mode": "equal_risk", "side_fraction": 0.20, "min_side": 3},
    },
    {
        "family": "E1",
        "variant": "e1_lb21_invvol",
        "primary": False,
        "decide": "e1",
        "params": {**_BOOK, "lookback": 21, "size_mode": "inv_vol", "side_fraction": 0.20, "min_side": 3},
    },
    {
        "family": "E2",
        "variant": "e2_r15_b60_v30_top25",
        "primary": True,
        "decide": "e2",
        "params": {
            **_BOOK,
            "top_n": 25,
            "resid_days": 15,
            "beta_days": 60,
            "vol_days": 30,
            "size_mode": "inv_vol",
            "side_fraction": 0.20,
            "min_side": 3,
        },
    },
    {
        "family": "E2",
        "variant": "e2_r10_b60_v30_top25",
        "primary": False,
        "decide": "e2",
        "params": {
            **_BOOK,
            "top_n": 25,
            "resid_days": 10,
            "beta_days": 60,
            "vol_days": 30,
            "size_mode": "inv_vol",
            "side_fraction": 0.20,
            "min_side": 3,
        },
    },
    {
        "family": "E2",
        "variant": "e2_r21_b60_v30_top25",
        "primary": False,
        "decide": "e2",
        "params": {
            **_BOOK,
            "top_n": 25,
            "resid_days": 21,
            "beta_days": 60,
            "vol_days": 30,
            "size_mode": "inv_vol",
            "side_fraction": 0.20,
            "min_side": 3,
        },
    },
    {
        "family": "E3",
        "variant": "e3a_sma50_trail3_top40",
        "primary": True,
        "decide": "e3a",
        "params": {**_BOOK, "sma_n": 50, "trail_mult": 3.0, "majors_only": False, "risk_fraction": 0.02},
    },
    {
        "family": "E3",
        "variant": "e3a_sma50_trail3_majors",
        "primary": False,
        "decide": "e3a",
        "params": {**_BOOK, "sma_n": 50, "trail_mult": 3.0, "majors_only": True, "majors_extra": 8, "risk_fraction": 0.02},
    },
    {
        "family": "E3",
        "variant": "e3b_donchian20_trail3_top40",
        "primary": False,
        "decide": "e3b",
        "params": {
            **_BOOK,
            "entry_n": 20,
            "exit_n": 10,
            "sma_n": 200,
            "trail_mult": 3.0,
            "vol_window_filter": 20,
            "vol_history": 252,
            "vol_decile": 0.90,
            "risk_fraction": 0.02,
        },
    },
    {
        "family": "E4",
        "variant": "e4_funding_7d",
        "primary": True,
        "decide": "e4",
        "params": {**_BOOK, "signal": "funding", "funding_days": 7, "z_filter": False, "size_mode": "equal_risk", "side_fraction": 0.20, "min_side": 3},
    },
    {
        "family": "E4",
        "variant": "e4_premium_7d",
        "primary": False,
        "decide": "e4",
        "params": {**_BOOK, "signal": "premium", "funding_days": 7, "z_filter": False, "size_mode": "equal_risk", "side_fraction": 0.20, "min_side": 3},
    },
    {
        "family": "E4",
        "variant": "e4_funding_z",
        "primary": False,
        "decide": "e4",
        "params": {**_BOOK, "signal": "funding", "funding_days": 7, "z_filter": True, "z_min": 1.0, "size_mode": "equal_risk", "side_fraction": 0.20, "min_side": 3},
    },
    {
        "family": "E5",
        "variant": "e5_daily_trail25",
        "primary": True,
        "decide": "e5",
        "params": {**_BOOK, "min_history": 200, "top_k": 5, "min_side": 3, "trail_mult": 2.5, "vol_target": 0.20, "use_oi": True, "risk_fraction": 0.02},
    },
    {
        "family": "E5",
        "variant": "e5_daily_trail15",
        "primary": False,
        "decide": "e5",
        "params": {**_BOOK, "min_history": 200, "top_k": 5, "min_side": 3, "trail_mult": 1.5, "vol_target": 0.20, "use_oi": True, "risk_fraction": 0.02},
    },
    {
        "family": "E5",
        "variant": "e5_daily_no_oi",
        "primary": False,
        "decide": "e5",
        "params": {**_BOOK, "min_history": 200, "top_k": 5, "min_side": 3, "trail_mult": 2.5, "vol_target": 0.20, "use_oi": False, "risk_fraction": 0.02},
    },
    {
        "family": "E6",
        "variant": "e6a_oi_flush",
        "primary": True,
        "decide": "e6a",
        "params": {
            **_BOOK,
            "shock_atr": 3.0,
            "oi_drop": 0.05,
            "retrace": 0.5,
            "stop_pad_atr": 0.25,
            "target_r": 1.5,
            "time_stop_days": 3,
            "risk_fraction": 0.01,
            "always": ("BTCUSDT", "ETHUSDT"),
        },
    },
    {
        "family": "E6",
        "variant": "e6b_crowded_squeeze",
        "primary": False,
        "decide": "e6b",
        "params": {
            **_BOOK,
            "min_history": 200,
            "range_n": 20,
            "near_pct": 0.05,
            "oi_z": 1.5,
            "funding_z": 1.5,
            "stop_atr": 2.0,
            "trail_mult": 2.5,
            "target_r": 3.0,
            "time_stop_days": 15,
            "risk_fraction": 0.02,
        },
    },
]

FAMILY_CONFIG = {
    "E1": "e1_cs_momentum",
    "E2": "e2_residual_momentum",
    "E3": "e3_slow_trend",
    "E4": "e4_funding_carry",
    "E5": "e5_positioning_trend",
    "E6": "e6_liquidation_proxy",
}

# What was listed in the research note and deliberately not estimated.
NOT_SEARCHED = {
    "E1": "Scott Phillips 49-day regression × R², and the N=25/60 universe, were not estimated.",
    "E2": "Beta 45/90 and vol 20 were not estimated. Residual horizon is the only searched axis. Top-40 was not crossed in.",
    "E3": "Trail 2× and 4×, the BTC 50-day gate, and the 55-day Donchian were not estimated. The exit multiple is frozen at 3.",
    "E4": "The daily-rebalance cost stress was not estimated. The headline is the weekly book.",
    "E5": "The 30% and 40% vol targets were not estimated. The book is targeted at 20% and then scaled down to the caps. No BTC hedge overlay.",
    "E6": "No third knob. The two variants are the pre-registered E6-A and E6-B rules.",
}


def family_variants(family: str) -> list[dict]:
    return [row for row in VARIANTS if row["family"] == family]


def primary_variant(family: str) -> dict:
    rows = [row for row in family_variants(family) if row["primary"]]
    if len(rows) != 1:
        raise ValueError(f"{family} needs exactly one primary variant")
    return rows[0]


def variant_by_id(variant: str) -> dict:
    for row in VARIANTS:
        if row["variant"] == variant:
            return row
    raise KeyError(variant)
