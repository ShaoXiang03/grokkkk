"""Resumable parallel backfill of USD-M perpetual archives.

Files come from data.binance.vision only. Completed months are recorded in
SQLite and the zip bytes are cached on disk, so a second run continues where
the first one stopped. Delisted contracts stay in the archive and are kept.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from desk.data.store import Store
from desk.data.vision import (
    is_um_usdt_perpetual_symbol,
    iter_months,
    list_um_usdt_perpetuals,
    load_funding_month_cached,
    load_klines_month_cached,
)
from desk.timeutils import DAY_MS

# Three empty months after a contract's last file ends the tail. The head is
# still scanned month by month so a one-month listing is not skipped.
TAIL_MISS_LIMIT = 3


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = "chart-analyst-backfill/0.4"
    retry = Retry(
        total=4,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session.mount("https://", adapter)
    return session


def base_asset_of(symbol: str) -> str:
    return symbol[: -len("USDT")] if symbol.endswith("USDT") else symbol


def classify_listing(last_open: int | None, archive_end_open: int | None) -> str:
    """Infer TRADING vs DELISTED from the last daily bar Vision actually has.

    exchangeInfo is not part of the archive. A contract whose last daily bar
    is more than three days before the archive's latest daily bar is treated
    as delisted. That is a listing inference, not an exchange status.
    """
    if last_open is None or archive_end_open is None:
        return "UNKNOWN"
    if int(last_open) < int(archive_end_open) - 3 * DAY_MS:
        return "DELISTED"
    return "TRADING"


def _emit_months(session, store_done: dict[str, int], symbol: str, dataset: str, months: list[str], loader, result_q, lock_print):
    misses = 0
    seen = False
    for month in months:
        if month in store_done:
            rows_n = store_done[month]
            if rows_n > 0:
                seen = True
                misses = 0
            else:
                misses += 1
                if seen and misses >= TAIL_MISS_LIMIT:
                    break
            continue
        try:
            rows = loader(month)
        except Exception as exc:
            with lock_print:
                print(f"{symbol} {dataset} {month}: retryable error: {exc}", flush=True)
            raise
        nrows = 0 if not rows else len(rows)
        result_q.put((symbol, dataset, month, rows or []))
        if nrows == 0:
            misses += 1
            if seen and misses >= TAIL_MISS_LIMIT:
                break
        else:
            seen = True
            misses = 0
            with lock_print:
                print(f"{symbol} {dataset} {month}: {nrows}", flush=True)


def _download_symbol(
    symbol: str,
    *,
    months: list[str],
    datasets: tuple[str, ...],
    done: dict[str, dict[str, int]],
    cache_dir: Path,
    timeout: float,
    result_q,
    lock_print,
) -> None:
    session = make_session()
    try:
        if "1d" in datasets:
            _emit_months(
                session,
                done.get("1d", {}),
                symbol,
                "1d",
                months,
                lambda month: load_klines_month_cached(session, cache_dir, symbol, "1d", month, timeout),
                result_q,
                lock_print,
            )
        if "1h" in datasets:
            _emit_months(
                session,
                done.get("1h", {}),
                symbol,
                "1h",
                months,
                lambda month: load_klines_month_cached(session, cache_dir, symbol, "1h", month, timeout),
                result_q,
                lock_print,
            )
        if "4h" in datasets:
            _emit_months(
                session,
                done.get("4h", {}),
                symbol,
                "4h",
                months,
                lambda month: load_klines_month_cached(session, cache_dir, symbol, "4h", month, timeout),
                result_q,
                lock_print,
            )
        if "funding" in datasets:
            _emit_months(
                session,
                done.get("funding", {}),
                symbol,
                "funding",
                months,
                lambda month: load_funding_month_cached(session, cache_dir, symbol, month, timeout),
                result_q,
                lock_print,
            )
    finally:
        session.close()


def _apply_item(store: Store, item) -> None:
    symbol, dataset, month, rows = item
    if dataset == "funding":
        store.write_vision_funding(symbol, month, rows)
        return
    store.write_vision_klines(symbol, dataset, month, rows)


def run_symbol_jobs(symbols: list[str], job, workers: int, store: Store) -> None:
    """Run one job per symbol. Jobs push (symbol, dataset, month, rows) and do not touch SQLite."""
    from queue import Queue

    result_q: Queue = Queue(maxsize=max(32, workers * 4))
    writer_error: list[BaseException] = []

    def writer() -> None:
        while True:
            item = result_q.get()
            if item is None:
                return
            try:
                _apply_item(store, item)
            except Exception as exc:
                writer_error.append(exc)

    writer_thread = threading.Thread(target=writer, name="vision-writer", daemon=True)
    writer_thread.start()
    job_error: BaseException | None = None
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(job, symbol, result_q) for symbol in symbols]
            for future in as_completed(futures):
                future.result()
    except Exception as exc:
        job_error = exc
    result_q.put(None)
    writer_thread.join()
    if job_error is not None:
        raise job_error
    if writer_error:
        raise writer_error[0]


def refresh_listing_status(store: Store, now_ms: int) -> dict:
    """Set TRADING or DELISTED from each symbol's last daily bar."""
    coverage = store.max_daily_quote_volume()
    if not coverage:
        return {"archive_end_open": None, "trading": 0, "delisted": 0}
    archive_end = max(int(row["last_open"]) for row in coverage if row["last_open"] is not None)
    trading = 0
    delisted = 0
    rows = []
    for row in coverage:
        status = classify_listing(row["last_open"], archive_end)
        if status == "DELISTED":
            delisted += 1
        elif status == "TRADING":
            trading += 1
        rows.append(
            {
                "symbol": row["symbol"],
                "status": status,
                "contract_type": "PERPETUAL",
                "base_asset": base_asset_of(row["symbol"]),
                "quote_asset": "USDT",
                "margin_asset": "USDT",
                "onboard_date_ms": row["first_open"],
            }
        )
    store.upsert_symbols(rows, now_ms)
    return {"archive_end_open": archive_end, "trading": trading, "delisted": delisted}


def symbols_meeting_quote_volume(store: Store, min_daily_quote_volume: float) -> list[str]:
    """Symbols whose best UTC day can still reach the loosest 24h gate.

    A 24h window sits inside two consecutive UTC days, so a symbol whose best
    day is below half the loosest gate can never pass that gate.
    """
    kept = []
    for row in store.max_daily_quote_volume():
        volume = row["max_quote_volume"]
        if volume is not None and float(volume) >= float(min_daily_quote_volume):
            kept.append(row["symbol"])
    return sorted(kept)


def backfill_vision_universe(
    store: Store,
    *,
    cache_dir: Path,
    start_ms: int,
    end_ms: int,
    now_ms: int,
    timeout: float,
    workers: int,
    min_daily_quote_volume: float,
    symbols: list[str] | None = None,
    intraday: bool = True,
) -> dict:
    """Download daily files for the universe, then 1h, 4h, and funding for liquid names."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    months = iter_months(start_ms, end_ms)
    lock_print = threading.Lock()
    if symbols is None:
        session = make_session()
        try:
            symbols = list_um_usdt_perpetuals(session, timeout)
        finally:
            session.close()
    symbols = [symbol for symbol in symbols if is_um_usdt_perpetual_symbol(symbol)]
    print(f"vision USDT-M perpetuals: {len(symbols)} months={months[0]}..{months[-1]} workers={workers}", flush=True)
    (cache_dir / "um-usdt-perpetuals.txt").write_text("\n".join(symbols) + "\n")

    daily_done = {symbol: {"1d": store.vision_months_for(symbol, "1d")} for symbol in symbols}

    def daily_job(symbol: str, result_q) -> None:
        _download_symbol(
            symbol,
            months=months,
            datasets=("1d",),
            done=daily_done[symbol],
            cache_dir=cache_dir,
            timeout=timeout,
            result_q=result_q,
            lock_print=lock_print,
        )

    run_symbol_jobs(symbols, daily_job, workers, store)
    listing = refresh_listing_status(store, now_ms)
    liquid = symbols_meeting_quote_volume(store, min_daily_quote_volume)
    print(
        f"daily archive_end={listing['archive_end_open']} trading={listing['trading']} "
        f"delisted={listing['delisted']} liquid_at_{min_daily_quote_volume:.0f}={len(liquid)}",
        flush=True,
    )
    if intraday and liquid:

        intraday_done = {
            symbol: {name: store.vision_months_for(symbol, name) for name in ("1h", "4h", "funding")}
            for symbol in liquid
        }
        daily_done = {symbol: store.vision_months_for(symbol, "1d") for symbol in liquid}

        def listed_span(symbol: str) -> list[str]:
            """Months that already have a daily file. Intraday does not re-probe the empty head."""
            positive = [month for month in months if daily_done[symbol].get(month, 0) > 0]
            if not positive:
                return []
            return months[months.index(positive[0]) : months.index(positive[-1]) + 1]

        def intraday_job(symbol: str, result_q) -> None:
            span = listed_span(symbol)
            if not span:
                return
            _download_symbol(
                symbol,
                months=span,
                datasets=("1h", "4h", "funding"),
                done=intraday_done[symbol],
                cache_dir=cache_dir,
                timeout=timeout,
                result_q=result_q,
                lock_print=lock_print,
            )

        run_symbol_jobs(liquid, intraday_job, workers, store)
    listing = refresh_listing_status(store, now_ms)
    return {
        "symbols_listed": len(symbols),
        "symbols_liquid": liquid,
        "listing": listing,
        "months": months,
    }
