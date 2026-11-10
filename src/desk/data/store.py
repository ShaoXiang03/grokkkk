"""SQLite cache for klines, funding, signals, cooldowns, and the blocklist."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pandas as pd


SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    symbol TEXT PRIMARY KEY,
    status TEXT,
    contract_type TEXT,
    base_asset TEXT,
    quote_asset TEXT,
    margin_asset TEXT,
    onboard_date_ms INTEGER,
    updated_at_ms INTEGER
);
CREATE TABLE IF NOT EXISTS klines (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    close_time INTEGER NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    quote_volume REAL,
    PRIMARY KEY (symbol, interval, open_time)
);
CREATE TABLE IF NOT EXISTS funding (
    symbol TEXT NOT NULL,
    funding_time INTEGER NOT NULL,
    funding_rate REAL NOT NULL,
    interval_hours REAL,
    PRIMARY KEY (symbol, funding_time)
);
CREATE TABLE IF NOT EXISTS funding_snapshot (
    symbol TEXT PRIMARY KEY,
    ts_ms INTEGER,
    predicted_rate REAL,
    next_funding_time INTEGER
);
CREATE TABLE IF NOT EXISTS book_ticker (
    symbol TEXT PRIMARY KEY,
    ts_ms INTEGER,
    bid REAL,
    ask REAL
);
CREATE TABLE IF NOT EXISTS ticker_24h (
    symbol TEXT PRIMARY KEY,
    ts_ms INTEGER,
    quote_volume REAL,
    last_price REAL
);
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    setup TEXT NOT NULL,
    direction TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cooldowns (
    symbol TEXT PRIMARY KEY,
    until_ms INTEGER NOT NULL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS blocklist (
    symbol TEXT PRIMARY KEY,
    reason TEXT,
    active INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vision_months (
    symbol TEXT NOT NULL,
    dataset TEXT NOT NULL,
    month TEXT NOT NULL,
    rows INTEGER NOT NULL,
    PRIMARY KEY (symbol, dataset, month)
);
CREATE TABLE IF NOT EXISTS vision_days (
    symbol TEXT NOT NULL,
    dataset TEXT NOT NULL,
    day TEXT NOT NULL,
    rows INTEGER NOT NULL,
    PRIMARY KEY (symbol, dataset, day)
);
CREATE TABLE IF NOT EXISTS metrics_daily (
    symbol TEXT NOT NULL,
    day_ms INTEGER NOT NULL,
    create_time INTEGER NOT NULL,
    sum_open_interest REAL,
    sum_open_interest_value REAL,
    count_long_short_ratio REAL,
    sum_taker_long_short_vol_ratio REAL,
    PRIMARY KEY (symbol, day_ms)
);
CREATE TABLE IF NOT EXISTS webhook_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_ms INTEGER NOT NULL,
    last_error TEXT,
    created_ms INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL,
    delivered_ms INTEGER
);
CREATE TABLE IF NOT EXISTS decision_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    signal_id TEXT NOT NULL,
    event TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    market_context TEXT NOT NULL,
    requested_decision TEXT NOT NULL,
    effective_decision TEXT NOT NULL,
    reasoning TEXT,
    decision_ts TEXT,
    override_refused INTEGER NOT NULL,
    reason TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_equity REAL,
    equity_usdt REAL,
    day_start_equity REAL,
    week_start_equity REAL,
    day_key TEXT,
    week_key TEXT,
    as_of_ms INTEGER,
    started_at_ms INTEGER,
    last_block TEXT,
    updated_ms INTEGER
);
CREATE TABLE IF NOT EXISTS paper_positions (
    symbol TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exit_ms INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_resolutions (
    signal_id TEXT PRIMARY KEY,
    outcome TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    class TEXT NOT NULL,
    kind TEXT NOT NULL,
    signal_id TEXT,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_book_positions (
    book TEXT NOT NULL,
    symbol TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_ms INTEGER NOT NULL,
    PRIMARY KEY (book, symbol)
);
CREATE TABLE IF NOT EXISTS paper_book_meta (
    book TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_ms INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._ensure_funding_interval()

    def close(self) -> None:
        self.conn.close()

    def _ensure_funding_interval(self) -> None:
        columns = [row[1] for row in self.conn.execute("PRAGMA table_info(funding)")]
        if "interval_hours" not in columns:
            self.conn.execute("ALTER TABLE funding ADD COLUMN interval_hours REAL")
            self.conn.commit()

    def upsert_symbols(self, rows: list[dict], updated_at_ms: int) -> None:
        self.conn.executemany(
            """
            INSERT INTO symbols (
                symbol, status, contract_type, base_asset, quote_asset, margin_asset, onboard_date_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                status=excluded.status,
                contract_type=excluded.contract_type,
                base_asset=excluded.base_asset,
                quote_asset=excluded.quote_asset,
                margin_asset=excluded.margin_asset,
                onboard_date_ms=excluded.onboard_date_ms,
                updated_at_ms=excluded.updated_at_ms
            """,
            [
                (
                    row["symbol"],
                    row.get("status"),
                    row.get("contract_type"),
                    row.get("base_asset"),
                    row.get("quote_asset"),
                    row.get("margin_asset"),
                    row.get("onboard_date_ms"),
                    updated_at_ms,
                )
                for row in rows
            ],
        )
        self.conn.commit()

    def list_symbols(self, trading_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM symbols WHERE contract_type = 'PERPETUAL' AND quote_asset = 'USDT'"
        if trading_only:
            sql += " AND status = 'TRADING'"
        sql += " ORDER BY symbol"
        return [dict(row) for row in self.conn.execute(sql)]

    def symbol_row(self, symbol: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM symbols WHERE symbol = ?", (symbol,)).fetchone()
        return dict(row) if row else None

    def count_non_trading(self) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM symbols
            WHERE contract_type = 'PERPETUAL' AND quote_asset = 'USDT' AND status != 'TRADING'
            """
        ).fetchone()
        return int(row["n"])

    def upsert_klines(self, symbol: str, interval: str, rows: list[dict]) -> int:
        self.conn.executemany(
            """
            INSERT INTO klines (
                symbol, interval, open_time, close_time, open, high, low, close, volume, quote_volume
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, interval, open_time) DO UPDATE SET
                close_time=excluded.close_time,
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                quote_volume=excluded.quote_volume
            """,
            [
                (
                    symbol,
                    interval,
                    int(row["open_time"]),
                    int(row["close_time"]),
                    row["open"],
                    row["high"],
                    row["low"],
                    row["close"],
                    row["volume"],
                    row["quote_volume"],
                )
                for row in rows
            ],
        )
        self.conn.commit()
        return len(rows)

    def latest_open_time(self, symbol: str, interval: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(open_time) AS t FROM klines WHERE symbol = ? AND interval = ?",
            (symbol, interval),
        ).fetchone()
        if row is None or row["t"] is None:
            return None
        return int(row["t"])

    def klines(self, symbol: str, interval: str, start_ms: int | None = None, end_ms: int | None = None) -> pd.DataFrame:
        sql = "SELECT open_time, close_time, open, high, low, close, volume, quote_volume FROM klines WHERE symbol = ? AND interval = ?"
        params: list = [symbol, interval]
        if start_ms is not None:
            sql += " AND open_time >= ?"
            params.append(int(start_ms))
        if end_ms is not None:
            sql += " AND close_time <= ?"
            params.append(int(end_ms))
        sql += " ORDER BY open_time"
        frame = pd.read_sql_query(sql, self.conn, params=params)
        return frame

    def upsert_funding(self, symbol: str, rows: list[dict]) -> None:
        self.conn.executemany(
            """
            INSERT INTO funding (symbol, funding_time, funding_rate, interval_hours) VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol, funding_time) DO UPDATE SET
                funding_rate=excluded.funding_rate,
                interval_hours=COALESCE(excluded.interval_hours, funding.interval_hours)
            """,
            [
                (
                    symbol,
                    int(row["funding_time"]),
                    float(row["funding_rate"]),
                    row.get("interval_hours"),
                )
                for row in rows
            ],
        )
        self.conn.commit()

    def funding(self, symbol: str) -> pd.DataFrame:
        return pd.read_sql_query(
            "SELECT funding_time, funding_rate FROM funding WHERE symbol = ? ORDER BY funding_time",
            self.conn,
            params=[symbol],
        )

    def save_funding_snapshot(self, symbol: str, ts_ms: int, predicted_rate: float | None, next_funding_time: int | None) -> None:
        self.conn.execute(
            """
            INSERT INTO funding_snapshot (symbol, ts_ms, predicted_rate, next_funding_time)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                ts_ms=excluded.ts_ms,
                predicted_rate=excluded.predicted_rate,
                next_funding_time=excluded.next_funding_time
            """,
            (symbol, ts_ms, predicted_rate, next_funding_time),
        )
        self.conn.commit()

    def funding_snapshot(self, symbol: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM funding_snapshot WHERE symbol = ?", (symbol,)).fetchone()
        return dict(row) if row else None

    def save_book(self, symbol: str, ts_ms: int, bid: float, ask: float) -> None:
        self.conn.execute(
            """
            INSERT INTO book_ticker (symbol, ts_ms, bid, ask) VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET ts_ms=excluded.ts_ms, bid=excluded.bid, ask=excluded.ask
            """,
            (symbol, ts_ms, bid, ask),
        )
        self.conn.commit()

    def book(self, symbol: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM book_ticker WHERE symbol = ?", (symbol,)).fetchone()
        return dict(row) if row else None

    def save_ticker(self, symbol: str, ts_ms: int, quote_volume: float | None, last_price: float | None) -> None:
        self.conn.execute(
            """
            INSERT INTO ticker_24h (symbol, ts_ms, quote_volume, last_price) VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                ts_ms=excluded.ts_ms, quote_volume=excluded.quote_volume, last_price=excluded.last_price
            """,
            (symbol, ts_ms, quote_volume, last_price),
        )
        self.conn.commit()

    def ticker(self, symbol: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM ticker_24h WHERE symbol = ?", (symbol,)).fetchone()
        return dict(row) if row else None

    def has_signal(self, signal_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM signals WHERE signal_id = ?", (signal_id,)).fetchone()
        return row is not None

    def has_dedupe(self, key: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM signals WHERE dedupe_key = ?", (key,)).fetchone()
        return row is not None

    def save_signal(self, packet: dict) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO signals (
                signal_id, dedupe_key, symbol, setup, direction, ts_ms, status, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                packet["signal_id"],
                packet["dedupe_key"],
                packet["symbol"],
                packet["setup"],
                packet["direction"],
                int(packet["ts_ms"]),
                packet["status"],
                json.dumps(packet, default=str),
            ),
        )
        self.conn.commit()

    def set_cooldown(self, symbol: str, until_ms: int, reason: str) -> None:
        self.conn.execute(
            """
            INSERT INTO cooldowns (symbol, until_ms, reason) VALUES (?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET until_ms=excluded.until_ms, reason=excluded.reason
            """,
            (symbol, until_ms, reason),
        )
        self.conn.commit()

    def cooldown_until(self, symbol: str) -> int | None:
        row = self.conn.execute("SELECT until_ms FROM cooldowns WHERE symbol = ?", (symbol,)).fetchone()
        return None if row is None else int(row["until_ms"])

    def blocklist_symbols(self) -> set[str]:
        rows = self.conn.execute("SELECT symbol FROM blocklist WHERE active = 1")
        return {row["symbol"] for row in rows}

    def set_blocklist(self, symbol: str, reason: str, active: bool = True) -> None:
        self.conn.execute(
            """
            INSERT INTO blocklist (symbol, reason, active) VALUES (?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET reason=excluded.reason, active=excluded.active
            """,
            (symbol, reason, 1 if active else 0),
        )
        self.conn.commit()

    def vision_month_rows(self, symbol: str, dataset: str, month: str) -> int | None:
        row = self.conn.execute(
            "SELECT rows FROM vision_months WHERE symbol = ? AND dataset = ? AND month = ?",
            (symbol, dataset, month),
        ).fetchone()
        return None if row is None else int(row["rows"])

    def vision_months_for(self, symbol: str, dataset: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT month, rows FROM vision_months WHERE symbol = ? AND dataset = ?",
            (symbol, dataset),
        )
        return {row["month"]: int(row["rows"]) for row in rows}

    def mark_vision_month(self, symbol: str, dataset: str, month: str, nrows: int) -> None:
        self.conn.execute(
            """
            INSERT INTO vision_months (symbol, dataset, month, rows) VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol, dataset, month) DO UPDATE SET rows=excluded.rows
            """,
            (symbol, dataset, month, int(nrows)),
        )
        self.conn.commit()

    def write_vision_klines(self, symbol: str, interval: str, month: str, rows: list[dict]) -> None:
        """Insert one cached month and its ledger row in a single commit."""
        if rows:
            self.conn.executemany(
                """
                INSERT INTO klines (
                    symbol, interval, open_time, close_time, open, high, low, close, volume, quote_volume
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval, open_time) DO UPDATE SET
                    close_time=excluded.close_time,
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    quote_volume=excluded.quote_volume
                """,
                [
                    (
                        symbol,
                        interval,
                        int(row["open_time"]),
                        int(row["close_time"]),
                        row["open"],
                        row["high"],
                        row["low"],
                        row["close"],
                        row["volume"],
                        row["quote_volume"],
                    )
                    for row in rows
                ],
            )
        self.conn.execute(
            """
            INSERT INTO vision_months (symbol, dataset, month, rows) VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol, dataset, month) DO UPDATE SET rows=excluded.rows
            """,
            (symbol, interval, month, len(rows)),
        )
        self.conn.commit()

    def write_vision_funding(self, symbol: str, month: str, rows: list[dict]) -> None:
        if rows:
            self.conn.executemany(
                """
                INSERT INTO funding (symbol, funding_time, funding_rate, interval_hours) VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol, funding_time) DO UPDATE SET
                    funding_rate=excluded.funding_rate,
                    interval_hours=COALESCE(excluded.interval_hours, funding.interval_hours)
                """,
                [
                    (
                        symbol,
                        int(row["funding_time"]),
                        float(row["funding_rate"]),
                        row.get("interval_hours"),
                    )
                    for row in rows
                ],
            )
        self.conn.execute(
            """
            INSERT INTO vision_months (symbol, dataset, month, rows) VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol, dataset, month) DO UPDATE SET rows=excluded.rows
            """,
            (symbol, "funding", month, len(rows)),
        )
        self.conn.commit()

    def max_daily_quote_volume(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT symbol, MAX(quote_volume) AS max_quote_volume, COUNT(*) AS bars,
                   MIN(open_time) AS first_open, MAX(open_time) AS last_open
            FROM klines
            WHERE interval = '1d'
            GROUP BY symbol
            """
        )
        return [dict(row) for row in rows]

    def load_interval_frame(self, interval: str) -> pd.DataFrame:
        return pd.read_sql_query(
            """
            SELECT symbol, open_time, open, high, low, close, quote_volume
            FROM klines WHERE interval = ?
            """,
            self.conn,
            params=[interval],
        )

    def load_funding_frame(self) -> pd.DataFrame:
        return pd.read_sql_query(
            "SELECT symbol, funding_time, funding_rate, interval_hours FROM funding",
            self.conn,
        )

    def load_metrics_frame(self) -> pd.DataFrame:
        return pd.read_sql_query(
            """
            SELECT symbol, day_ms, create_time, sum_open_interest, sum_open_interest_value
            FROM metrics_daily
            """,
            self.conn,
        )

    def upsert_metrics_daily(self, rows: list[dict]) -> None:
        if not rows:
            return
        self.conn.executemany(
            """
            INSERT INTO metrics_daily (
                symbol, day_ms, create_time, sum_open_interest, sum_open_interest_value,
                count_long_short_ratio, sum_taker_long_short_vol_ratio
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, day_ms) DO UPDATE SET
                create_time=excluded.create_time,
                sum_open_interest=excluded.sum_open_interest,
                sum_open_interest_value=excluded.sum_open_interest_value,
                count_long_short_ratio=excluded.count_long_short_ratio,
                sum_taker_long_short_vol_ratio=excluded.sum_taker_long_short_vol_ratio
            """,
            [
                (
                    row["symbol"],
                    int(row["day_ms"]),
                    int(row["create_time"]),
                    row.get("sum_open_interest"),
                    row.get("sum_open_interest_value"),
                    row.get("count_long_short_ratio"),
                    row.get("sum_taker_long_short_vol_ratio"),
                )
                for row in rows
            ],
        )
        self.conn.commit()

    def vision_day_done(self, symbol: str, dataset: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT day FROM vision_days WHERE symbol = ? AND dataset = ?",
            (symbol, dataset),
        )
        return {row["day"] for row in rows}

    def write_metrics_batch(self, metric_rows: list[dict], day_marks: list[tuple]) -> None:
        """Insert daily metrics and resume markers in one transaction.

        `day_marks` is (symbol, dataset, day, rows).
        """
        if metric_rows:
            self.conn.executemany(
                """
                INSERT INTO metrics_daily (
                    symbol, day_ms, create_time, sum_open_interest, sum_open_interest_value,
                    count_long_short_ratio, sum_taker_long_short_vol_ratio
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, day_ms) DO UPDATE SET
                    create_time=excluded.create_time,
                    sum_open_interest=excluded.sum_open_interest,
                    sum_open_interest_value=excluded.sum_open_interest_value,
                    count_long_short_ratio=excluded.count_long_short_ratio,
                    sum_taker_long_short_vol_ratio=excluded.sum_taker_long_short_vol_ratio
                """,
                [
                    (
                        row["symbol"],
                        int(row["day_ms"]),
                        int(row["create_time"]),
                        row.get("sum_open_interest"),
                        row.get("sum_open_interest_value"),
                        row.get("count_long_short_ratio"),
                        row.get("sum_taker_long_short_vol_ratio"),
                    )
                    for row in metric_rows
                ],
            )
        if day_marks:
            self.conn.executemany(
                """
                INSERT INTO vision_days (symbol, dataset, day, rows) VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol, dataset, day) DO UPDATE SET rows=excluded.rows
                """,
                day_marks,
            )
        self.conn.commit()

    def mark_vision_day(self, symbol: str, dataset: str, day: str, nrows: int) -> None:
        self.conn.execute(
            """
            INSERT INTO vision_days (symbol, dataset, day, rows) VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol, dataset, day) DO UPDATE SET rows=excluded.rows
            """,
            (symbol, dataset, day, int(nrows)),
        )
        self.conn.commit()

    def symbols_with_interval(self, interval: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT symbol FROM klines WHERE interval = ? ORDER BY symbol",
            (interval,),
        )
        return [row["symbol"] for row in rows]

    def journal(self, ts_ms: int, kind: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO journal (ts_ms, kind, payload) VALUES (?, ?, ?)",
            (ts_ms, kind, json.dumps(payload, default=str)),
        )
        self.conn.commit()

    def get_signal(self, signal_id: str) -> dict | None:
        row = self.conn.execute("SELECT payload FROM signals WHERE signal_id = ?", (signal_id,)).fetchone()
        if row is None:
            return None
        return json.loads(row["payload"])

    def list_signal_packets(self, status: str | None = None) -> list[dict]:
        if status is None:
            rows = self.conn.execute("SELECT payload FROM signals ORDER BY ts_ms, signal_id")
        else:
            rows = self.conn.execute(
                "SELECT payload FROM signals WHERE status = ? ORDER BY ts_ms, signal_id",
                (status,),
            )
        return [json.loads(row["payload"]) for row in rows]

    def outbox_by_key(self, idempotency_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM webhook_outbox WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return dict(row) if row else None

    def insert_outbox(self, row: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO webhook_outbox (
                signal_id, idempotency_key, body, status, attempts, next_attempt_ms,
                last_error, created_ms, updated_ms, delivered_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["signal_id"],
                row["idempotency_key"],
                row["body"],
                row["status"],
                int(row["attempts"]),
                int(row["next_attempt_ms"]),
                row.get("last_error"),
                int(row["created_ms"]),
                int(row["updated_ms"]),
                row.get("delivered_ms"),
            ),
        )
        self.conn.commit()

    def due_outbox(self, now_ms: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT * FROM webhook_outbox
            WHERE status = 'pending' AND next_attempt_ms <= ?
            ORDER BY id
            """,
            (int(now_ms),),
        )
        return [dict(row) for row in rows]

    def update_outbox(self, idempotency_key: str, **fields) -> None:
        allowed = {
            "status",
            "attempts",
            "next_attempt_ms",
            "last_error",
            "updated_ms",
            "delivered_ms",
        }
        keys = [key for key in fields if key in allowed]
        if not keys:
            return
        assignments = ", ".join(f"{key} = ?" for key in keys)
        values = [fields[key] for key in keys]
        values.append(idempotency_key)
        self.conn.execute(
            f"UPDATE webhook_outbox SET {assignments} WHERE idempotency_key = ?",
            values,
        )
        self.conn.commit()

    def append_decision_journal(self, ts_ms: int, signal_id: str, event: str, payload: dict) -> int:
        """Append one journal row. Rows are never updated or deleted."""
        cursor = self.conn.execute(
            "INSERT INTO decision_journal (ts_ms, signal_id, event, payload) VALUES (?, ?, ?, ?)",
            (int(ts_ms), signal_id, event, json.dumps(payload, sort_keys=True, default=str)),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def read_decision_journal(self, signal_id: str | None = None) -> list[dict]:
        if signal_id is None:
            rows = self.conn.execute("SELECT * FROM decision_journal ORDER BY id")
        else:
            rows = self.conn.execute(
                "SELECT * FROM decision_journal WHERE signal_id = ? ORDER BY id",
                (signal_id,),
            )
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            out.append(item)
        return out

    def append_review(self, row: dict) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO reviews (
                signal_id, ts_ms, market_context, requested_decision, effective_decision,
                reasoning, decision_ts, override_refused, reason, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["signal_id"],
                int(row["ts_ms"]),
                json.dumps(row["market_context"], sort_keys=True, default=str),
                row["requested_decision"],
                row["effective_decision"],
                row.get("reasoning"),
                row.get("decision_ts"),
                1 if row.get("override_refused") else 0,
                row["reason"],
                json.dumps(row, sort_keys=True, default=str),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def latest_review(self, signal_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM reviews WHERE signal_id = ? ORDER BY id DESC LIMIT 1",
            (signal_id,),
        ).fetchone()
        return _review_dict(row) if row else None

    def list_reviews(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM reviews ORDER BY id")
        return [_review_dict(row) for row in rows]

    def paper_account(self) -> dict | None:
        row = self.conn.execute("SELECT * FROM paper_account WHERE id = 1").fetchone()
        return dict(row) if row else None

    def save_paper_account(self, row: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO paper_account (
                id, initial_equity, equity_usdt, day_start_equity, week_start_equity,
                day_key, week_key, as_of_ms, started_at_ms, last_block, updated_ms
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                initial_equity=excluded.initial_equity,
                equity_usdt=excluded.equity_usdt,
                day_start_equity=excluded.day_start_equity,
                week_start_equity=excluded.week_start_equity,
                day_key=excluded.day_key,
                week_key=excluded.week_key,
                as_of_ms=excluded.as_of_ms,
                started_at_ms=excluded.started_at_ms,
                last_block=excluded.last_block,
                updated_ms=excluded.updated_ms
            """,
            (
                row.get("initial_equity"),
                row.get("equity_usdt"),
                row.get("day_start_equity"),
                row.get("week_start_equity"),
                row.get("day_key"),
                row.get("week_key"),
                row.get("as_of_ms"),
                row.get("started_at_ms"),
                row.get("last_block"),
                int(row["updated_ms"]),
            ),
        )
        self.conn.commit()

    def save_paper_position(self, symbol: str, payload: str, updated_ms: int) -> None:
        self.conn.execute(
            """
            INSERT INTO paper_positions (symbol, payload, updated_ms) VALUES (?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET payload=excluded.payload, updated_ms=excluded.updated_ms
            """,
            (symbol, payload, int(updated_ms)),
        )
        self.conn.commit()

    def delete_paper_position(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM paper_positions WHERE symbol = ?", (symbol,))
        self.conn.commit()

    def paper_position_rows(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM paper_positions ORDER BY symbol")
        return [dict(row) for row in rows]

    def save_book_position(self, book: str, symbol: str, payload: str, updated_ms: int) -> None:
        self.conn.execute(
            """
            INSERT INTO paper_book_positions (book, symbol, payload, updated_ms)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(book, symbol) DO UPDATE SET
                payload=excluded.payload, updated_ms=excluded.updated_ms
            """,
            (book, symbol, payload, int(updated_ms)),
        )
        self.conn.commit()

    def delete_book_position(self, book: str, symbol: str) -> None:
        self.conn.execute(
            "DELETE FROM paper_book_positions WHERE book = ? AND symbol = ?",
            (book, symbol),
        )
        self.conn.commit()

    def book_position_rows(self, book: str | None = None) -> list[dict]:
        if book is None:
            rows = self.conn.execute("SELECT * FROM paper_book_positions ORDER BY book, symbol")
        else:
            rows = self.conn.execute(
                "SELECT * FROM paper_book_positions WHERE book = ? ORDER BY symbol",
                (book,),
            )
        return [dict(row) for row in rows]

    def book_meta(self, book: str) -> dict | None:
        row = self.conn.execute("SELECT payload FROM paper_book_meta WHERE book = ?", (book,)).fetchone()
        if row is None:
            return None
        return json.loads(row["payload"])

    def save_book_meta(self, book: str, payload: dict, updated_ms: int) -> None:
        self.conn.execute(
            """
            INSERT INTO paper_book_meta (book, payload, updated_ms) VALUES (?, ?, ?)
            ON CONFLICT(book) DO UPDATE SET payload=excluded.payload, updated_ms=excluded.updated_ms
            """,
            (book, json.dumps(payload, sort_keys=True, default=str), int(updated_ms)),
        )
        self.conn.commit()

    def save_paper_trade(self, trade: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO paper_trades (signal_id, symbol, exit_ms, payload)
            VALUES (?, ?, ?, ?)
            """,
            (
                trade["signal_id"],
                trade["symbol"],
                int(trade["exit_time"]),
                json.dumps(trade, sort_keys=True, default=str),
            ),
        )
        self.conn.commit()

    def list_paper_trades(self) -> list[dict]:
        rows = self.conn.execute("SELECT payload FROM paper_trades ORDER BY id")
        return [json.loads(row["payload"]) for row in rows]

    def append_equity_point(self, ts_ms: int, equity: float) -> None:
        self.conn.execute(
            "INSERT INTO paper_equity (ts_ms, equity) VALUES (?, ?)",
            (int(ts_ms), float(equity)),
        )
        self.conn.commit()

    def equity_curve(self) -> list[tuple[int, float]]:
        rows = self.conn.execute("SELECT ts_ms, equity FROM paper_equity ORDER BY id")
        return [(int(row["ts_ms"]), float(row["equity"])) for row in rows]

    def latest_equity_point(self) -> float | None:
        row = self.conn.execute("SELECT equity FROM paper_equity ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else float(row["equity"])

    def resolution(self, signal_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM paper_resolutions WHERE signal_id = ?",
            (signal_id,),
        ).fetchone()
        return dict(row) if row else None

    def save_resolution(self, signal_id: str, outcome: str, ts_ms: int, detail: str) -> None:
        self.conn.execute(
            """
            INSERT INTO paper_resolutions (signal_id, outcome, ts_ms, detail)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(signal_id) DO UPDATE SET
                outcome=excluded.outcome,
                ts_ms=excluded.ts_ms,
                detail=excluded.detail
            """,
            (signal_id, outcome, int(ts_ms), detail),
        )
        self.conn.commit()

    def append_paper_event(self, ts_ms: int, klass: str, kind: str, signal_id: str | None, detail: str) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO paper_violations (ts_ms, class, kind, signal_id, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(ts_ms), klass, kind, signal_id, detail),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_paper_events(self, klass: str | None = None) -> list[dict]:
        if klass is None:
            rows = self.conn.execute("SELECT * FROM paper_violations ORDER BY id")
        else:
            rows = self.conn.execute(
                "SELECT * FROM paper_violations WHERE class = ? ORDER BY id",
                (klass,),
            )
        return [dict(row) for row in rows]


def _review_dict(row) -> dict:
    item = dict(row)
    item["market_context"] = json.loads(item["market_context"])
    item["override_refused"] = bool(item["override_refused"])
    item["payload"] = json.loads(item["payload"])
    return item
