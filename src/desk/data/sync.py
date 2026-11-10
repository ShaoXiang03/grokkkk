"""Incremental public-data sync. Historical backfill and a latest-bar refresh."""

from __future__ import annotations

import time

from desk.data.binance import parse_klines
from desk.data.store import Store
from desk.timeutils import DAY_MS, interval_ms


def sync_exchange_info(client, store: Store, now_ms: int) -> int:
    info = client.exchange_info()
    from desk.data.binance import usdt_m_perpetuals

    rows = usdt_m_perpetuals(info, trading_only=False)
    store.upsert_symbols(rows, now_ms)
    return len(rows)


def sync_klines(
    client,
    store: Store,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    limit: int = 1500,
    pause: float = 0.2,
) -> int:
    """Fetch closed-range klines, resuming at the last stored open time."""
    cursor = start_ms
    latest = store.latest_open_time(symbol, interval)
    if latest is not None:
        cursor = max(start_ms, latest)
    step = interval_ms(interval)
    written = 0
    while cursor < end_ms:
        raw = client.klines(symbol, interval, start_ms=cursor, end_ms=end_ms, limit=limit)
        rows = parse_klines(raw)
        if not rows:
            break
        store.upsert_klines(symbol, interval, rows)
        written += len(rows)
        next_cursor = int(rows[-1]["open_time"]) + step
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(rows) < limit:
            break
        if pause:
            time.sleep(pause)
    return written


def sync_funding(client, store: Store, symbol: str, start_ms: int, end_ms: int, *, limit: int = 1000, pause: float = 0.2) -> int:
    cursor = start_ms
    written = 0
    while cursor < end_ms:
        raw = client.funding_history(symbol, start_ms=cursor, end_ms=end_ms, limit=limit)
        rows = [
            {"funding_time": int(item["fundingTime"]), "funding_rate": float(item["fundingRate"])}
            for item in raw
        ]
        if not rows:
            break
        store.upsert_funding(symbol, rows)
        written += len(rows)
        next_cursor = rows[-1]["funding_time"] + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(rows) < limit:
            break
        if pause:
            time.sleep(pause)
    return written


def sync_live_quotes(client, store: Store, symbol: str, now_ms: int) -> None:
    book = client.book_ticker(symbol)
    if isinstance(book, dict) and book.get("bidPrice") is not None:
        store.save_book(symbol, now_ms, float(book["bidPrice"]), float(book["askPrice"]))
    ticker = client.ticker_24h(symbol)
    if isinstance(ticker, dict):
        quote = ticker.get("quoteVolume")
        last = ticker.get("lastPrice")
        store.save_ticker(
            symbol,
            now_ms,
            None if quote is None else float(quote),
            None if last is None else float(last),
        )
    premium = client.premium_index(symbol)
    if isinstance(premium, dict):
        rate = premium.get("lastFundingRate")
        nxt = premium.get("nextFundingTime")
        store.save_funding_snapshot(
            symbol,
            now_ms,
            None if rate is None else float(rate),
            None if nxt is None else int(nxt),
        )


def backfill_symbol(
    client,
    store: Store,
    symbol: str,
    *,
    now_ms: int,
    days: int,
    daily_days: int,
    limit: int,
    pause: float,
) -> dict:
    intraday_start = now_ms - days * DAY_MS
    daily_start = now_ms - daily_days * DAY_MS
    counts = {}
    for interval, start in (("1h", intraday_start), ("4h", intraday_start), ("1d", daily_start)):
        counts[interval] = sync_klines(
            client, store, symbol, interval, start, now_ms, limit=limit, pause=pause
        )
    counts["funding"] = sync_funding(client, store, symbol, intraday_start, now_ms, pause=pause)
    sync_live_quotes(client, store, symbol, now_ms)
    return counts
