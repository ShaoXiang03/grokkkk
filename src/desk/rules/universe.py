"""Quality filters that can be computed from market data, plus local blocklist state."""

from __future__ import annotations

from desk.rules.regime import SETUP_MEME


def spread_fraction(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid


def volume_and_spread_limits(setup: str, settings: dict) -> tuple[float, float]:
    uni = settings["universe"]
    if setup == SETUP_MEME:
        meme = settings["setups"]["meme_momentum"]
        return float(meme["min_quote_volume"]), float(meme["max_spread"])
    return float(uni["min_quote_volume"]), float(uni["max_spread"])


def listing_age_days(onboard_date_ms: int | None, now_ms: int) -> float | None:
    if onboard_date_ms is None:
        return None
    return (now_ms - onboard_date_ms) / 86_400_000


def cluster_for(symbol: str, base_asset: str | None = None) -> str:
    base = (base_asset or "").upper()
    if not base and symbol.upper().endswith("USDT"):
        base = symbol.upper()[: -len("USDT")]
    if base == "BTC":
        return "btc"
    if base == "ETH":
        return "eth"
    return "alts"


def cluster_key(cluster: str, direction: str) -> str:
    return f"{cluster}-{direction}"


def is_usdt_m_perpetual(market: dict) -> bool:
    return (
        str(market.get("contract_type") or "").upper() == "PERPETUAL"
        and str(market.get("quote_asset") or "").upper() == "USDT"
        and str(market.get("status") or "").upper() == "TRADING"
    )


def funding_blocks(direction: str, current: float | None, predicted: float | None, settings: dict) -> str | None:
    """Return a reason string when the funding gate fails, else None."""
    if current is None or predicted is None:
        return "current or predicted funding is missing"
    uni = settings["universe"]
    if direction == "long":
        limit = float(uni["funding_long_max"])
        if current > limit or predicted > limit:
            return f"funding {current:.6f} / predicted {predicted:.6f} above {limit}"
        return None
    limit = float(uni["funding_short_min"])
    if current < limit or predicted < limit:
        return f"funding {current:.6f} / predicted {predicted:.6f} below {limit}"
    return None
