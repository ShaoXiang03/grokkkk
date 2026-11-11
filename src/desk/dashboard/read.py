"""Read the paper desk without taking a write lock.

The connection uses SQLite URI mode=ro and PRAGMA query_only. It does not
run the schema script and it does not set journal_mode, so it cannot create
the database or rewrite it. In WAL mode a reader does not block the paper
writer. Each snapshot opens a connection and closes it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

from desk.backtest.costs import directional_pnl
from desk.backtest.metrics import max_drawdown
from desk.backtest.replay import position_risk_usdt
from desk.dashboard.views import (
    clock_view,
    cycle_plain,
    drift_plain,
    format_myt,
    interval_plain,
    journal_labels,
    journal_summary,
    kill_view,
    news_view,
    next_decision_windows,
    orders_view,
    outbox_plain,
    parse_strategy_report,
    refresh_plain,
    review_status_of,
    scrub,
    sensitive_key,
    service_view,
    signal_hint,
    stale_view,
)
from desk.data.budget import WeightBudget
from desk.paper.books import BookPosition, enabled_specs, load_book_position
from desk.paper.clock import paper_clock
from desk.paper.engine import cycle_symbols
from desk.paper.freshness import data_is_fresh, setups_enabled
from desk.paper.scorecard import build_scorecard
from desk.paper.state import load_position
from desk.timeutils import DAY_MS, last_closed_daily_open

SERVICE_UNIT = "chart-analyst-paper.service"
DEFAULT_SECRETS_ENV = Path("/etc/chart-analyst/secrets.env")
JOURNAL_LIMIT = 400
SIGNAL_LIMIT = 80
CHART_LIMIT = 480
REFRESH_STALE_MS = 15 * 60 * 1000

_SECRET_ENV_PARTS = ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "URL")


def open_readonly(path: str | Path) -> sqlite3.Connection:
    """Open an existing database read-only. Missing files raise FileNotFoundError."""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"sqlite database is not there: {target}")
    uri = "file:" + quote(target.resolve().as_posix(), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=2000")
    conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
    return conn


def resolve_sqlite_path(settings: dict) -> Path:
    raw = Path((settings.get("data") or {}).get("sqlite_path") or "data/desk.sqlite")
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    return raw


def read_named_assignment(path: str | Path | None, name: str) -> str | None:
    """Return one KEY=value from an env file. Every other line is ignored."""
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    prefix = name + "="
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].strip() or None
    return None


def secret_denylist(settings: dict, secrets_env_path: str | Path | None) -> set[str]:
    """Values that must never appear in a response. Short strings are ignored."""
    found: set[str] = set()
    webhook = (settings.get("webhook") or {}).get("url")
    if isinstance(webhook, str) and len(webhook) >= 8:
        found.add(webhook)
    for key, value in (settings.get("account") or {}).items():
        if sensitive_key(str(key)) and isinstance(value, str) and len(value) >= 8:
            found.add(value)
    if secrets_env_path:
        try:
            lines = Path(secrets_env_path).read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            upper = key.strip().upper()
            if upper == "KILL_SWITCH_PATH":
                continue
            if any(part in upper for part in _SECRET_ENV_PARTS):
                secret = value.strip()
                if len(secret) >= 8:
                    found.add(secret)
    return found


def resolve_kill_path(settings: dict, secrets_env_path, environ: dict | None = None) -> Path:
    env = os.environ if environ is None else environ
    raw = env.get("KILL_SWITCH_PATH") or read_named_assignment(secrets_env_path, "KILL_SWITCH_PATH")
    if not raw:
        raw = (settings.get("paper") or {}).get("kill_switch_path") or "data/KILL"
    path = Path(str(raw))
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def systemd_active(unit: str = SERVICE_UNIT) -> str | None:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (proc.stdout or "").strip().lower()
    return text or None


def build_snapshot(
    settings: dict,
    *,
    now_ms: int | None = None,
    sqlite_path: str | Path | None = None,
    secrets_env_path: str | Path | None = DEFAULT_SECRETS_ENV,
    service_state: str | None = None,
    probe_service: bool = True,
    environ: dict | None = None,
    report_path: str | Path | None = None,
) -> dict:
    """One JSON document for the page. Read-only. Secrets are scrubbed."""
    import time

    now = int(time.time() * 1000 if now_ms is None else now_ms)
    path = Path(sqlite_path) if sqlite_path is not None else resolve_sqlite_path(settings)
    denylist = secret_denylist(settings, secrets_env_path)
    conn = open_readonly(path)
    try:
        reader = Reader(conn, now)
        payload = _assemble(
            reader,
            settings,
            now,
            db_path=path,
            secrets_env_path=secrets_env_path,
            service_state=service_state,
            probe_service=probe_service,
            environ=environ,
            report_path=report_path,
        )
    finally:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        conn.close()
    from desk.dashboard.views import json_safe

    return scrub(json_safe(payload), denylist)


class Reader:
    """The slice of Store the scorecard and freshness gate already call."""

    def __init__(self, conn: sqlite3.Connection, now_ms: int):
        self.conn = conn
        self.now_ms = int(now_ms)
        self.daily_close = {
            row["symbol"]: int(row["close_time"])
            for row in self._all(
                """
                SELECT symbol, MAX(close_time) AS close_time
                FROM klines
                WHERE interval = '1d' AND close_time <= ?
                GROUP BY symbol
                """,
                (self.now_ms,),
            )
            if row["symbol"] is not None and row["close_time"] is not None
        }

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        try:
            return list(self.conn.execute(sql, params))
        except sqlite3.OperationalError:
            return []

    def _one(self, sql: str, params: tuple = ()):
        rows = self._all(sql, params)
        return rows[0] if rows else None

    def paper_account(self) -> dict | None:
        row = self._one("SELECT * FROM paper_account WHERE id = 1")
        return dict(row) if row else None

    def list_paper_trades(self) -> list[dict]:
        out = []
        for row in self._all("SELECT payload FROM paper_trades ORDER BY id"):
            try:
                out.append(json.loads(row["payload"]))
            except (TypeError, json.JSONDecodeError):
                continue
        return out

    def equity_curve(self) -> list[tuple[int, float]]:
        return [(int(row["ts_ms"]), float(row["equity"])) for row in self._all("SELECT ts_ms, equity FROM paper_equity ORDER BY id")]

    def list_reviews(self) -> list[dict]:
        out = []
        for row in self._all("SELECT * FROM reviews ORDER BY id"):
            item = dict(row)
            item["override_refused"] = bool(item.get("override_refused"))
            for key in ("market_context", "payload"):
                try:
                    item[key] = json.loads(item[key]) if isinstance(item.get(key), str) else item.get(key)
                except (TypeError, json.JSONDecodeError):
                    item[key] = {}
            out.append(item)
        return out

    def list_paper_events(self, klass: str | None = None) -> list[dict]:
        if klass is None:
            rows = self._all("SELECT * FROM paper_violations ORDER BY id")
        else:
            rows = self._all("SELECT * FROM paper_violations WHERE class = ? ORDER BY id", (klass,))
        return [dict(row) for row in rows]

    def book_position_rows(self, book: str | None = None) -> list[dict]:
        if book is None:
            rows = self._all("SELECT * FROM paper_book_positions ORDER BY book, symbol")
        else:
            rows = self._all(
                "SELECT * FROM paper_book_positions WHERE book = ? ORDER BY symbol",
                (book,),
            )
        return [dict(row) for row in rows]

    def paper_position_rows(self) -> list[dict]:
        return [dict(row) for row in self._all("SELECT * FROM paper_positions ORDER BY symbol")]

    def list_symbols(self, trading_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM symbols WHERE contract_type = 'PERPETUAL' AND quote_asset = 'USDT'"
        if trading_only:
            sql += " AND status = 'TRADING'"
        sql += " ORDER BY symbol"
        return [dict(row) for row in self._all(sql)]

    def symbols_with_interval(self, interval: str) -> list[str]:
        return [row["symbol"] for row in self._all("SELECT DISTINCT symbol FROM klines WHERE interval = ? ORDER BY symbol", (interval,))]

    def latest_close_time(self, symbol: str, interval: str, end_ms: int | None = None) -> int | None:
        if interval == "1d" and (end_ms is None or int(end_ms) >= self.now_ms):
            return self.daily_close.get(symbol)
        if end_ms is None:
            row = self._one(
                "SELECT MAX(close_time) AS t FROM klines WHERE symbol = ? AND interval = ?",
                (symbol, interval),
            )
        else:
            row = self._one(
                "SELECT MAX(close_time) AS t FROM klines WHERE symbol = ? AND interval = ? AND close_time <= ?",
                (symbol, interval, int(end_ms)),
            )
        if row is None or row["t"] is None:
            return None
        return int(row["t"])

    def latest_close(self, symbol: str, interval: str) -> float | None:
        row = self._one(
            """
            SELECT close FROM klines
            WHERE symbol = ? AND interval = ? AND close_time <= ?
            ORDER BY open_time DESC LIMIT 1
            """,
            (symbol, interval, self.now_ms),
        )
        if row is None or row["close"] is None:
            return None
        price = float(row["close"])
        return price if price > 0 else None

    def closes(self, symbol: str, interval: str) -> list[tuple[int, float]]:
        rows = self._all(
            """
            SELECT close_time, close FROM klines
            WHERE symbol = ? AND interval = ? AND close_time <= ? AND close > 0
            ORDER BY open_time
            """,
            (symbol, interval, self.now_ms),
        )
        return [(int(row["close_time"]), float(row["close"])) for row in rows]

    def freshness_rows(self) -> list[dict]:
        return [dict(row) for row in self._all(
            """
            SELECT interval, MAX(close_time) AS close_time, COUNT(*) AS bars, COUNT(DISTINCT symbol) AS symbols
            FROM klines GROUP BY interval
            """
        )]

    def vision_archive(self) -> list[dict]:
        return [dict(row) for row in self._all(
            "SELECT dataset, COUNT(*) AS n, MAX(day) AS latest FROM vision_days GROUP BY dataset"
        )]

    def signal_rows(self, limit: int) -> list[dict]:
        return [dict(row) for row in self._all(
            """
            SELECT signal_id, symbol, setup, direction, ts_ms, status, payload
            FROM signals ORDER BY ts_ms DESC, signal_id DESC LIMIT ?
            """,
            (int(limit),),
        )]

    def count_signals(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM signals")
        return 0 if row is None else int(row["n"])

    def journal_rows(self, limit: int) -> list[dict]:
        return [dict(row) for row in self._all(
            """
            SELECT id, ts_ms, signal_id, event, payload
            FROM decision_journal ORDER BY id DESC LIMIT ?
            """,
            (int(limit),),
        )]

    def count_journal(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM decision_journal")
        return 0 if row is None else int(row["n"])

    def events_for(self, signal_ids: list[str], names: tuple[str, ...]) -> list[dict]:
        if not signal_ids:
            return []
        marks = ",".join("?" for _ in signal_ids)
        event_marks = ",".join("?" for _ in names)
        sql = (
            f"SELECT signal_id, event, ts_ms, payload FROM decision_journal "
            f"WHERE signal_id IN ({marks}) AND event IN ({event_marks}) ORDER BY id"
        )
        return [dict(row) for row in self._all(sql, tuple(signal_ids) + tuple(names))]

    def outbox_rows(self) -> list[dict]:
        return [dict(row) for row in self._all(
            """
            SELECT id, signal_id, status, attempts, next_attempt_ms, last_error,
                   created_ms, updated_ms, delivered_ms
            FROM webhook_outbox ORDER BY id DESC LIMIT 50
            """
        )]

    def outbox_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self._all("SELECT status, COUNT(*) AS n FROM webhook_outbox GROUP BY status"):
            counts[str(row["status"])] = int(row["n"])
        return counts


def _assemble(reader: Reader, settings, now_ms, *, db_path, secrets_env_path, service_state, probe_service, environ, report_path) -> dict:
    account = reader.paper_account()
    kill_path = resolve_kill_path(settings, secrets_env_path, environ)
    kill = _read_kill(kill_path)
    news = news_view((settings.get("account") or {}).get("news_status"))
    mode = orders_view(settings.get("mode"))
    positions, warnings = _positions(reader)
    equity = _equity(reader, settings, positions)
    books = _books(reader, settings, now_ms, equity, positions, warnings)
    gates_stale, fresh_reason = _stale_gate(reader, settings, now_ms)
    if service_state is None and probe_service:
        service_state = systemd_active()
    health = _health(
        reader,
        settings,
        now_ms,
        account=account,
        news=news,
        kill=kill,
        mode=mode,
        stale=gates_stale,
        fresh_reason=fresh_reason,
        equity=equity,
        service_state=service_state,
        db_path=db_path,
    )
    started = None if account is None else account.get("started_at_ms")
    days = int((settings.get("paper") or {}).get("clock_days") or 14)
    clock = clock_view(paper_clock(None if started is None else int(started), now_ms, days))
    timeout = int((settings.get("decision") or {}).get("timeout_seconds") or 3600)
    windows = next_decision_windows(now_ms, timeout)
    signals = _signals(reader, now_ms, timeout * 1000)
    outbox = _outbox(reader, settings)
    journal = _journal(reader)
    trades = _trades(reader)
    scorecard, expectations = _scorecard(reader, settings, now_ms, report_path)
    return {
        "read_only": True,
        "orders": False,
        "generated_at_ms": now_ms,
        "generated_at_myt": format_myt(now_ms, seconds=True),
        "warnings": warnings,
        "health": health,
        "clock": clock,
        "windows": windows,
        "books": books,
        "signals": signals["rows"],
        "signal_meta": signals["meta"],
        "outbox": outbox,
        "journal": journal["rows"],
        "journal_meta": journal["meta"],
        "trades": trades,
        "scorecard": scorecard,
        "expectations": expectations,
    }


def _read_kill(path: Path) -> dict:
    if not path.exists():
        return kill_view(False, None, str(path))
    try:
        text = path.read_text(encoding="utf-8")[:500].strip()
    except OSError:
        return kill_view(True, "kill switch file is unreadable", str(path))
    return kill_view(True, text or "kill switch file is present", str(path))


def _stale_gate(reader: Reader, settings, now_ms) -> tuple[dict, str]:
    try:
        symbols = cycle_symbols(settings, reader, None)
        ok, reason = data_is_fresh(reader, symbols, now_ms, settings)
    except Exception:
        return stale_view(True, "The freshness check could not be read."), "unreadable"
    return stale_view(not ok, reason), reason


def _health(reader, settings, now_ms, *, account, news, kill, mode, stale, fresh_reason, equity, service_state, db_path) -> dict:
    loop_s = float((settings.get("paper") or {}).get("loop_seconds") or 60)
    limit_ms = max(loop_s * 3, 180) * 1000
    updated = None if account is None else account.get("updated_ms")
    age_ms = None if updated is None else int(now_ms) - int(updated)
    recent = age_ms is not None and abs(age_ms) <= limit_ms
    age_seconds = None if age_ms is None else age_ms / 1000.0
    last_block = None if account is None else account.get("last_block")
    detail = fresh_reason if last_block == "stale_data" else None
    service = service_view(service_state, recent, age_seconds)
    budget = WeightBudget.from_settings(settings)
    process = _refresh_process(Path(db_path).parent / "refresh_status.json", now_ms)
    archive = _archive(reader)
    setups = setups_enabled(settings)
    return {
        "service": service,
        "mode": mode["mode"],
        "mode_plain": mode["plain"],
        "orders": False,
        "orders_plain": mode["plain"],
        "paper": mode["paper"],
        "rules_version": settings.get("rules_version"),
        "roster": _roster(settings),
        "cycle": {
            "last_ms": None if updated is None else int(updated),
            "last_myt": None if updated is None else format_myt(int(updated), seconds=True),
            "age_seconds": age_seconds,
            "recent": recent,
            "last_block": last_block,
            "plain": cycle_plain(last_block, news["blocking"], recent, detail),
        },
        "freshness": _freshness(reader, settings, now_ms, setups),
        "gates": {
            "news": news,
            "kill": kill,
            "stale_data": stale,
            "equity": _equity_gate(equity, account),
        },
        "refresh": {
            "weight_per_minute": budget.max_weight,
            "pause_above": budget.pause_above,
            "archive": archive,
            "process": process,
            "plain": refresh_plain(process, weight=budget.max_weight, pause_above=budget.pause_above),
        },
    }


def _equity_gate(equity: dict, account) -> dict:
    if equity.get("readable"):
        return {
            "readable": True,
            "tone": "ok",
            "equity": equity.get("marked"),
            "plain": f"Paper equity can be marked from the local ledger ({_money(equity.get('marked'))}). Binance account equity is not read. There is no key.",
        }
    if account is None:
        plain = "The paper ledger has not been created yet. The clock starts on the first paper cycle."
    else:
        plain = "Paper equity could not be marked from the local ledger, so new entries are refused."
    return {"readable": False, "tone": "bad", "equity": equity.get("marked"), "plain": plain}


def _money(value) -> str:
    if value is None:
        return "unknown"
    return f"{value:,.2f} USDT"


def _roster(settings: dict) -> dict:
    labels = {
        "e1_cs_momentum": "E1",
        "e2_residual_momentum": "E2",
        "e3_slow_trend": "E3",
        "e4_funding_carry": "E4",
        "e5_positioning_trend": "E5",
        "e6_liquidation_proxy": "E6",
    }
    on = []
    off = []
    for spec in enabled_specs(settings):
        on.append(
            {
                "book": spec["book"],
                "name": spec["family"],
                "variant": spec["variant"],
                "cadence": spec["cadence"],
            }
        )
    live = {row["name"] for row in on}
    for key, name in labels.items():
        cfg = (settings.get("strategies") or {}).get(key) or {}
        if name not in live:
            off.append(name if cfg.get("enabled") else name)
    setups_on = []
    setup_names = {
        "squeeze_breakout": "A",
        "trend_pullback": "B",
        "range_reclaim": "C",
        "meme_momentum": "D",
    }
    for key, letter in setup_names.items():
        cfg = (settings.get("setups") or {}).get(key) or {}
        if isinstance(cfg, dict) and cfg.get("enabled"):
            setups_on.append(letter)
    if setups_on:
        on.append({"book": "setups", "name": "Setups " + ",".join(setups_on), "variant": None, "cadence": "intraday"})
    else:
        off.append("setups A–D")
    live_names = ", ".join(row["name"] for row in on) or "none"
    off_names = ", ".join(off) or "none"
    return {
        "on": on,
        "off": off,
        "plain": f"{live_names} on. {off_names} off.",
    }


def _freshness(reader: Reader, settings, now_ms: int, setups_on: bool) -> list[dict]:
    by_interval = {row["interval"]: row for row in reader.freshness_rows()}
    max_age_ms = int((settings.get("paper") or {}).get("max_data_age_seconds") or 5400) * 1000
    expected_close = last_closed_daily_open(now_ms) + DAY_MS
    rows = []
    for interval in ("1h", "4h", "1d"):
        found = by_interval.get(interval)
        latest = None if found is None or found.get("close_time") is None else int(found["close_time"])
        if latest is None:
            fresh = False
        elif interval == "1d":
            fresh = latest + max_age_ms >= expected_close
        else:
            fresh = int(now_ms) - latest <= max_age_ms
        rows.append(
            {
                "interval": interval,
                "fresh": fresh,
                "latest_close_ms": latest,
                "latest_close_myt": None if latest is None else format_myt(latest),
                "symbols": 0 if found is None else int(found.get("symbols") or 0),
                "bars": 0 if found is None else int(found.get("bars") or 0),
                "plain": interval_plain(interval, latest, now_ms, fresh, setups_on),
            }
        )
    return rows


def _refresh_process(path: Path, now_ms: int) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {"unreadable": True, "stale": True, "plain_state": "unreadable"}
    if not isinstance(payload, dict):
        return {"unreadable": True, "stale": True}
    updated = payload.get("updated_ms")
    age = None if updated is None else int(now_ms) - int(updated)
    stale = age is None or age > REFRESH_STALE_MS
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    last = payload.get("last_slice") if isinstance(payload.get("last_slice"), dict) else {}
    return {
        "unreadable": False,
        "updated_ms": None if updated is None else int(updated),
        "updated_myt": None if updated is None else format_myt(int(updated), seconds=True),
        "age_seconds": None if age is None else age / 1000.0,
        "stale": stale,
        "paused": bool(payload.get("paused")),
        "pause_reason": payload.get("pause_reason"),
        "totals": {key: int(totals.get(key) or 0) for key in (
            "vision_files", "vision_rows", "fapi_requests", "fapi_rows", "funding_requests",
            "errors", "pauses", "http_429", "http_418", "slices",
        )},
        "last_slice": last,
    }


def _archive(reader: Reader) -> dict:
    rows = []
    total = 0
    latest = None
    for row in reader.vision_archive():
        count = int(row.get("n") or 0)
        total += count
        day = row.get("latest")
        if day and (latest is None or str(day) > latest):
            latest = str(day)
        rows.append({"dataset": row.get("dataset"), "days": count, "latest": day})
    return {"days": total, "latest_day": latest, "by_dataset": rows}


def _positions(reader: Reader) -> tuple[list[dict], list[str]]:
    warnings = []
    positions = []
    for row in reader.book_position_rows():
        try:
            pos = load_book_position(row["payload"])
        except (TypeError, json.JSONDecodeError, KeyError, ValueError):
            warnings.append(f"Skipped an unreadable {row.get('book')} position.")
            continue
        mark = reader.latest_close(pos.symbol, "1d")
        mark_source = "1d"
        if mark is None:
            mark = pos.last_mark if pos.last_mark else None
            mark_source = "stored"
            warnings.append(f"No daily close for {pos.symbol}. Showing the mark stored on the position.")
        positions.append(_book_position_view(pos, mark, mark_source))
    for row in reader.paper_position_rows():
        try:
            pos = load_position(row["payload"])
        except (TypeError, json.JSONDecodeError, KeyError, ValueError):
            warnings.append(f"Skipped an unreadable setup position on {row.get('symbol')}.")
            continue
        mark = reader.latest_close(pos.symbol, "1h")
        mark_source = "1h"
        if mark is None:
            mark_source = "missing"
            warnings.append(f"No 1h close for {pos.symbol}.")
        positions.append(_setup_position_view(pos, mark, mark_source))
    return positions, warnings


def _book_position_view(pos: BookPosition, mark: float | None, mark_source: str) -> dict:
    signed = float(pos.qty)
    unrealized = None if mark is None else signed * (float(mark) - float(pos.entry_price))
    ledger = None if mark is None else float(pos.cash_impact) + signed * float(mark)
    risk = float(pos.risk_usdt or 0.0)
    initial = float(pos.initial_risk or 0.0) or risk
    notional = None if mark is None else abs(signed) * float(mark)
    return {
        "kind": "book",
        "book": pos.book,
        "symbol": pos.symbol,
        "side": "long" if int(pos.side) > 0 else "short",
        "size": abs(signed),
        "entry": float(pos.entry_price),
        "stop": float(pos.stop_price),
        "mark": None if mark is None else float(mark),
        "mark_source": mark_source,
        "r": None if unrealized is None or initial <= 0 else unrealized / initial,
        "unrealized_pnl": unrealized,
        "ledger_pnl": ledger,
        "risk_usdt": risk,
        "notional": notional,
        "cluster": pos.cluster or "alts",
        "signal_id": pos.signal_id,
    }


def _setup_position_view(pos, mark: float | None, mark_source: str) -> dict:
    remaining = float(pos.remaining)
    unrealized = None if mark is None else directional_pnl(pos.direction, remaining, float(pos.fill), float(mark))
    initial_risk = abs(float(pos.fill) - float(pos.initial_stop)) * abs(remaining)
    try:
        live_risk = float(position_risk_usdt(pos))
    except Exception:
        live_risk = initial_risk
    notional = None if mark is None else abs(remaining) * float(mark)
    side = "long" if pos.direction != "short" else "short"
    return {
        "kind": "setup",
        "book": "setup",
        "symbol": pos.symbol,
        "side": side,
        "size": abs(remaining),
        "entry": float(pos.fill),
        "stop": float(pos.stop),
        "mark": None if mark is None else float(mark),
        "mark_source": mark_source,
        "r": None if unrealized is None or initial_risk <= 0 else unrealized / initial_risk,
        "unrealized_pnl": unrealized,
        "ledger_pnl": None if unrealized is None else unrealized + float(pos.realized),
        "risk_usdt": live_risk,
        "notional": notional,
        "cluster": pos.cluster_key or "alts",
        "signal_id": pos.signal_id,
        "setup": pos.setup,
    }


def _equity(reader: Reader, settings, positions: list[dict]) -> dict:
    account = reader.paper_account() or {}
    initial = account.get("initial_equity")
    if initial is None:
        initial = (settings.get("paper") or {}).get("initial_equity")
    try:
        initial_f = None if initial is None else float(initial)
    except (TypeError, ValueError):
        initial_f = None
    realized = 0.0
    realized_ok = True
    for trade in reader.list_paper_trades():
        pnl = trade.get("pnl")
        if pnl is None:
            realized_ok = False
            break
        realized += float(pnl)
    open_pnl = 0.0
    missing = False
    for pos in positions:
        if pos.get("ledger_pnl") is None:
            missing = True
            break
        open_pnl += float(pos["ledger_pnl"])
    marked = None
    if initial_f is not None and initial_f > 0 and realized_ok and not missing:
        marked = initial_f + realized + open_pnl
    last = account.get("equity_usdt")
    try:
        last_f = None if last is None else float(last)
    except (TypeError, ValueError):
        last_f = None
    return {
        "initial": initial_f,
        "marked": marked,
        "last_cycle": last_f,
        "realized": realized if realized_ok else None,
        "open_pnl": None if missing else open_pnl,
        "readable": marked is not None and marked > 0,
    }


def _books(reader, settings, now_ms, equity, positions, warnings) -> dict:
    initial = equity.get("initial") or 0.0
    marked = equity.get("marked") if equity.get("readable") else equity.get("last_cycle")
    base = marked if marked else initial
    curve = reader.equity_curve()
    drawdown = max_drawdown([point[1] for point in curve]) if curve else None
    current_dd = _current_drawdown([point[1] for point in curve]) if curve else None
    started = None
    account = reader.paper_account() or {}
    if account.get("started_at_ms") is not None:
        started = int(account["started_at_ms"])
    btc = _btc(reader, started, initial if initial else None)
    risk = (settings.get("risk") or {})
    open_cap_pct = float(risk.get("max_open_risk_pct") or 0.06)
    cluster_cap_pct = float(risk.get("max_cluster_risk_pct") or 0.03)
    name_mult = float(risk.get("max_notional_equity_mult") or 3.0)
    total_risk = sum(float(pos.get("risk_usdt") or 0.0) for pos in positions)
    total_notional = sum(float(pos["notional"]) for pos in positions if pos.get("notional") is not None)
    open_cap = open_cap_pct * base if base else None
    clusters: dict[str, float] = defaultdict(float)
    names: dict[str, float] = defaultdict(float)
    for pos in positions:
        clusters[str(pos.get("cluster") or "alts")] += float(pos.get("risk_usdt") or 0.0)
        if pos.get("notional") is not None:
            names[pos["symbol"]] += float(pos["notional"])
    cluster_cap = cluster_cap_pct * base if base else None
    name_cap = name_mult * base if base else None
    by_book = {}
    for spec in enabled_specs(settings):
        by_book[spec["book"]] = _one_book(reader, spec, positions, initial, now_ms, base)
    desk_return = None if not initial or marked is None else (marked / initial) - 1.0
    btc_return = btc.get("return")
    return {
        "equity": marked,
        "initial_equity": initial or None,
        "last_cycle_equity": equity.get("last_cycle"),
        "desk_return": desk_return,
        "drawdown": drawdown,
        "current_drawdown": current_dd,
        "gross_leverage": (total_notional / base) if base else None,
        "gross_notional": total_notional,
        "desk_curve": _downsample([{"ms": ts, "equity": value} for ts, value in curve], CHART_LIMIT),
        "btc": btc,
        "vs_btc": None
        if desk_return is None or btc_return is None
        else {"desk_return": desk_return, "btc_return": btc_return, "difference": desk_return - btc_return},
        "open_risk": {
            "usdt": total_risk,
            "cap_usdt": open_cap,
            "cap_pct": open_cap_pct,
            "fraction_of_cap": None if not open_cap else total_risk / open_cap,
        },
        "clusters": [
            {
                "cluster": name,
                "risk_usdt": amount,
                "cap_usdt": cluster_cap,
                "cap_pct": cluster_cap_pct,
                "fraction_of_cap": None if not cluster_cap else amount / cluster_cap,
            }
            for name, amount in sorted(clusters.items())
        ],
        "names": [
            {
                "symbol": symbol,
                "notional": amount,
                "cap_usdt": name_cap,
                "cap_multiple": name_mult,
                "fraction_of_cap": None if not name_cap else amount / name_cap,
            }
            for symbol, amount in sorted(names.items(), key=lambda item: item[1], reverse=True)
        ],
        "by_book": by_book,
        "positions": positions,
        "plain": (
            "Marks on E2 and E3 use the latest daily close, the same price the paper books use. "
            "BTC buy-and-hold uses stored closes since the clock start. "
            "The two books share one ledger, so desk equity is not the sum of the book charts."
        ),
        "warnings": warnings,
    }


def _one_book(reader, spec, positions, initial, now_ms, equity_base) -> dict:
    book = spec["book"]
    held = [pos for pos in positions if pos.get("book") == book]
    trades = [trade for trade in reader.list_paper_trades() if trade.get("book") == book]
    realized = sum(float(trade.get("pnl") or 0.0) for trade in trades)
    open_pnl = sum(float(pos["ledger_pnl"]) for pos in held if pos.get("ledger_pnl") is not None)
    pnl = realized + open_pnl
    notional = sum(float(pos["notional"]) for pos in held if pos.get("notional") is not None)
    points = []
    if initial:
        account = reader.paper_account() or {}
        anchor = int(account["started_at_ms"]) if account.get("started_at_ms") is not None else int(now_ms)
        running = 0.0
        ordered = sorted(trades, key=lambda trade: int(trade.get("exit_time") or 0))
        points.append({"ms": anchor, "equity": initial})
        for trade in ordered:
            running += float(trade.get("pnl") or 0.0)
            points.append({"ms": int(trade.get("exit_time") or now_ms), "equity": initial + running})
        points.append({"ms": int(now_ms), "equity": initial + pnl})
    values = [point["equity"] for point in points]
    return {
        "book": book,
        "name": spec["family"],
        "variant": spec["variant"],
        "cadence": spec["cadence"],
        "enabled": True,
        "pnl": pnl,
        "return": (pnl / initial) if initial else None,
        "drawdown": max_drawdown(values) if len(values) > 1 else 0.0,
        "gross_leverage": (notional / equity_base) if equity_base else None,
        "notional": notional,
        "positions": held,
        "trades": len(trades),
        "curve": _downsample(points, 240),
    }


def _btc(reader: Reader, started_ms: int | None, initial: float | None) -> dict:
    if started_ms is None or not initial:
        return {"available": False, "plain": "BTC buy-and-hold needs a paper clock start and a starting equity."}
    series = reader.closes("BTCUSDT", "1h")
    interval = "1h"
    if not series:
        series = reader.closes("BTCUSDT", "1d")
        interval = "1d"
    if not series:
        return {"available": False, "plain": "No BTCUSDT close is stored, so buy-and-hold cannot be marked."}
    start_price = None
    for ts, close in series:
        if ts <= started_ms:
            start_price = close
    note = f"BTC 1x buy-and-hold from the {interval} close at or before the clock start."
    if start_price is None:
        start_price = series[0][1]
        note = "The first stored BTC close is after the clock start, so the hold starts on that bar."
    curve = [{"ms": int(started_ms), "equity": float(initial), "price": float(start_price)}]
    last_price = start_price
    for ts, close in series:
        if ts <= started_ms:
            continue
        last_price = close
        curve.append({"ms": int(ts), "equity": float(initial) * close / start_price, "price": float(close)})
    ret = (curve[-1]["equity"] / float(initial)) - 1.0
    return {
        "available": True,
        "interval": interval,
        "start_price": float(start_price),
        "last_price": float(last_price),
        "return": ret,
        "curve": _downsample(curve, CHART_LIMIT),
        "plain": note,
    }


def _current_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    peak = values[0]
    for value in values:
        if value > peak:
            peak = value
    if peak <= 0:
        return None
    return (peak - values[-1]) / peak


def _downsample(points: list[dict], limit: int) -> list[dict]:
    if len(points) <= limit:
        return points
    step = len(points) / float(limit)
    picked = [points[min(len(points) - 1, int(index * step))] for index in range(limit)]
    if picked[-1] is not points[-1]:
        picked.append(points[-1])
    return picked


def _signals(reader: Reader, now_ms: int, timeout_ms: int) -> dict:
    total = reader.count_signals()
    raw_rows = reader.signal_rows(SIGNAL_LIMIT)
    packets = []
    for row in raw_rows:
        try:
            packet = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(packet, dict):
            packets.append(packet)
    reviews = {}
    for review in reader.list_reviews():
        reviews[review["signal_id"]] = review
    ids = [packet.get("signal_id") for packet in packets if packet.get("signal_id")]
    fills = {}
    for event in reader.events_for([str(item) for item in ids], ("fill",)):
        try:
            payload = json.loads(event["payload"]) if isinstance(event["payload"], str) else event["payload"]
        except (TypeError, json.JSONDecodeError):
            payload = {}
        fills[event["signal_id"]] = payload if isinstance(payload, dict) else {}
    outbox_status = {}
    for row in reader.outbox_rows():
        # Rows are newest id first. Keep the newest status per signal.
        outbox_status.setdefault(row["signal_id"], row["status"])
    resolutions = {row["signal_id"]: row["outcome"] for row in _resolutions(reader)}
    shown = []
    for packet in packets:
        signal_id = packet.get("signal_id")
        review = reviews.get(signal_id)
        status = review_status_of(packet, review, now_ms, timeout_ms)
        failures = []
        for name, gate in (packet.get("hard_gates") or {}).items():
            if not isinstance(gate, dict) or gate.get("result") != "pass":
                failures.append({"name": name, "reason": None if not isinstance(gate, dict) else gate.get("reason")})
        targets = []
        for leg in (packet.get("targets") or [])[:8]:
            if isinstance(leg, dict):
                targets.append(
                    {
                        "symbol": leg.get("symbol"),
                        "side": leg.get("side"),
                        "notional": leg.get("notional"),
                        "risk_usdt": leg.get("risk_usdt"),
                    }
                )
        diffs = packet.get("diffs") if isinstance(packet.get("diffs"), dict) else {}
        diff_counts = {
            name: len(diffs.get(name) or [])
            for name in ("open", "close", "increase", "decrease", "flip", "unchanged")
        }
        fill = fills.get(signal_id)
        shown.append(
            {
                "signal_id": signal_id,
                "ts_ms": packet.get("ts_ms"),
                "ts_myt": None if packet.get("ts_ms") is None else format_myt(int(packet["ts_ms"])),
                "deadline_myt": None
                if packet.get("ts_ms") is None
                else format_myt(int(packet["ts_ms"]) + timeout_ms),
                "symbol": packet.get("symbol"),
                "setup": packet.get("setup"),
                "direction": packet.get("direction"),
                "book": packet.get("book"),
                "packet_kind": packet.get("packet_kind"),
                "variant": packet.get("variant"),
                "status": packet.get("status"),
                "decision_path": packet.get("decision_path"),
                "review_status": status,
                "hint": signal_hint(status, packet.get("packet_kind")),
                "requested": None if review is None else review.get("requested_decision"),
                "effective": None if review is None else review.get("effective_decision"),
                "reason": None if review is None else review.get("reason"),
                "reasoning": None if review is None else review.get("reasoning"),
                "override_refused": False if review is None else bool(review.get("override_refused")),
                "master_decision": (packet.get("master_decision") or {}).get("decision"),
                "risk_usdt": packet.get("risk_usdt"),
                "notional_usdt": packet.get("notional_usdt"),
                "targets": targets,
                "target_count": len(packet.get("targets") or []),
                "diffs": diff_counts,
                "gate_failures": failures,
                "fill": None
                if not fill
                else {
                    "price": fill.get("fill_price"),
                    "size": fill.get("size"),
                    "fee": fill.get("fee"),
                    "stop": fill.get("stop"),
                },
                "resolution": resolutions.get(signal_id),
                "outbox_status": outbox_status.get(signal_id) or "absent",
            }
        )
    counts: dict[str, int] = defaultdict(int)
    for row in shown:
        counts[row["review_status"]] += 1
    return {
        "rows": shown,
        "meta": {
            "returned": len(shown),
            "total": total,
            "counts": dict(counts),
            "truncated": total > len(shown),
        },
    }


def _resolutions(reader: Reader) -> list[dict]:
    return [dict(row) for row in reader._all("SELECT signal_id, outcome FROM paper_resolutions")]


def _outbox(reader: Reader, settings) -> dict:
    counts = reader.outbox_counts()
    rows = []
    for row in reader.outbox_rows():
        error = row.get("last_error")
        if isinstance(error, str) and len(error) > 240:
            error = error[:240]
        rows.append(
            {
                "id": row.get("id"),
                "signal_id": row.get("signal_id"),
                "status": row.get("status"),
                "attempts": row.get("attempts"),
                "next_attempt_ms": row.get("next_attempt_ms"),
                "last_error": error,
                "created_ms": row.get("created_ms"),
                "updated_ms": row.get("updated_ms"),
                "delivered_ms": row.get("delivered_ms"),
                "updated_myt": None if row.get("updated_ms") is None else format_myt(int(row["updated_ms"])),
            }
        )
    enabled = bool((settings.get("webhook") or {}).get("enabled"))
    return {"enabled": enabled, "counts": counts, "rows": rows, "plain": outbox_plain(enabled, counts)}


def _journal(reader: Reader) -> dict:
    total = reader.count_journal()
    rows = []
    events = set()
    books = set()
    symbols = set()
    for row in reader.journal_rows(JOURNAL_LIMIT):
        try:
            payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        except (TypeError, json.JSONDecodeError):
            payload = {}
        symbol, book = journal_labels(payload)
        event = str(row.get("event") or "")
        summary = journal_summary(event, payload)
        if len(summary) > 400:
            summary = summary[:400]
        item = {
            "id": row.get("id"),
            "ts_ms": row.get("ts_ms"),
            "ts_myt": None if row.get("ts_ms") is None else format_myt(int(row["ts_ms"])),
            "signal_id": row.get("signal_id"),
            "event": event,
            "symbol": symbol,
            "book": book,
            "summary": summary,
        }
        rows.append(item)
        if event:
            events.add(event)
        if book:
            books.add(book)
        if symbol:
            symbols.add(symbol)
    return {
        "rows": rows,
        "meta": {
            "returned": len(rows),
            "total": total,
            "truncated": total > len(rows),
            "events": sorted(events),
            "books": sorted(books),
            "symbols": sorted(symbols),
        },
    }


def _trades(reader: Reader) -> list[dict]:
    rows = []
    for trade in reader.list_paper_trades():
        exit_ms = trade.get("exit_time")
        rows.append(
            {
                "signal_id": trade.get("signal_id"),
                "symbol": trade.get("symbol"),
                "book": trade.get("book"),
                "setup": trade.get("setup"),
                "direction": trade.get("direction"),
                "pnl": trade.get("pnl"),
                "r_multiple": trade.get("r_multiple"),
                "fees": trade.get("fees"),
                "funding": trade.get("funding"),
                "exit_reason": trade.get("exit_reason"),
                "exit_price": trade.get("exit_price"),
                "exit_ms": exit_ms,
                "exit_myt": None if exit_ms is None else format_myt(int(exit_ms)),
                "entry_ms": trade.get("entry_time"),
            }
        )
    rows.sort(key=lambda item: int(item.get("exit_ms") or 0), reverse=True)
    return rows


def _scorecard(reader, settings, now_ms, report_path) -> tuple[dict, dict]:
    try:
        card = build_scorecard(reader, settings, now_ms)
    except Exception:
        card = None
    bands = ((settings.get("paper") or {}).get("drift") or {})
    elapsed = 0.0
    if isinstance(card, dict):
        elapsed = float((card.get("books") or {}).get("elapsed_days") or 0.0)
    report = _load_report(settings, report_path)
    books = {}
    if isinstance(card, dict):
        for spec in enabled_specs(settings):
            row = (card.get("books") or {}).get(spec["book"]) or {}
            drift = row.get("drift") or {}
            family = spec["family"]
            books[spec["book"]] = {
                "name": family,
                "variant": spec["variant"],
                "actual": {key: row.get(key) for key in (
                    "positions", "trades", "turnover", "turnover_per_year", "slippage",
                    "slippage_frac", "fees", "funding", "net_pnl", "return",
                )},
                "drift": drift,
                "plain": drift_plain(drift, elapsed, bands),
                "holdout": (report.get("books") or {}).get(family),
                "configured_expected": spec.get("expected") or {},
            }
    expectations = {
        "bands": {
            "min_days": bands.get("min_days"),
            "turnover_rel": bands.get("turnover_rel"),
            "slippage_rel": bands.get("slippage_rel"),
            "return_abs": bands.get("return_abs"),
        },
        "elapsed_days": elapsed,
        "report": report.get("name"),
        "report_note": report.get("note"),
        "books": books,
        "e6_note": report.get("e6_note"),
    }
    public = None if card is None else {
        "trades": card.get("trades"),
        "win_rate": card.get("win_rate"),
        "expectancy_r": card.get("expectancy_r"),
        "max_drawdown": card.get("max_drawdown"),
        "rule_violations": card.get("rule_violations"),
        "safety_blocks": card.get("safety_blocks"),
        "reviewer": card.get("reviewer"),
        "net_pnl": card.get("net_pnl"),
        "elapsed_days": elapsed,
    }
    return public, expectations


def _load_report(settings, report_path) -> dict:
    path = Path(report_path) if report_path else _find_report(settings)
    if path is None or not path.is_file():
        return {
            "name": None,
            "note": "No strategies report was found next to the config. The engine still compares the yaml hold-out rates.",
            "books": {},
            "e6_note": None,
        }
    try:
        parsed = parse_strategy_report(path.read_text(encoding="utf-8"))
    except OSError:
        parsed = {}
    e6 = parsed.get("E6")
    e6_on = bool(((settings.get("strategies") or {}).get("e6_liquidation_proxy") or {}).get("enabled"))
    note = None
    if e6 and not e6_on:
        trades = e6.get("trades")
        note = (
            f"The study's E6 hold-out was {trades} trades. "
            "The running config leaves E6 off. This page does not turn it on."
        )
    return {
        "name": path.name,
        "note": (
            "Hold-out figures are the 2025-01-01 through 2026-10-31 study in "
            f"{path.name}. They are the rates the books were switched on with, not a new backtest."
        ),
        "books": parsed,
        "e6_note": note,
    }


def _find_report(settings) -> Path | None:
    configured = (settings.get("data") or {}).get("sqlite_path")
    # Reports live in the repo, next to config/, not next to the runtime database.
    candidates = []
    here = Path(__file__).resolve()
    candidates.append(here.parents[3] / "reports")
    candidates.append(Path.cwd() / "reports")
    _ = configured
    seen = set()
    files = []
    for directory in candidates:
        if directory in seen or not directory.is_dir():
            continue
        seen.add(directory)
        files.extend(directory.glob("strategies-*.md"))
    if not files:
        return None
    return sorted(files)[-1]
