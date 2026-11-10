"""Hard gates that do not place orders. Missing inputs fail closed."""

from __future__ import annotations

from typing import Any

from desk.rules.onchain import onchain_gate
from desk.rules.regime import SETUP_MEME
from desk.rules.universe import cluster_key, funding_blocks, is_usdt_m_perpetual, listing_age_days, spread_fraction, volume_and_spread_limits
from desk.timeutils import ms_to_utc_iso


def gate(result: str, reason: str, ts_ms: int, **extra: Any) -> dict:
    payload = {"result": result, "reason": reason, "ts": ms_to_utc_iso(ts_ms)}
    payload.update(extra)
    return payload


def _pass(reason: str, ts_ms: int, **extra: Any) -> dict:
    return gate("pass", reason, ts_ms, **extra)


def _fail(reason: str, ts_ms: int, **extra: Any) -> dict:
    return gate("fail", reason, ts_ms, **extra)


def evaluate_gates(
    *,
    setup: str,
    direction: str,
    symbol: str,
    market: dict,
    account: dict,
    open_positions: list[dict],
    size_ok: bool,
    size_reason: str,
    risk_usdt: float | None,
    spread_frac: float | None,
    now_ms: int,
    settings: dict,
    cooldown_until_ms: int | None,
    blocklist_symbols: set[str],
    blocklist_available: bool,
    onchain_raw: dict | None,
    regime_ok: bool,
    regime_name: str | None,
    bias_ok: bool | None,
    stop_ok: bool,
    stop_reason: str,
) -> dict[str, dict]:
    ts = now_ms
    gates: dict[str, dict] = {}

    if is_usdt_m_perpetual(market):
        gates["product"] = _pass("live USDT-M perpetual", ts)
    else:
        gates["product"] = _fail("exchange info cannot confirm a live USDT-M perpetual", ts)

    # This build does not query account margin mode. The packet is isolated-only.
    gates["margin_mode"] = _pass(
        "packet is isolated-only; account margin mode is not queried because this build has no trading key",
        ts,
        confirmed_on_exchange=False,
    )

    age = listing_age_days(market.get("onboard_date_ms"), now_ms)
    min_age = float(settings["universe"]["min_listing_age_days"])
    if age is None:
        gates["listing_age"] = _fail("listing date is unknown", ts)
    elif age < min_age:
        gates["listing_age"] = _fail(f"listed {age:.2f} days, minimum is {min_age}", ts, listing_age_days=age)
    else:
        gates["listing_age"] = _pass(f"listed {age:.2f} days", ts, listing_age_days=age)

    min_vol, max_spread = volume_and_spread_limits(setup, settings)
    quote_vol = market.get("quote_volume_24h")
    if quote_vol is None:
        gates["volume"] = _fail("24h quote volume is missing", ts)
    elif float(quote_vol) < min_vol:
        gates["volume"] = _fail(f"24h quote volume {quote_vol} below {min_vol}", ts)
    else:
        gates["volume"] = _pass(f"24h quote volume {quote_vol}", ts)

    if spread_frac is None:
        gates["spread"] = _fail("the book cannot be read and no allowed spread model applies", ts)
    elif spread_frac > max_spread:
        gates["spread"] = _fail(f"spread {spread_frac:.6f} above {max_spread}", ts, spread=spread_frac)
    else:
        gates["spread"] = _pass(f"spread {spread_frac:.6f}", ts, spread=spread_frac, source=market.get("spread_source"))

    funding_reason = funding_blocks(
        direction,
        market.get("funding_rate"),
        market.get("predicted_funding_rate"),
        settings,
    )
    if funding_reason:
        gates["funding"] = _fail(funding_reason, ts, predicted_source=market.get("funding_predicted_source"))
    else:
        gates["funding"] = _pass("funding inside the sanity band", ts, predicted_source=market.get("funding_predicted_source"))

    if not blocklist_available:
        gates["blocklist"] = _fail("blocklist state cannot be read", ts)
    elif symbol in blocklist_symbols:
        gates["blocklist"] = _fail("symbol is on the auto-blocklist", ts)
    else:
        gates["blocklist"] = _pass("symbol is not on the local blocklist", ts)

    news = account.get("news_status")
    if news is None:
        gates["news"] = _fail("News Watcher status is missing", ts)
    elif str(news).upper() != "ALLOW":
        gates["news"] = _fail(f"News Watcher status is {news}", ts)
    else:
        gates["news"] = _pass("News Watcher ALLOW", ts)

    if regime_name is None:
        gates["regime"] = _fail("BTC regime cannot be computed", ts)
    elif not regime_ok:
        gates["regime"] = _fail(f"regime {regime_name} does not allow {setup} {direction}", ts, regime=regime_name)
    else:
        gates["regime"] = _pass(f"regime {regime_name} allows {setup} {direction}", ts, regime=regime_name)

    if bias_ok is None:
        gates["daily_bias"] = _pass("daily bias not required for this setup", ts)
    elif bias_ok:
        gates["daily_bias"] = _pass("daily EMA 200 bias agrees", ts)
    else:
        gates["daily_bias"] = _fail("daily EMA 200 bias disagrees or daily history is short", ts)

    if stop_ok:
        gates["stop_distance"] = _pass(stop_reason, ts)
    else:
        gates["stop_distance"] = _fail(stop_reason, ts)

    if size_ok and risk_usdt is not None:
        gates["sizing"] = _pass(size_reason, ts)
    else:
        gates["sizing"] = _fail(size_reason, ts)

    if setup == SETUP_MEME:
        chain = onchain_gate(onchain_raw)
        if chain["pass"]:
            gates["on_chain"] = _pass("all on-chain checks PASS", ts, checks=chain["checks"])
        else:
            gates["on_chain"] = _fail(
                "on-chain check FAIL or MISSING; fail closed",
                ts,
                checks=chain["checks"],
                blocking=chain["blocking"],
            )
    else:
        gates["on_chain"] = _pass("not Setup D", ts, checks={})

    if any(p.get("symbol") == symbol for p in open_positions):
        gates["conflicting_position"] = _fail("symbol already has an open position", ts)
    else:
        gates["conflicting_position"] = _pass("no open position on this symbol", ts)

    if cooldown_until_ms is not None and cooldown_until_ms > now_ms:
        gates["cooldown"] = _fail("symbol is inside the 7-day stop-out cooldown", ts, until_ms=cooldown_until_ms)
    else:
        gates["cooldown"] = _pass("no stop-out cooldown", ts)

    equity = account.get("equity_usdt")
    gates["open_risk"] = _risk_cap_gate(
        "open risk",
        equity,
        open_positions,
        risk_usdt,
        float(settings["risk"]["max_open_risk_pct"]),
        ts,
        predicate=lambda p: True,
    )
    this_key = None
    if market.get("base_asset") is not None or symbol:
        from desk.rules.universe import cluster_for

        this_key = cluster_key(cluster_for(symbol, market.get("base_asset")), direction)
    gates["cluster"] = _risk_cap_gate(
        "cluster risk",
        equity,
        open_positions,
        risk_usdt,
        float(settings["risk"]["max_cluster_risk_pct"]),
        ts,
        predicate=lambda p: p.get("cluster_key") == this_key,
    )
    if setup == SETUP_MEME:
        gates["setup_d_cap"] = _risk_cap_gate(
            "setup D risk",
            equity,
            open_positions,
            risk_usdt,
            float(settings["risk"]["max_setup_d_risk_pct"]),
            ts,
            predicate=lambda p: p.get("setup") == SETUP_MEME,
        )
    else:
        gates["setup_d_cap"] = _pass("not Setup D", ts)

    daily = account.get("daily_pnl_pct")
    weekly = account.get("weekly_pnl_pct")
    if daily is None:
        gates["daily_loss"] = _fail("daily P&L cannot be calculated", ts)
    elif float(daily) <= float(settings["risk"]["daily_loss_pause_pct"]):
        gates["daily_loss"] = _fail("daily loss pause", ts, daily_pnl_pct=daily)
    else:
        gates["daily_loss"] = _pass("daily loss inside the limit", ts, daily_pnl_pct=daily)
    if weekly is None:
        gates["weekly_loss"] = _fail("weekly P&L cannot be calculated", ts)
    elif float(weekly) <= float(settings["risk"]["weekly_loss_pause_pct"]):
        gates["weekly_loss"] = _fail("weekly loss pause", ts, weekly_pnl_pct=weekly)
    else:
        gates["weekly_loss"] = _pass("weekly loss inside the limit", ts, weekly_pnl_pct=weekly)

    if setup == SETUP_MEME and size_ok:
        fraction = float(settings["setups"]["meme_momentum"]["max_notional_of_1h_volume"])
        last_1h = market.get("last_1h_quote_volume")
        notional = market.get("_planned_notional")
        if last_1h is None or notional is None:
            gates["participation"] = _fail("Setup D participation cap cannot be computed", ts)
        elif float(notional) > fraction * float(last_1h) + 1e-9:
            gates["participation"] = _fail("notional exceeds 1% of the last 1H volume", ts)
        else:
            gates["participation"] = _pass("notional within 1% of the last 1H volume", ts)
    else:
        gates["participation"] = _pass("participation cap is Setup D only", ts)

    return gates


def _risk_cap_gate(
    name: str,
    equity: float | None,
    open_positions: list[dict],
    new_risk: float | None,
    cap_pct: float,
    ts: int,
    predicate,
) -> dict:
    if equity is None or equity <= 0 or new_risk is None:
        return _fail(f"{name} cannot be calculated", ts)
    existing = 0.0
    for pos in open_positions:
        if predicate(pos):
            existing += float(pos.get("risk_usdt") or 0.0)
    total = existing + float(new_risk)
    cap = cap_pct * float(equity)
    if total > cap + 1e-9:
        return _fail(f"{name} {total:.4f} exceeds {cap:.4f}", ts, total_risk=total, cap=cap)
    return _pass(f"{name} {total:.4f} within {cap:.4f}", ts, total_risk=total, cap=cap)


def fresh_account_blocks(
    packet: dict,
    account: dict | None,
    open_positions: list[dict],
    settings: dict,
    now_ms: int,
) -> list[str]:
    """Re-check vetoes and caps that can change after the packet was built.

    The packet's stored gates are not rewritten here. A later master_decision
    cannot clear a name this function returns. Setup-specific caps stay in this
    module; delivery and the paper loop only consume the names.
    """
    if packet.get("packet_kind") in ("rebalance", "book_entry"):
        from desk.paper.books import book_fresh_blocks

        return book_fresh_blocks(packet, account, open_positions, settings, now_ms)
    blocks: list[str] = []
    equity = _as_float(None if account is None else account.get("equity_usdt"))
    risk = _as_float(packet.get("risk_usdt"))
    news = None if account is None else account.get("news_status")
    if news is None or str(news).upper() != "ALLOW":
        blocks.append("news")
    if equity is None or equity <= 0 or risk is None:
        blocks.extend(["open_risk", "cluster"])
    else:
        opened = _risk_cap_gate(
            "open risk",
            equity,
            open_positions,
            risk,
            float(settings["risk"]["max_open_risk_pct"]),
            now_ms,
            predicate=lambda p: True,
        )
        if opened["result"] != "pass":
            blocks.append("open_risk")
        this_key = packet.get("cluster_key")
        cluster = _risk_cap_gate(
            "cluster risk",
            equity,
            open_positions,
            risk,
            float(settings["risk"]["max_cluster_risk_pct"]),
            now_ms,
            predicate=lambda p: p.get("cluster_key") == this_key,
        )
        if cluster["result"] != "pass":
            blocks.append("cluster")
        if packet.get("setup") == SETUP_MEME:
            capped = _risk_cap_gate(
                "setup D risk",
                equity,
                open_positions,
                risk,
                float(settings["risk"]["max_setup_d_risk_pct"]),
                now_ms,
                predicate=lambda p: p.get("setup") == SETUP_MEME,
            )
            if capped["result"] != "pass":
                blocks.append("setup_d_cap")
    daily = _as_float(None if account is None else account.get("daily_pnl_pct"))
    weekly = _as_float(None if account is None else account.get("weekly_pnl_pct"))
    if daily is None or daily <= float(settings["risk"]["daily_loss_pause_pct"]):
        blocks.append("daily_loss")
    if weekly is None or weekly <= float(settings["risk"]["weekly_loss_pause_pct"]):
        blocks.append("weekly_loss")
    chain = (packet.get("hard_gates") or {}).get("on_chain")
    if isinstance(chain, dict) and chain.get("result") != "pass":
        blocks.append("on_chain")
    return blocks


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def all_passed(gates: dict[str, dict]) -> bool:
    return all(item.get("result") == "pass" for item in gates.values())


def passed_ids(gates: dict[str, dict]) -> list[str]:
    return [name for name, item in gates.items() if item.get("result") == "pass"]


def first_failure(gates: dict[str, dict]) -> str | None:
    for name, item in gates.items():
        if item.get("result") != "pass":
            return name
    return None


def resolve_spread(market: dict, settings: dict, *, historical: bool) -> tuple[float | None, str]:
    bid = market.get("bid")
    ask = market.get("ask")
    measured = spread_fraction(None if bid is None else float(bid), None if ask is None else float(ask))
    if measured is not None:
        return measured, "book"
    if historical and settings["backtest"].get("allow_assumed_spread"):
        return float(settings["backtest"]["assumed_spread_pct"]), "assumed"
    return None, "missing"
