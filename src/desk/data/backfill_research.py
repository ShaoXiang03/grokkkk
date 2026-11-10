"""Funding, premium-index, and daily open-interest for the research books.

Daily klines are the existing Vision backfill. This module adds the three
archives those books need and keeps the zip cache for the monthly files.
Metrics are reduced to the last complete 5-minute snapshot of each UTC day
so the sqlite file stays a daily panel. A 404 is remembered and not retried.
"""

from __future__ import annotations

import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from desk.data.backfill_vision import make_session
from desk.data.store import Store
from desk.data.vision import (
    _zip_text,
    fetch_bytes,
    iter_months,
    load_funding_month_cached,
    load_premium_month_cached,
    metrics_last_snapshot,
    metrics_url,
    parse_metrics_csv,
)
from desk.timeutils import DAY_MS

# Below the $20m/day gate so a name that only barely clears it is still downloaded.
CANDIDATE_AVG_QUOTE = 8_000_000.0
PREMIUM_INTERVAL = "premium1d"


def candidate_symbols(store: Store, min_avg_quote: float = CANDIDATE_AVG_QUOTE) -> list[str]:
    """Names that could pass the liquidity floor on some 30-day window, plus BTC and ETH."""
    frame = store.load_interval_frame("1d")
    if frame.empty:
        return []
    frame = frame.sort_values(["symbol", "open_time"])
    keep: set[str] = set()
    for symbol, group in frame.groupby("symbol", sort=False):
        volume = group["quote_volume"].to_numpy(dtype=float)
        if volume.size == 0:
            continue
        if float(np.nanmax(volume)) >= 20_000_000.0:
            keep.add(str(symbol))
            continue
        if volume.size < 30:
            if float(np.nansum(volume)) / 30.0 >= min_avg_quote:
                keep.add(str(symbol))
            continue
        rolling = pd.Series(np.nan_to_num(volume, nan=0.0)).rolling(30, min_periods=30).mean()
        if float(rolling.max()) >= min_avg_quote:
            keep.add(str(symbol))
    keep.add("BTCUSDT")
    keep.add("ETHUSDT")
    have = set(frame["symbol"].astype(str))
    return sorted(symbol for symbol in keep if symbol in have)


def _month_span(store: Store, symbol: str) -> list[str]:
    done = store.vision_months_for(symbol, "1d")
    positive = sorted(month for month, rows in done.items() if rows > 0)
    if not positive:
        return []
    start = datetime.strptime(positive[0], "%Y-%m").replace(tzinfo=timezone.utc)
    end = datetime.strptime(positive[-1], "%Y-%m").replace(tzinfo=timezone.utc)
    return iter_months(int(start.timestamp() * 1000), int(end.timestamp() * 1000))


def backfill_funding_and_premium(
    store: Store,
    symbols: list[str],
    *,
    cache_dir: Path,
    timeout: float,
    workers: int,
) -> None:
    spans = {symbol: _month_span(store, symbol) for symbol in symbols}
    funding_done = {symbol: store.vision_months_for(symbol, "funding") for symbol in symbols}
    premium_done = {symbol: store.vision_months_for(symbol, PREMIUM_INTERVAL) for symbol in symbols}

    def job(symbol: str, result_q) -> None:
        months = spans.get(symbol) or []
        if not months:
            return
        session = make_session()
        try:
            for month in months:
                if month not in funding_done.get(symbol, {}):
                    rows = load_funding_month_cached(session, cache_dir, symbol, month, timeout)
                    result_q.put((symbol, "funding", month, rows or []))
                if month not in premium_done.get(symbol, {}):
                    rows = load_premium_month_cached(session, cache_dir, symbol, "1d", month, timeout)
                    result_q.put((symbol, PREMIUM_INTERVAL, month, rows or []))
        finally:
            session.close()

    # The shared writer only knows funding vs klines. Premium is written as klines
    # with interval premium1d, which is what load_panel reads.
    from desk.data.backfill_vision import _apply_item

    def apply(item) -> None:
        symbol, dataset, month, rows = item
        if dataset == "funding":
            _apply_item(store, item)
            return
        store.write_vision_klines(symbol, PREMIUM_INTERVAL, month, rows)

    _run_jobs(symbols, job, workers, apply)


def _run_jobs(symbols, job, workers, apply) -> None:
    from queue import Queue
    import threading

    result_q: Queue = Queue(maxsize=max(32, workers * 4))
    errors: list[BaseException] = []

    def writer() -> None:
        while True:
            item = result_q.get()
            if item is None:
                return
            try:
                apply(item)
            except Exception as exc:
                errors.append(exc)

    thread = threading.Thread(target=writer, name="research-writer", daemon=True)
    thread.start()
    job_error = None
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(job, symbol, result_q) for symbol in symbols]
            for future in as_completed(futures):
                future.result()
    except Exception as exc:
        job_error = exc
    result_q.put(None)
    thread.join()
    if job_error:
        raise job_error
    if errors:
        raise errors[0]


def _symbol_days(store: Store, symbol: str) -> list[str]:
    row = store.conn.execute(
        "SELECT MIN(open_time) AS a, MAX(open_time) AS b FROM klines WHERE symbol = ? AND interval = '1d'",
        (symbol,),
    ).fetchone()
    if row is None or row["a"] is None:
        return []
    start = max(int(row["a"]), int(datetime(2020, 9, 1, tzinfo=timezone.utc).timestamp() * 1000))
    end = int(row["b"])
    start -= start % DAY_MS
    end -= end % DAY_MS
    days = []
    cursor = datetime.fromtimestamp(start / 1000, tz=timezone.utc)
    last = datetime.fromtimestamp(end / 1000, tz=timezone.utc)
    while cursor <= last:
        days.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    return days


def targeted_metric_days(
    store: Store,
    *,
    top_n: int = 80,
    min_avg_quote: float = 10_000_000.0,
    start: str = "2020-09-01",
    end: str = "2026-11-01",
) -> dict[str, list[str]]:
    """Days to fetch: the point-in-time top N by 30-day quote volume, plus BTC and ETH.

    The books only hold the top 40. The extra names are the buffer for the
    open-interest floor, which can drop a liquid name and let the next one in.
    A name outside this set cannot enter the book on that day.
    """
    frame = store.load_interval_frame("1d")
    if frame.empty:
        return {}
    frame["day"] = (frame["open_time"].astype("int64") // DAY_MS) * DAY_MS
    series: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for symbol, group in frame.groupby("symbol", sort=False):
        group = group.sort_values("day")
        series[str(symbol)] = (
            group["day"].to_numpy(np.int64),
            np.nan_to_num(group["quote_volume"].to_numpy(dtype=float), nan=0.0),
        )
    start_ms = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    plan: dict[str, list[str]] = {}
    day = start_ms
    while day < end_ms:
        scores: list[tuple[float, str]] = []
        for symbol, (idx, volume) in series.items():
            pos = int(np.searchsorted(idx, day))
            if pos >= len(idx) or int(idx[pos]) != day:
                continue
            left = int(np.searchsorted(idx, day - 29 * DAY_MS))
            avg = float(volume[left : pos + 1].sum()) / 30.0
            if avg >= min_avg_quote or symbol in ("BTCUSDT", "ETHUSDT"):
                scores.append((avg, symbol))
        scores.sort(reverse=True)
        picked = [symbol for _avg, symbol in scores[: int(top_n)]]
        for symbol in ("BTCUSDT", "ETHUSDT"):
            if symbol not in picked and any(name == symbol for _avg, name in scores):
                picked.append(symbol)
        label = datetime.fromtimestamp(day / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        for symbol in picked:
            plan.setdefault(symbol, []).append(label)
        day += DAY_MS
    # Drop days already stored so a restart continues.
    trimmed: dict[str, list[str]] = {}
    for symbol, days in plan.items():
        done = store.vision_day_done(symbol, "metrics")
        left = [day for day in days if day not in done]
        if left:
            trimmed[symbol] = left
    return trimmed


def backfill_metrics(store: Store, symbols: list[str], *, timeout: float, workers: int, plan: dict[str, list[str]] | None = None) -> None:
    """Download daily metrics and store the last complete snapshot of each day."""
    if plan is None:
        plans = []
        for symbol in symbols:
            done = store.vision_day_done(symbol, "metrics")
            days = [day for day in _symbol_days(store, symbol) if day not in done]
            plans.append((symbol, days))
    else:
        plans = [(symbol, days) for symbol, days in plan.items() if days]

    def one(symbol: str, days: list[str]) -> tuple[str, list[dict], list[tuple]]:
        session = make_session()
        metrics: list[dict] = []
        marks: list[tuple] = []
        try:
            for day in days:
                payload = fetch_bytes(metrics_url(symbol, day), session, timeout)
                if payload is None:
                    marks.append((symbol, "metrics", day, 0))
                    continue
                try:
                    text = _zip_text(payload)
                    rows = parse_metrics_csv(text)
                except (zipfile.BadZipFile, IndexError, UnicodeDecodeError, ValueError):
                    marks.append((symbol, "metrics", day, 0))
                    continue
                snap = metrics_last_snapshot(rows)
                if snap is None:
                    marks.append((symbol, "metrics", day, 0))
                    continue
                snap["symbol"] = symbol
                metrics.append(snap)
                marks.append((symbol, "metrics", day, 1))
        finally:
            session.close()
        return symbol, metrics, marks

    print(f"metrics symbols={len(plans)} workers={workers}", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, symbol, days) for symbol, days in plans]
        finished = 0
        for future in as_completed(futures):
            symbol, metrics, marks = future.result()
            store.write_metrics_batch(metrics, marks)
            finished += 1
            if finished % 10 == 0 or finished == len(plans):
                print(f"metrics {finished}/{len(plans)} last={symbol} days={len(marks)}", flush=True)
