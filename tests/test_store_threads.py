"""The refresh thread and the paper cycle share one sqlite connection."""

import sqlite3
import threading

from desk.data.store import Store
from desk.paper.books import book_priority_symbols
from desk.timeutils import DAY_MS
from desk_helpers import make_settings

OPEN = 1_700_000_000_000


def _bar(symbol: str, volume: float) -> dict:
    return {
        "open_time": OPEN,
        "close_time": OPEN + DAY_MS - 1,
        "open": 1.0,
        "high": 1.0,
        "low": 1.0,
        "close": 1.0,
        "volume": 1.0,
        "quote_volume": volume,
    }


def test_priority_ranking_and_writes_do_not_share_the_connection_unsafely(tmp_path):
    """Two threads on one Store used to raise InterfaceError from latest_daily_quote_volume."""
    settings = make_settings(tmp_path)
    store = Store(tmp_path / "desk.sqlite")
    names = [f"N{i:02d}USDT" for i in range(24)]
    for index, name in enumerate(names):
        store.upsert_klines(name, "1d", [_bar(name, float(index + 1))])
    errors: list[BaseException] = []
    stop = threading.Event()

    def rank() -> None:
        flip = False
        while not stop.is_set():
            # Order is part of the cache key, so each pass reads sqlite again.
            pool = list(reversed(names)) if flip else names
            flip = not flip
            try:
                ranked = book_priority_symbols(store, settings, pool)
            except sqlite3.Error as exc:
                errors.append(exc)
                stop.set()
                return
            if not ranked or set(ranked) != set(names):
                errors.append(AssertionError(ranked))
                stop.set()
                return

    def write() -> None:
        while not stop.is_set():
            try:
                store.upsert_klines("N00USDT", "1d", [_bar("N00USDT", 100.0)])
                store.latest_opens("1d")
            except sqlite3.Error as exc:
                errors.append(exc)
                stop.set()
                return

    threads = [threading.Thread(target=rank), threading.Thread(target=rank), threading.Thread(target=write)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(0.4)
    stop.set()
    for thread in threads:
        thread.join(2)
    store.close()
    assert errors == []
