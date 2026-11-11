"""When stored bars are fresh enough to open a new paper entry.

E2 and E3 decide on the daily close (00:00 UTC). A 90-minute horizon is the
right gate for a 1h setup and the wrong one for a daily bar: the bar that
closed at midnight is still the current close all day. The gate fails closed
when that close is missing. It does not block a stop; the cycle applies
risk-reducing exits before it asks.
"""

from __future__ import annotations

from desk.timeutils import DAY_MS, last_closed_daily_open


def setups_enabled(settings: dict) -> bool:
    for cfg in (settings.get("setups") or {}).values():
        if isinstance(cfg, dict) and cfg.get("enabled"):
            return True
    return False


def daily_books_enabled(settings: dict) -> bool:
    from desk.paper.books import enabled_specs

    return bool(enabled_specs(settings))


def daily_only(settings: dict) -> bool:
    """E2/E3 (or any enabled daily book) and no intraday setup."""
    return daily_books_enabled(settings) and not setups_enabled(settings)


def _status_by_symbol(store) -> dict[str, str | None]:
    return {row["symbol"]: row.get("status") for row in store.list_symbols()}


def without_delisted(store, symbols: list[str]) -> list[str]:
    """Drop names the archive already marked DELISTED.

    A delisted contract will not print today's bar. Keeping it in the gate
    would block every new entry forever. An unlabeled store (tests, a cache
    that never wrote symbols) keeps every name.
    """
    status = _status_by_symbol(store)
    if not status:
        return list(symbols)
    return [symbol for symbol in symbols if status.get(symbol) != "DELISTED"]


def _has_interval(store, symbol: str, interval: str) -> bool:
    return store.latest_open_time(symbol, interval) is not None


def _age_fresh(store, symbols: list[str], now_ms: int, interval: str, max_age_ms: int) -> tuple[bool, str]:
    if not symbols:
        return False, "no symbols to check"
    stale = []
    for symbol in symbols:
        close_time = store.latest_close_time(symbol, interval, now_ms)
        if close_time is None or int(now_ms) - close_time > max_age_ms:
            stale.append(symbol)
    if stale:
        return False, f"stale or missing closed {interval} bars: " + _join(stale)
    return True, f"closed {interval} bars are fresh"


def _daily_close_fresh(store, symbols: list[str], now_ms: int, settings: dict) -> tuple[bool, str]:
    """Fresh when each name has the daily bar that closed at the last 00:00 UTC.

    `paper.max_data_age_seconds` is slack for the close timestamp (Binance
    stamps close_time one millisecond before midnight). It is not a license
    to trade a bar from the previous month.
    """
    if not symbols:
        return False, "no symbols to check"
    expected_close = last_closed_daily_open(now_ms) + DAY_MS
    grace = int(settings["paper"]["max_data_age_seconds"]) * 1000
    stale = []
    for symbol in symbols:
        close_time = store.latest_close_time(symbol, "1d", now_ms)
        if close_time is None or close_time + grace < expected_close:
            stale.append(symbol)
    if stale:
        return False, "stale or missing closed 1d bars: " + _join(stale)
    return True, "closed 1d bars are fresh"


def data_is_fresh(store, symbols: list[str], now_ms: int, settings: dict) -> tuple[bool, str]:
    """Fail closed for new entries. The bar size follows what is enabled.

    Intraday setups keep the 1h horizon (`max_data_age_seconds`). Daily books
    with those setups off are judged on the last closed daily bar. A daily
    name that is DELISTED is not part of that check.
    """
    intraday = setups_enabled(settings)
    daily = daily_books_enabled(settings)
    max_age_ms = int(settings["paper"]["max_data_age_seconds"]) * 1000
    if intraday:
        ok, reason = _age_fresh(store, symbols, now_ms, "1h", max_age_ms)
        if not ok or not daily:
            return ok, reason
        # Setups and books together. A symbol with no daily history is not a
        # book input. One that has daily bars still has to be current.
        daily_names = [symbol for symbol in symbols if _has_interval(store, symbol, "1d")]
        if not daily_names:
            return True, reason
        return _daily_close_fresh(store, without_delisted(store, daily_names), now_ms, settings)
    if daily:
        return _daily_close_fresh(store, without_delisted(store, list(symbols)), now_ms, settings)
    return _age_fresh(store, symbols, now_ms, "1h", max_age_ms)


def _join(symbols: list[str]) -> str:
    shown = symbols[:8]
    text = ",".join(shown)
    extra = len(symbols) - len(shown)
    if extra > 0:
        text += f",+{extra}"
    return text
