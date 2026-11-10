#!/usr/bin/env python3
"""Download public Binance USDT-M klines and funding into SQLite.

`--source rest` uses fapi.binance.com public GET routes.
`--source vision` uses the public data.binance.vision monthly archives.
No API key is read. No order route is called.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from desk.config import load_settings  # noqa: E402
from desk.data.backfill_vision import backfill_vision_universe  # noqa: E402
from desk.data.binance import BinancePublicClient, usdt_m_perpetuals  # noqa: E402
from desk.data.store import Store  # noqa: E402
from desk.data.sync import backfill_symbol, sync_exchange_info  # noqa: E402
from desk.data.vision import iter_months, load_funding_month, load_klines_month  # noqa: E402
from desk.timeutils import DAY_MS, dt_to_ms  # noqa: E402

# Months to probe for the first archived daily bar. This is not exchangeInfo.onboardDate.
LISTING_PROBES = ("2019-09", "2020-01", "2020-09", "2021-01", "2021-09", "2022-01", "2023-01", "2024-01")


def _vision(store: Store, symbols: list[str], now_ms: int, days: int, daily_days: int, timeout: float) -> None:
    session = requests.Session()
    session.headers["User-Agent"] = "chart-analyst-backfill/0.3"
    intraday_months = iter_months(now_ms - (days + 45) * DAY_MS, now_ms)
    daily_months = iter_months(now_ms - daily_days * DAY_MS, now_ms)
    print(
        "source=data.binance.vision "
        f"intraday_months={intraday_months[0]}..{intraday_months[-1]} "
        f"daily_months={daily_months[0]}..{daily_months[-1]}"
    )
    print("exchangeInfo was not queried. Listing age uses the first archived daily bar found in LISTING_PROBES.")
    for symbol in symbols:
        base = symbol[: -len("USDT")] if symbol.endswith("USDT") else symbol
        onboard = _first_daily_open(session, symbol, timeout)
        counts = {"1h": 0, "4h": 0, "1d": 0, "funding": 0}
        for month in intraday_months:
            for interval in ("1h", "4h"):
                rows = load_klines_month(session, symbol, interval, month, timeout)
                if rows:
                    store.upsert_klines(symbol, interval, rows)
                    counts[interval] += len(rows)
                    print(f"{symbol} {interval} {month}: {len(rows)}")
        for month in daily_months:
            rows = load_klines_month(session, symbol, "1d", month, timeout)
            if rows:
                store.upsert_klines(symbol, "1d", rows)
                counts["1d"] += len(rows)
        for month in intraday_months:
            rows = load_funding_month(session, symbol, month, timeout)
            if rows:
                store.upsert_funding(symbol, rows)
                counts["funding"] += len(rows)
        store.upsert_symbols(
            [
                {
                    "symbol": symbol,
                    "status": "TRADING",
                    "contract_type": "PERPETUAL",
                    "base_asset": base,
                    "quote_asset": "USDT",
                    "margin_asset": "USDT",
                    "onboard_date_ms": onboard,
                }
            ],
            now_ms,
        )
        print(f"{symbol}: {counts} onboard_ms={onboard}")
    session.close()


def _month_start_ms(month: str) -> int:
    year, mon = month.split("-")
    return dt_to_ms(datetime(int(year), int(mon), 1, tzinfo=timezone.utc))


def _previous_month(now_ms: int) -> str:
    dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    year, month = dt.year, dt.month - 1
    if month == 0:
        year -= 1
        month = 12
    return f"{year:04d}-{month:02d}"


def _first_daily_open(session, symbol: str, timeout: float) -> int | None:
    for month in LISTING_PROBES:
        rows = load_klines_month(session, symbol, "1d", month, timeout)
        if rows:
            return int(rows[0]["open_time"])
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill public Binance USDT-M data")
    parser.add_argument("--settings", default=str(ROOT / "config" / "settings.yaml"))
    parser.add_argument("--symbols", nargs="*", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--all-universe", action="store_true", help="Every USDT-M perpetual in exchangeInfo")
    parser.add_argument("--days", type=int, default=120, help="1h and 4h lookback")
    parser.add_argument("--daily-days", type=int, default=400, help="Daily lookback so the 200-day bias can be computed")
    parser.add_argument("--source", choices=("rest", "vision"), default="rest")
    parser.add_argument("--start", default="2019-09", help="First vision month, YYYY-MM. Used with --all-universe")
    parser.add_argument("--end", default="", help="Last vision month, YYYY-MM. Default: previous UTC month")
    parser.add_argument("--workers", type=int, default=12, help="Parallel vision downloads")
    parser.add_argument(
        "--min-daily-quote-volume",
        type=float,
        default=500_000,
        help="Keep symbols whose best UTC day is at least this quote volume (half of the 1M Setup D gate)",
    )
    parser.add_argument("--cache-dir", default=str(ROOT / "data" / "vision-cache"))
    args = parser.parse_args()
    settings = load_settings(args.settings)
    path = Path(settings["data"]["sqlite_path"])
    if not path.is_absolute():
        path = ROOT / path
    store = Store(path)
    now_ms = int(time.time() * 1000)
    symbols = list(args.symbols)
    if args.source == "vision":
        if args.all_universe:
            end_month = args.end or _previous_month(now_ms)
            start_ms = _month_start_ms(args.start)
            end_ms = _month_start_ms(end_month)
            backfill_vision_universe(
                store,
                cache_dir=Path(args.cache_dir),
                start_ms=start_ms,
                end_ms=end_ms,
                now_ms=now_ms,
                timeout=max(60.0, float(settings["data"]["request_timeout_seconds"])),
                workers=max(1, args.workers),
                min_daily_quote_volume=float(args.min_daily_quote_volume),
            )
            print(f"non-trading USDT-M rows stored: {store.count_non_trading()}")
            store.close()
            return
        _vision(store, symbols, now_ms, args.days, args.daily_days, float(settings["data"]["request_timeout_seconds"]))
        store.close()
        return
    client = BinancePublicClient(
        settings["data"]["rest_base"],
        timeout=float(settings["data"]["request_timeout_seconds"]),
    )
    count = sync_exchange_info(client, store, now_ms)
    if args.all_universe:
        info = client.exchange_info()
        symbols = [row["symbol"] for row in usdt_m_perpetuals(info, trading_only=True)]
    print(f"exchangeInfo USDT-M rows stored: {count}")
    print(f"non-trading USDT-M rows in this snapshot: {store.count_non_trading()}")
    print(f"backfilling {len(symbols)} symbols")
    for symbol in symbols:
        counts = backfill_symbol(
            client,
            store,
            symbol,
            now_ms=now_ms,
            days=args.days,
            daily_days=args.daily_days,
            limit=int(settings["data"]["kline_limit"]),
            pause=float(settings["data"]["request_pause_seconds"]),
        )
        print(f"{symbol}: {counts}")
    store.close()


if __name__ == "__main__":
    main()
