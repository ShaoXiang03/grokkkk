"""Incremental public refresh for the paper loop.

E2 and E3 read daily closes. Completed days come from data.binance.vision
daily files, which are not on the fapi IP-weight limit. fapi is only the
tail vision does not have yet (the last day or two), and only inside
`WeightBudget`. A 418 or 429 pauses fapi. It does not raise out of a slice
and it does not retry in a loop.

Symbols with no stored daily history are left alone. The monthly backfill
owns that history. This module does not call exchangeInfo and does not
place orders.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from desk.data.binance import (
    BinancePublicClient,
    BinancePublicError,
    BinanceRateLimitError,
    request_weight,
)
from desk.data.budget import WeightBudget
from desk.data.vision import load_klines_day
from desk.timeutils import DAY_MS, HOUR_MS, floor_day_ms, last_closed_daily_open

# A 404 this close to the latest closed day is "the file is not up yet",
# not "the contract died". Older 404s are holes.
TAIL_DAYS = 2
# Don't re-hit a 404 tail, or an empty fapi tail, on every slice.
TAIL_RETRY_MS = 15 * 60 * 1000
VISION_ERROR_RETRY_MS = 60_000
# One fapi kline page stays in the weight-1 band (limit < 100).
FAPI_LIMIT = 99
# Vision 404s at least this far before the target, plus an empty fapi tail,
# mean the name has stopped printing. Don't spend the IP confirming it again.
INACTIVE_DATASET = "1d"


def due_daily_symbols(store) -> list[str]:
    """Daily names the paper books can still trade.

    DELISTED is the archive's own label. Everything else with a stored daily
    bar is due, including names that were never written to the symbols table.
    """
    names = store.symbols_with_interval("1d")
    rows = store.list_symbols()
    if not rows:
        return names
    status = {row["symbol"]: row.get("status") for row in rows}
    return [name for name in names if status.get(name) != "DELISTED"]


def _held_symbols(store) -> set[str]:
    held = {row["symbol"] for row in store.paper_position_rows()}
    held.update(row["symbol"] for row in store.book_position_rows())
    return held


def _day_string(open_ms: int) -> str:
    return datetime.fromtimestamp(int(open_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _coerce_klines(raw) -> list[dict]:
    if not raw:
        return []
    if isinstance(raw[0], dict):
        return list(raw)
    from desk.data.binance import parse_klines

    return parse_klines(raw)


def _closed(rows: list[dict] | None, now_ms: int) -> list[dict]:
    if not rows:
        return []
    return [row for row in rows if int(row.get("close_time") or 0) <= int(now_ms)]


class PaperRefresh:
    """One catch-up cursor. Safe to call every loop; safe on a background thread.

    `kline_fetch(symbol, day) -> rows or None` overrides the archive for tests.
    None means the file is not there (HTTP 404).
    """

    def __init__(
        self,
        settings: dict,
        *,
        budget: WeightBudget | None = None,
        client=None,
        kline_fetch=None,
        sleep=None,
        session=None,
        on_summary=None,
    ):
        self.settings = settings
        self.budget = budget if budget is not None else WeightBudget.from_settings(settings)
        self.client = client
        self.kline_fetch = kline_fetch
        self.sleep = sleep if sleep is not None else time.sleep
        self._session = session
        self.on_summary = on_summary
        self.cursor = 0
        self._retry_after: dict[tuple, int] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        timeout = float((settings.get("data") or {}).get("request_timeout_seconds") or 20)
        self.timeout = min(20.0, max(5.0, timeout))
        weight = self.budget.max_weight
        # Spread the cap across the minute so a slice cannot dump it in one second.
        self.fapi_interval = 60.0 / float(weight)

    def fapi_quiet_ms(self, now_ms: int) -> int:
        return self.budget.retry_in_ms(now_ms)

    def start_background(self, store, symbols_fn) -> None:
        """Refresh beside the paper cycle. Failures are logged, not raised."""
        if self._thread is not None and self._thread.is_alive():
            return

        def _run() -> None:
            logging.info(
                "paper refresh started budget=%s weight/min pause_above=%s",
                self.budget.max_weight,
                self.budget.pause_above,
            )
            while not self._stop.is_set():
                try:
                    now_ms = int(time.time() * 1000)
                    symbols = list(symbols_fn() or [])
                    summary = self.run_slice(store, symbols, now_ms)
                except Exception:
                    logging.exception("paper refresh failed; entries stay on stored bars")
                    self._stop.wait(5)
                    continue
                self._log(summary)
                self._stop.wait(self._idle_seconds(summary))

        self._thread = threading.Thread(target=_run, name="paper-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_for(self, store, symbols: list[str] | None, *, max_ms: int, clock=None) -> dict:
        """Foreground catch-up. Stops when fapi is paused, when there is no work, or at max_ms.

        The deadline is monotonic. The budget clock comes from `clock` so the
        rolling minute moves during a long one-shot.
        """
        clock = clock or (lambda: int(time.time() * 1000))
        started = time.monotonic()
        last = {"http": 0, "paused": False, "pause_reason": None}
        symbols = list(symbols or [])
        while (time.monotonic() - started) * 1000 < max_ms:
            last = self.run_slice(store, symbols, int(clock()), max_wall_ms=max_ms)
            if last.get("paused") and int(last.get("vision_files") or 0) == 0:
                break
            if int(last.get("http") or 0) == 0:
                break
        return last

    def run_slice(
        self,
        store,
        symbols: list[str] | None,
        now_ms: int,
        *,
        max_wall_ms: int = 30_000,
        max_vision_files: int = 400,
    ) -> dict:
        """Advance the cursor. Never raises on 418/429 or a bad archive file.

        Returns when the file cap is hit, the wall clock is hit, fapi is
        paused and vision has nothing to do, or a full pass finds no work.
        """
        symbols = [symbol for symbol in (symbols or []) if symbol]
        summary = {
            "vision_files": 0,
            "vision_rows": 0,
            "fapi_requests": 0,
            "fapi_rows": 0,
            "funding_files": 0,
            "http": 0,
            "errors": 0,
            "paused": False,
            "pause_reason": None,
            "inactive": 0,
            "pending": 0,
        }
        if not symbols:
            self._emit(summary)
            return summary
        started = time.monotonic()
        opens = store.latest_opens("1d")
        self._held = _held_symbols(store)
        idle = 0
        steps = 0
        limit = max(len(symbols) * (TAIL_DAYS + 4), max_vision_files) + len(symbols)
        while steps < limit and summary["vision_files"] < max_vision_files:
            steps += 1
            if (time.monotonic() - started) * 1000 >= max_wall_ms:
                break
            symbol = symbols[self.cursor % len(symbols)]
            self.cursor = (self.cursor + 1) % len(symbols)
            try:
                outcome = self._step(store, symbol, opens, now_ms, summary)
            except Exception:
                logging.exception("paper refresh step failed for %s", symbol)
                summary["errors"] += 1
                self._retry_after[("err", symbol)] = int(now_ms) + VISION_ERROR_RETRY_MS
                outcome = "error"
            if outcome == "paused":
                # fapi is quiet. Keep walking so vision for other names still runs.
                summary["paused"] = True
                summary["pause_reason"] = summary.get("pause_reason") or self.budget.pause_reason
                idle += 1
                if idle >= len(symbols):
                    break
                continue
            if outcome in ("skip", "done"):
                idle += 1
                if idle >= len(symbols):
                    break
                continue
            idle = 0
        summary["pending"] = self._pending(opens, symbols, now_ms)
        if self.fapi_quiet_ms(now_ms) > 0 and summary["pause_reason"] is None and summary["fapi_requests"] == 0:
            # Budget or a previous pause blocked fapi. Vision may still have run.
            allowed, reason = self.budget.allow(1, now_ms)
            if not allowed and self._needs_fapi(store, opens, symbols, now_ms):
                summary["paused"] = True
                summary["pause_reason"] = reason
        self._emit(summary)
        return summary

    def _pending(self, opens: dict[str, int], symbols: list[str], now_ms: int) -> int:
        target = last_closed_daily_open(now_ms)
        return sum(1 for symbol in symbols if opens.get(symbol) is not None and int(opens[symbol]) < target)

    def _needs_fapi(self, store, opens, symbols, now_ms) -> bool:
        target = last_closed_daily_open(now_ms)
        for symbol in symbols:
            latest = opens.get(symbol)
            if latest is None or int(latest) >= target:
                continue
            if self._tail_unfilled(store, symbol, int(latest), target):
                return True
        return False

    def _tail_unfilled(self, store, symbol: str, latest: int, target: int) -> bool:
        day = floor_day_ms(latest) + DAY_MS
        while day <= target:
            if day > target - TAIL_DAYS * DAY_MS:
                marked = store.vision_day_rows(symbol, INACTIVE_DATASET, _day_string(day))
                if not marked:
                    return True
            day += DAY_MS
        return False

    def _step(self, store, symbol: str, opens: dict[str, int], now_ms: int, summary: dict) -> str:
        if int(now_ms) < self._retry_after.get(("err", symbol), 0):
            return "skip"
        row = store.symbol_row(symbol)
        if row and row.get("status") == "DELISTED":
            return "skip"
        latest = opens.get(symbol)
        if latest is None:
            return "skip"
        target = last_closed_daily_open(now_ms)
        latest = floor_day_ms(int(latest))
        if latest >= target:
            if symbol not in self._held:
                return "done"
            return self._funding_fapi(store, symbol, now_ms, summary)
        day = latest + DAY_MS
        while day <= target:
            key = _day_string(day)
            if int(now_ms) < self._retry_after.get(("vision", symbol, key), 0):
                if day > target - TAIL_DAYS * DAY_MS:
                    return self._fapi_tail(store, symbol, latest, target, now_ms, opens, summary)
                day += DAY_MS
                continue
            marked = None
            with store.lock:
                marked = store.vision_day_rows(symbol, INACTIVE_DATASET, key)
            if marked == 0:
                day += DAY_MS
                continue
            if marked and marked > 0:
                day += DAY_MS
                continue
            rows, found = self._vision_klines(symbol, key)
            if not found:
                # Transport error. Do not mark a hole and do not call fapi.
                summary["errors"] += 1
                summary["vision_files"] += 1
                summary["http"] += 1
                self._retry_after[("err", symbol)] = int(now_ms) + VISION_ERROR_RETRY_MS
                return "error"
            summary["vision_files"] += 1
            summary["http"] += 1
            if rows is not None:
                closed = _closed(rows, now_ms)
                kept = [row for row in closed if floor_day_ms(int(row["open_time"])) == day]
                with store.lock:
                    if kept:
                        store.upsert_klines(symbol, "1d", kept)
                    store.mark_vision_day(symbol, INACTIVE_DATASET, key, len(kept))
                if kept:
                    opens[symbol] = max(int(opens.get(symbol) or 0), max(int(row["open_time"]) for row in kept))
                    summary["vision_rows"] += len(kept)
                return "vision"
            # 404.
            if day <= target - TAIL_DAYS * DAY_MS:
                with store.lock:
                    store.mark_vision_day(symbol, INACTIVE_DATASET, key, 0)
                return "vision_miss"
            self._retry_after[("vision", symbol, key)] = int(now_ms) + TAIL_RETRY_MS
            return self._fapi_tail(store, symbol, latest, target, now_ms, opens, summary)
        # Every missing day is a recorded hole. Confirm once on fapi, then drop the name.
        return self._fapi_tail(store, symbol, latest, target, now_ms, opens, summary)

    def _vision_klines(self, symbol: str, day: str) -> tuple[list[dict] | None, bool]:
        """Return (rows, found). found False is a transport error. rows None is HTTP 404."""
        try:
            if self.kline_fetch is not None:
                rows = self.kline_fetch(symbol, day)
            else:
                rows = load_klines_day(self._vision_session(), symbol, "1d", day, self.timeout)
        except Exception:
            logging.warning("vision daily klines %s %s failed", symbol, day, exc_info=True)
            return None, False
        if rows is None:
            return None, True
        return list(rows), True

    def _funding_fapi(self, store, symbol: str, now_ms: int, summary: dict) -> str:
        """One funding history call for a name the book holds. Weight 1.

        Vision does not publish USD-M daily funding files. History stays in
        the monthly archive. This only continues from the last stored print,
        and only while the name is open.
        """
        if int(now_ms) < self._retry_after.get(("funding", symbol), 0):
            return "skip"
        with store.lock:
            latest = store.latest_funding_time(symbol)
        if latest is not None and int(now_ms) - int(latest) < 8 * HOUR_MS:
            return "done"
        start = int(now_ms) - 14 * DAY_MS if latest is None else int(latest) + 1
        weight = request_weight("/fapi/v1/fundingRate", {"limit": 1000})
        allowed, reason = self.budget.allow(weight, now_ms)
        if not allowed:
            summary["paused"] = True
            summary["pause_reason"] = self.budget.pause_reason or reason
            return "paused"
        quiet = self.fapi_quiet_ms(now_ms)
        if quiet > 0:
            summary["paused"] = True
            summary["pause_reason"] = self.budget.pause_reason or "budget"
            return "paused"
        self.budget.charge(weight, now_ms)
        try:
            raw = self._client().funding_history(symbol, start_ms=start, end_ms=int(now_ms), limit=1000)
        except BinanceRateLimitError as exc:
            self.budget.observe_used_weight(exc.used_weight, now_ms)
            self.budget.observe_rejection(exc.status_code, exc.retry_after_seconds, now_ms)
            summary["fapi_requests"] += 1
            summary["http"] += 1
            summary["paused"] = True
            summary["pause_reason"] = self.budget.pause_reason
            logging.warning(
                "paper refresh paused on HTTP %s retry_after=%s; not retrying",
                exc.status_code,
                exc.retry_after_seconds,
            )
            return "paused"
        except BinancePublicError as exc:
            summary["fapi_requests"] += 1
            summary["http"] += 1
            summary["errors"] += 1
            if "HTTP 451" in str(exc):
                self.budget.pause_for(now_ms, 6 * 60 * 60 * 1000, "http_451")
                summary["paused"] = True
                summary["pause_reason"] = "http_451"
                logging.warning("paper refresh paused; fapi returned HTTP 451")
                return "paused"
            logging.warning("paper refresh funding %s failed: %s", symbol, exc)
            self._retry_after[("funding", symbol)] = int(now_ms) + VISION_ERROR_RETRY_MS
            return "error"
        except Exception:
            logging.warning("paper refresh funding %s failed", symbol, exc_info=True)
            summary["fapi_requests"] += 1
            summary["http"] += 1
            summary["errors"] += 1
            self._retry_after[("funding", symbol)] = int(now_ms) + VISION_ERROR_RETRY_MS
            return "error"
        self.budget.observe_used_weight(getattr(self._client(), "last_used_weight_1m", None), now_ms)
        rows = []
        for item in raw or []:
            if not isinstance(item, dict) or "fundingTime" not in item:
                continue
            stamp = int(item["fundingTime"])
            if stamp > int(now_ms):
                continue
            rows.append(
                {
                    "funding_time": stamp,
                    "funding_rate": float(item["fundingRate"]),
                    "interval_hours": item.get("fundingIntervalHours") or 8.0,
                }
            )
        summary["fapi_requests"] += 1
        summary["funding_files"] += 1
        summary["http"] += 1
        if rows:
            with store.lock:
                store.upsert_funding(symbol, rows)
        self._retry_after[("funding", symbol)] = int(now_ms) + 8 * HOUR_MS
        self.sleep(self.fapi_interval)
        if self.fapi_quiet_ms(now_ms) > 0:
            summary["paused"] = True
            summary["pause_reason"] = self.budget.pause_reason or "budget"
            return "paused"
        return "funding"

    def _fapi_tail(self, store, symbol, latest, target, now_ms, opens, summary) -> str:
        if int(now_ms) < self._retry_after.get(("fapi", symbol), 0):
            return "skip"
        quiet = self.fapi_quiet_ms(now_ms)
        if quiet > 0:
            summary["paused"] = True
            summary["pause_reason"] = self.budget.pause_reason or "budget"
            return "paused"
        missing = max(1, int((target - floor_day_ms(latest)) // DAY_MS))
        limit = min(FAPI_LIMIT, missing)
        weight = request_weight("/fapi/v1/klines", {"limit": limit})
        allowed, reason = self.budget.allow(weight, now_ms)
        if not allowed:
            summary["paused"] = True
            summary["pause_reason"] = reason
            return "paused"
        self.budget.charge(weight, now_ms)
        try:
            raw = self._client().klines(
                symbol,
                "1d",
                start_ms=floor_day_ms(latest) + DAY_MS,
                end_ms=int(now_ms),
                limit=limit,
            )
        except BinanceRateLimitError as exc:
            self.budget.observe_used_weight(exc.used_weight, now_ms)
            self.budget.observe_rejection(exc.status_code, exc.retry_after_seconds, now_ms)
            summary["fapi_requests"] += 1
            summary["http"] += 1
            summary["paused"] = True
            summary["pause_reason"] = self.budget.pause_reason
            logging.warning(
                "paper refresh paused on HTTP %s retry_after=%s; not retrying",
                exc.status_code,
                exc.retry_after_seconds,
            )
            return "paused"
        except BinancePublicError as exc:
            summary["fapi_requests"] += 1
            summary["http"] += 1
            summary["errors"] += 1
            text = str(exc)
            if "HTTP 451" in text:
                self.budget.pause_for(now_ms, 6 * 60 * 60 * 1000, "http_451")
                summary["paused"] = True
                summary["pause_reason"] = "http_451"
                logging.warning("paper refresh paused; fapi returned HTTP 451")
                return "paused"
            logging.warning("paper refresh fapi %s failed: %s", symbol, text)
            self._retry_after[("fapi", symbol)] = int(now_ms) + VISION_ERROR_RETRY_MS
            return "error"
        except Exception:
            logging.warning("paper refresh fapi %s failed", symbol, exc_info=True)
            summary["fapi_requests"] += 1
            summary["http"] += 1
            summary["errors"] += 1
            self._retry_after[("fapi", symbol)] = int(now_ms) + VISION_ERROR_RETRY_MS
            return "error"
        used = getattr(self._client(), "last_used_weight_1m", None)
        self.budget.observe_used_weight(used, now_ms)
        parsed = _closed(_coerce_klines(raw), now_ms)
        kept = [row for row in parsed if floor_day_ms(int(row["open_time"])) > floor_day_ms(latest)]
        kept = [row for row in kept if floor_day_ms(int(row["open_time"])) <= target]
        summary["fapi_requests"] += 1
        summary["http"] += 1
        summary["fapi_rows"] += len(kept)
        if kept:
            with store.lock:
                store.upsert_klines(symbol, "1d", kept)
            opens[symbol] = max(int(row["open_time"]) for row in kept)
            self.sleep(self.fapi_interval)
            if self.fapi_quiet_ms(now_ms) > 0:
                summary["paused"] = True
                summary["pause_reason"] = self.budget.pause_reason or "budget"
                return "paused"
            return "fapi"
        # Nothing newer than what we stored. A gap that starts before the
        # latest closed day is a contract that has stopped printing. The
        # target day alone can just be a file that is not published yet.
        if floor_day_ms(latest) + DAY_MS < target:
            self._mark_inactive(store, symbol, now_ms)
            summary["inactive"] += 1
        else:
            self._retry_after[("fapi", symbol)] = int(now_ms) + TAIL_RETRY_MS
        self.sleep(self.fapi_interval)
        if self.budget.pause_until_ms > int(now_ms):
            summary["paused"] = True
            summary["pause_reason"] = self.budget.pause_reason
            return "paused"
        return "fapi"

    def _mark_inactive(self, store, symbol: str, now_ms: int) -> None:
        with store.lock:
            current = store.symbol_row(symbol) or {}
            base = symbol[: -len("USDT")] if symbol.endswith("USDT") else symbol
            store.upsert_symbols(
                [
                    {
                        "symbol": symbol,
                        "status": "DELISTED",
                        "contract_type": current.get("contract_type") or "PERPETUAL",
                        "base_asset": current.get("base_asset") or base,
                        "quote_asset": current.get("quote_asset") or "USDT",
                        "margin_asset": current.get("margin_asset") or "USDT",
                        "onboard_date_ms": current.get("onboard_date_ms"),
                    }
                ],
                int(now_ms),
            )
        logging.info("paper refresh marked %s DELISTED; vision and fapi have no newer daily bar", symbol)

    def _emit(self, summary: dict) -> None:
        callback = self.on_summary
        if callback is None:
            return
        try:
            callback(summary)
        except Exception:
            logging.exception("paper refresh status observer failed")

    def _client(self):
        if self.client is not None:
            return self.client
        data = self.settings.get("data") or {}
        self.client = BinancePublicClient(
            data.get("rest_base") or "https://fapi.binance.com",
            timeout=self.timeout,
        )
        return self.client

    def _vision_session(self):
        if self._session is not None:
            return self._session
        import requests

        self._session = requests.Session()
        self._session.headers["User-Agent"] = "chart-analyst-paper-refresh/0.4"
        return self._session

    def _idle_seconds(self, summary: dict) -> float:
        if summary.get("paused"):
            wait = self.fapi_quiet_ms(int(time.time() * 1000)) / 1000.0
            if int(summary.get("vision_files") or 0) > 0:
                return 0.05
            return min(300.0, max(1.0, wait))
        if int(summary.get("http") or 0) == 0:
            return 30.0
        return 0.05

    def _log(self, summary: dict) -> None:
        if int(summary.get("http") or 0) == 0 and not summary.get("paused"):
            return
        logging.info(
            "paper refresh vision=%s fapi=%s rows=%s funding=%s paused=%s reason=%s pending=%s inactive=%s errors=%s",
            summary.get("vision_files"),
            summary.get("fapi_requests"),
            summary.get("vision_rows"),
            summary.get("funding_files"),
            summary.get("paused"),
            summary.get("pause_reason"),
            summary.get("pending"),
            summary.get("inactive"),
            summary.get("errors"),
        )
