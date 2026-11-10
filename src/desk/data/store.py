"""SQLite cache for klines, funding, signals, cooldowns, and the blocklist."""

from __future__ import annotations

import json
import sqlite3
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
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

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
            INSERT INTO funding (symbol, funding_time, funding_rate) VALUES (?, ?, ?)
            ON CONFLICT(symbol, funding_time) DO UPDATE SET funding_rate=excluded.funding_rate
            """,
            [(symbol, int(row["funding_time"]), float(row["funding_rate"])) for row in rows],
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
                INSERT INTO funding (symbol, funding_time, funding_rate) VALUES (?, ?, ?)
                ON CONFLICT(symbol, funding_time) DO UPDATE SET funding_rate=excluded.funding_rate
                """,
                [(symbol, int(row["funding_time"]), float(row["funding_rate"])) for row in rows],
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
