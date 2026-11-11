"""Dashboard data layer, bind rules, and the read-only HTTP server."""

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dashboard_fixture import BODY_MARKER, clock_start_ms, write_fixture
from desk.config import load_settings
from desk.dashboard.http import DashboardBindError, resolve_listen, serve
from desk.dashboard.read import build_snapshot, open_readonly
from desk.dashboard.refresh_status import RefreshObserver
from desk.dashboard.views import filter_journal, next_decision_windows, parse_strategy_report
from desk.data.paper_refresh import PaperRefresh
from desk.data.store import Store
from desk.timeutils import DAY_MS, HOUR_MS, last_closed_daily_open

ROOT = Path(__file__).resolve().parents[1]
NOW = clock_start_ms() + 6 * HOUR_MS


class _Context:
    def __init__(self, settings, db, secrets, token=None):
        self.settings = settings
        self.token = token
        self.secrets_env_path = secrets
        self.environ = {}
        self.now_ms = lambda: NOW

    # build_snapshot reads sqlite from settings when the server calls it.
    # Tests pass the fixture path on the settings dict.


def _settings(tmp_path, db):
    settings = load_settings(ROOT / "config" / "settings.yaml")
    settings["data"]["sqlite_path"] = str(db)
    settings["paper"]["kill_switch_path"] = str(tmp_path / "KILL-absent")
    return settings


def _snapshot(tmp_path, **kwargs):
    db = write_fixture(tmp_path / "desk", NOW)
    settings = _settings(tmp_path, db)
    return build_snapshot(
        settings,
        now_ms=NOW,
        sqlite_path=db,
        secrets_env_path=tmp_path / "no-such.env",
        service_state="active",
        probe_service=False,
        environ={},
        **kwargs,
    ), db, settings


def test_readonly_connection_refuses_writes_and_does_not_block_a_writer(tmp_path):
    db = write_fixture(tmp_path / "desk", NOW)
    before = Store(db)
    count = before.conn.execute("SELECT COUNT(*) AS n FROM decision_journal").fetchone()["n"]
    before.close()
    reader = open_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        reader.execute("INSERT INTO journal (ts_ms, kind, payload) VALUES (1, 'x', '{}')")
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA busy_timeout=2000")
    writer.execute("INSERT INTO journal (ts_ms, kind, payload) VALUES (1, 'probe', '{}')")
    writer.commit()
    writer.close()
    reader.close()
    check = Store(db)
    # The snapshot above used a different database. This file only gained the probe row.
    assert check.conn.execute("SELECT COUNT(*) AS n FROM decision_journal").fetchone()["n"] == count
    check.close()


def test_snapshot_reads_the_desk_without_secrets(tmp_path):
    snap, db, _ = _snapshot(tmp_path)
    assert "Day 1 of 14" in snap["clock"]["plain"]
    assert "13:13:38 MYT" in snap["clock"]["plain"]
    assert snap["health"]["gates"]["news"]["blocking"] is True
    assert "Entries refused" in snap["health"]["gates"]["news"]["plain"]
    assert snap["health"]["gates"]["kill"]["active"] is False
    assert snap["health"]["gates"]["stale_data"]["blocking"] is False
    assert snap["health"]["gates"]["equity"]["readable"] is True
    assert "news gate is closed" in snap["health"]["cycle"]["plain"]
    assert snap["health"]["service"]["headline"] == "Paper service is up."
    assert snap["health"]["orders"] is False
    assert snap["health"]["refresh"]["process"]["totals"]["http_429"] == 1
    assert snap["health"]["refresh"]["process"]["totals"]["vision_files"] == 48
    assert snap["health"]["refresh"]["weight_per_minute"] == 300
    assert snap["health"]["refresh"]["pause_above"] == 1200
    assert "E6" in snap["health"]["roster"]["plain"]
    assert "off" in snap["health"]["roster"]["plain"]
    windows = {row["book"]: row for row in snap["windows"]}
    assert windows["e2"]["state"] == "upcoming"
    assert windows["e3"]["state"] == "upcoming"
    assert "Mon 16 Nov 2026, 08:00–09:00 MYT" in windows["e2"]["plain"]
    assert "Thu 12 Nov 2026, 08:00–09:00 MYT" in windows["e3"]["plain"]
    assert snap["books"]["equity"] == pytest.approx(807.2)
    btc = next(row for row in snap["books"]["positions"] if row["symbol"] == "BTCUSDT")
    assert btc["side"] == "long"
    assert btc["size"] == pytest.approx(0.004)
    assert btc["entry"] == pytest.approx(94800)
    assert btc["stop"] == pytest.approx(93000)
    assert btc["unrealized_pnl"] == pytest.approx(2.8)
    assert btc["r"] == pytest.approx(2.8 / 7.2)
    assert snap["books"]["open_risk"]["fraction_of_cap"] == pytest.approx(29.2 / (0.06 * 807.2))
    assert snap["books"]["gross_leverage"] == pytest.approx(971 / 807.2, rel=1e-3)
    clusters = {row["cluster"]: row for row in snap["books"]["clusters"]}
    assert clusters["alts"]["risk_usdt"] == pytest.approx(12)
    assert clusters["btc"]["cap_pct"] == pytest.approx(0.03)
    assert snap["books"]["btc"]["return"] == pytest.approx(96800 / 95100 - 1)
    assert snap["books"]["vs_btc"]["difference"] < 0
    statuses = {row["signal_id"]: row["review_status"] for row in snap["signals"]}
    assert statuses == {
        "e2-rebalance-pending": "pending",
        "e3-sol-open": "open",
        "e3-bnb-skip": "skip",
        "e3-old-timeout": "timeout",
        "e2-rejected-news": "rejected",
    }
    opened = next(row for row in snap["signals"] if row["signal_id"] == "e3-sol-open")
    assert opened["reasoning"] == "daily close held above the 50-day"
    assert opened["fill"]["price"] == pytest.approx(180.2)
    assert opened["outbox_status"] == "pending"
    assert "Silence keeps the current book" in next(row["hint"] for row in snap["signals"] if row["review_status"] == "pending")
    blob = json.dumps(snap)
    assert BODY_MARKER not in blob
    assert snap["scorecard"]["trades"] == 1
    assert snap["scorecard"]["expectancy_r"] == pytest.approx(0.45)
    assert snap["scorecard"]["reviewer"]["timeout_skip"] == 1
    assert snap["expectations"]["books"]["e2"]["holdout"]["cagr"] == pytest.approx(0.143)
    assert "11 trades" in snap["expectations"]["e6_note"]
    assert "not an alert yet" in snap["expectations"]["books"]["e2"]["plain"]
    # The read did not add a journal row.
    store = Store(db)
    assert store.conn.execute("SELECT COUNT(*) AS n FROM decision_journal").fetchone()["n"] == 5
    store.close()


def test_secret_values_are_scrubbed_and_kill_file_is_reported(tmp_path):
    db = write_fixture(tmp_path / "desk", NOW)
    secret = "whsec_should_not_appear"
    url = "https://hooks.example/super-secret-path"
    store = Store(db)
    store.append_decision_journal(NOW, "leak-check", "decision", {"reasoning": f"see {secret}", "effective": "skip", "book": "e2", "symbol": "BTCUSDT"})
    store.close()
    kill = tmp_path / "KILL"
    kill.write_text("halt\n", encoding="utf-8")
    env = tmp_path / "secrets.env"
    env.write_text(f"WEBHOOK_SECRET={secret}\nWEBHOOK_URL={url}\nKILL_SWITCH_PATH={kill}\n", encoding="utf-8")
    settings = _settings(tmp_path, db)
    settings["webhook"]["url"] = url
    settings["account"]["api_key"] = "AKIA_SHOULD_NOT_APPEAR"
    snap = build_snapshot(
        settings,
        now_ms=NOW,
        sqlite_path=db,
        secrets_env_path=env,
        service_state="active",
        probe_service=False,
        environ={},
    )
    blob = json.dumps(snap)
    assert secret not in blob
    assert url not in blob
    assert "AKIA_SHOULD_NOT_APPEAR" not in blob
    assert "[redacted]" in blob
    assert snap["health"]["gates"]["kill"]["active"] is True
    assert "halt" in snap["health"]["gates"]["kill"]["plain"]


def test_stale_daily_bar_blocks_entries(tmp_path):
    db = tmp_path / "desk.sqlite"
    store = Store(db)
    now = NOW
    expected = last_closed_daily_open(now) + DAY_MS
    old_open = expected - 3 * DAY_MS
    store.upsert_symbols(
        [{
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "contract_type": "PERPETUAL",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "margin_asset": "USDT",
            "onboard_date_ms": now - 100 * DAY_MS,
        }],
        now,
    )
    store.upsert_klines(
        "BTCUSDT",
        "1d",
        [{
            "open_time": old_open,
            "close_time": old_open + DAY_MS - 1,
            "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "quote_volume": 1,
        }],
    )
    store.close()
    settings = _settings(tmp_path, db)
    snap = build_snapshot(
        settings,
        now_ms=now,
        sqlite_path=db,
        secrets_env_path=tmp_path / "missing.env",
        service_state="inactive",
        probe_service=False,
        environ={},
    )
    assert snap["health"]["gates"]["stale_data"]["blocking"] is True
    assert "stale" in snap["health"]["gates"]["stale_data"]["plain"].lower()


def test_decision_windows_and_report_and_journal_filter():
    during = int(datetime(2026, 11, 16, 0, 30, tzinfo=timezone.utc).timestamp() * 1000)
    windows = {row["book"]: row for row in next_decision_windows(during, 3600)}
    assert windows["e2"]["state"] == "open"
    assert windows["e3"]["state"] == "open"
    assert "09:00" in windows["e2"]["deadline_label"]
    parsed = parse_strategy_report((ROOT / "reports" / "strategies-20261110.md").read_text(encoding="utf-8"))
    assert parsed["E2"]["variant"] == "e2_r15_b60_v30_top25"
    assert parsed["E2"]["cagr"] == pytest.approx(0.143)
    assert parsed["E2"]["expectancy_r"] == pytest.approx(0.06)
    assert parsed["E3"]["sharpe"] == pytest.approx(0.97)
    assert parsed["E3"]["max_dd"] == pytest.approx(0.123)
    assert parsed["E6"]["trades"] == 11
    rows = [
        {"book": "e3", "symbol": "SOLUSDT", "event": "fill", "summary": "fill 180", "signal_id": "a"},
        {"book": "e2", "symbol": "BTCUSDT", "event": "signal", "summary": "btc", "signal_id": "b"},
    ]
    assert len(filter_journal(rows, event="fill")) == 1
    assert filter_journal(rows, symbol="btcusdt")[0]["book"] == "e2"


def test_refresh_observer_counts_a_429_once_per_pause(tmp_path):
    path = tmp_path / "refresh_status.json"
    observer = RefreshObserver(path)
    observer.observe({"vision_files": 2, "fapi_requests": 1, "paused": True, "pause_reason": "http_429"})
    observer.observe({"vision_files": 1, "paused": True, "pause_reason": "http_429"})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["totals"]["http_429"] == 1
    assert payload["totals"]["vision_files"] == 3
    assert payload["totals"]["pauses"] == 1
    observer.observe({"paused": False, "vision_files": 0})
    observer.observe({"paused": True, "pause_reason": "http_418", "fapi_requests": 1})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["totals"]["http_429"] == 1
    assert payload["totals"]["http_418"] == 1
    assert payload["totals"]["pauses"] == 2
    assert "secret" not in path.read_text(encoding="utf-8").lower()


def test_refresh_hook_does_not_change_an_empty_slice():
    seen = []
    refresher = PaperRefresh({"data": {}}, on_summary=seen.append)
    summary = refresher.run_slice(None, [], 0)
    assert summary["http"] == 0
    assert summary["vision_files"] == 0
    assert len(seen) == 1

    def boom(_summary):
        raise RuntimeError("observer failed")

    noisy = PaperRefresh({"data": {}}, on_summary=boom)
    assert noisy.run_slice(None, [], 0)["paused"] is False


def test_listen_stays_on_loopback_unless_a_token_is_set():
    host, port, token = resolve_listen("127.0.0.1:8788", allow_remote=False, token=None)
    assert (host, port, token) == ("127.0.0.1", 8788, None)
    with pytest.raises(DashboardBindError):
        resolve_listen("0.0.0.0:8788", allow_remote=False, token=None)
    with pytest.raises(DashboardBindError):
        resolve_listen("0.0.0.0:8788", allow_remote=True, token=None)
    with pytest.raises(DashboardBindError):
        resolve_listen("127.0.0.1:8788", allow_remote=False, token="short")
    host, port, token = resolve_listen("[::1]:8788", allow_remote=False, token=None)
    assert host == "::1"
    host, _, token = resolve_listen("0.0.0.0:8788", allow_remote=True, token="0123456789abcdef")
    assert host == "0.0.0.0"
    assert token == "0123456789abcdef"


def test_http_server_is_get_only(tmp_path):
    db = write_fixture(tmp_path / "desk", NOW)
    settings = _settings(tmp_path, db)
    context = _Context(settings, db, tmp_path / "missing.env", token="0123456789abcdef")
    server = serve("127.0.0.1", 0, context)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        page = urllib.request.urlopen(base + "/", timeout=5)
        html = page.read().decode("utf-8")
        assert "viewport" in html
        assert "Pause page refresh" in html
        unauth = urllib.request.Request(base + "/api/snapshot")
        with pytest.raises(urllib.error.HTTPError) as blocked:
            urllib.request.urlopen(unauth, timeout=5)
        assert blocked.value.code == 401
        request = urllib.request.Request(base + "/api/snapshot", headers={"Authorization": "Bearer 0123456789abcdef"})
        payload = json.loads(urllib.request.urlopen(request, timeout=5).read().decode("utf-8"))
        assert payload["orders"] is False
        assert BODY_MARKER not in json.dumps(payload)
        journal = urllib.request.Request(
            base + "/api/journal?event=decision",
            headers={"Authorization": "Bearer 0123456789abcdef"},
        )
        rows = json.loads(urllib.request.urlopen(journal, timeout=5).read().decode("utf-8"))["journal"]
        assert rows and all(row["event"] == "decision" for row in rows)
        post = urllib.request.Request(base + "/api/snapshot", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(post, timeout=5)
        assert denied.value.code == 405
        health = json.loads(
            urllib.request.urlopen(
                urllib.request.Request(base + "/health", headers={"Authorization": "Bearer 0123456789abcdef"}),
                timeout=5,
            ).read().decode("utf-8")
        )
        assert health == {"ok": True, "read_only": True, "orders": False}
    finally:
        server.shutdown()
        server.server_close()


def test_static_page_and_unit_file_stay_read_only():
    app = (ROOT / "src" / "desk" / "dashboard" / "static" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "src" / "desk" / "dashboard" / "static" / "index.html").read_text(encoding="utf-8")
    assert "POST" not in app
    assert "fetch(" in app
    assert "15000" in app
    assert 'name="viewport"' in html
    unit = (ROOT / "deploy" / "chart-analyst-dashboard.service").read_text(encoding="utf-8")
    assert "User=chartanalyst" in unit
    assert "Nice=10" in unit
    assert "MemoryMax=512M" in unit
    assert "127.0.0.1:8788" in unit
    assert "EnvironmentFile" not in unit
    assert "secrets.env" not in unit
    docs = (ROOT / "docs" / "dashboard.md").read_text(encoding="utf-8")
    assert "ssh -L 8788:127.0.0.1:8788" in docs
    assert "--allow-remote" in docs
