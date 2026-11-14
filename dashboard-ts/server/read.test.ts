import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import Database from "better-sqlite3";
import { readDesk } from "./read.js";

const START = Date.UTC(2026, 10, 11, 5, 13, 38);
const NOW = START + 6 * 3_600_000;

function seed(dir: string): string {
  const dbPath = path.join(dir, "desk.sqlite");
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE symbols (symbol TEXT PRIMARY KEY, status TEXT, contract_type TEXT, base_asset TEXT, quote_asset TEXT, margin_asset TEXT, onboard_date_ms INTEGER, updated_at_ms INTEGER);
    CREATE TABLE klines (symbol TEXT, interval TEXT, open_time INTEGER, close_time INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL, quote_volume REAL, PRIMARY KEY (symbol, interval, open_time));
    CREATE TABLE signals (signal_id TEXT PRIMARY KEY, dedupe_key TEXT, symbol TEXT, setup TEXT, direction TEXT, ts_ms INTEGER, status TEXT, payload TEXT);
    CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT, ts_ms INTEGER, market_context TEXT, requested_decision TEXT, effective_decision TEXT, reasoning TEXT, decision_ts TEXT, override_refused INTEGER, reason TEXT, payload TEXT);
    CREATE TABLE webhook_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT, idempotency_key TEXT, body TEXT, status TEXT, attempts INTEGER, next_attempt_ms INTEGER, last_error TEXT, created_ms INTEGER, updated_ms INTEGER, delivered_ms INTEGER);
    CREATE TABLE decision_journal (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, signal_id TEXT, event TEXT, payload TEXT);
    CREATE TABLE paper_account (id INTEGER PRIMARY KEY, initial_equity REAL, equity_usdt REAL, day_start_equity REAL, week_start_equity REAL, day_key TEXT, week_key TEXT, as_of_ms INTEGER, started_at_ms INTEGER, last_block TEXT, updated_ms INTEGER);
    CREATE TABLE paper_book_positions (book TEXT, symbol TEXT, payload TEXT, updated_ms INTEGER, PRIMARY KEY (book, symbol));
    CREATE TABLE paper_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT, symbol TEXT, exit_ms INTEGER, payload TEXT);
    CREATE TABLE paper_equity (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, equity REAL);
    CREATE TABLE vision_days (symbol TEXT, dataset TEXT, day TEXT, rows INTEGER, PRIMARY KEY (symbol, dataset, day));
  `);
  const dailyOpen = Math.floor(NOW / 86_400_000) * 86_400_000 - 86_400_000;
  const dailyClose = dailyOpen + 86_400_000 - 1;
  const bar = (symbol: string, close: number) => {
    db.prepare("INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?,?)").run(symbol, "1d", dailyOpen, dailyClose, close, close, close, close, 1, 1);
    db.prepare("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?)").run(symbol, "TRADING", "PERPETUAL", symbol.replace("USDT", ""), "USDT", "USDT", START, NOW);
  };
  bar("BTCUSDT", 95500);
  bar("ETHUSDT", 3460);
  bar("SOLUSDT", 176);
  bar("BNBUSDT", 608);
  db.prepare("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?)").run("OLDUSDT", "DELISTED", "PERPETUAL", "OLD", "USDT", "USDT", START, NOW);
  db.prepare("INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?,?)").run("OLDUSDT", "1d", dailyOpen - 3 * 86_400_000, dailyOpen - 2 * 86_400_000 - 1, 1, 1, 1, 1, 1, 1);
  const hour = (START - 3_600_000) - ((START - 3_600_000) % 3_600_000);
  const prices = [95100, 95500, 95800, 96100, 96400, 96600, 96800];
  let cursor = hour;
  for (const px of prices) {
    const closeTime = cursor + 3_600_000 - 1;
    if (closeTime <= NOW) db.prepare("INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?,?)").run("BTCUSDT", "1h", cursor, closeTime, px, px, px, px, 1, 1);
    cursor += 3_600_000;
  }
  const pos = (book: string, symbol: string, side: number, qty: number, entry: number, stop: number, mark: number, risk: number, cluster: string) => {
    const payload = {
      book, symbol, side, qty, entry_price: entry, stop_price: stop, last_mark: mark,
      cash_impact: -(qty * entry), risk_usdt: risk, initial_risk: risk, cluster,
    };
    db.prepare("INSERT INTO paper_book_positions VALUES (?,?,?,?)").run(book, symbol, JSON.stringify(payload), NOW);
  };
  pos("e2", "BTCUSDT", 1, 0.004, 94800, 93000, 95500, 7.2, "btc");
  pos("e2", "ETHUSDT", -1, -0.05, 3500, 3620, 3460, 6, "eth");
  pos("e3", "SOLUSDT", 1, 1.5, 180, 172, 176, 12, "alts");
  pos("e3", "BNBUSDT", 1, 0.25, 600, 584, 608, 4, "l1");
  db.prepare("INSERT INTO paper_trades (signal_id, symbol, exit_ms, payload) VALUES (?,?,?,?)").run(
    "e3-avax-exit", "AVAXUSDT", START + 7_200_000,
    JSON.stringify({ signal_id: "e3-avax-exit", symbol: "AVAXUSDT", book: "e3", direction: "long", pnl: 6.4, r_multiple: 0.45, exit_reason: "trail", exit_time: START + 7_200_000, exit_price: 36.4 }),
  );
  db.prepare("INSERT INTO paper_equity (ts_ms, equity) VALUES (?,?)").run(START, 800);
  db.prepare("INSERT INTO paper_equity (ts_ms, equity) VALUES (?,?)").run(NOW, 807.2);
  db.prepare("INSERT INTO paper_account VALUES (1,?,?,?,?,?,?,?,?,?,?)").run(800, 807.2, 800, 800, "2026-11-11", "w", NOW, START, null, NOW - 20_000);
  const packet = (id: string, extra: Record<string, unknown>) => {
    const body = { signal_id: id, symbol: "BTCUSDT", direction: "long", ts_ms: NOW - 60_000, status: "proposed", ...extra };
    db.prepare("INSERT INTO signals VALUES (?,?,?,?,?,?,?,?)").run(id, id, body.symbol, "e2", body.direction, body.ts_ms, body.status, JSON.stringify(body));
  };
  packet("e2-rebalance-pending", { book: "e2", packet_kind: "rebalance", ts_ms: NOW - 15 * 60_000, symbol: "BTCUSDT" });
  packet("e3-sol-open", { book: "e3", packet_kind: "book_entry", symbol: "SOLUSDT", ts_ms: NOW - 30 * 60_000 });
  packet("e3-bnb-skip", { book: "e3", packet_kind: "book_entry", symbol: "BNBUSDT", ts_ms: NOW - 40 * 60_000, status: "proposed" });
  packet("e3-old-timeout", { book: "e3", packet_kind: "book_entry", symbol: "ADAUSDT", ts_ms: NOW - 2 * 3_600_000, status: "proposed" });
  db.prepare("INSERT INTO reviews (signal_id, ts_ms, market_context, requested_decision, effective_decision, reasoning, decision_ts, override_refused, reason, payload) VALUES (?,?,?,?,?,?,?,?,?,?)")
    .run("e3-bnb-skip", NOW, "{}", "skip", "skip", "cluster already holds the risk I want", "", 0, "reviewer_skip", "{}");
  db.prepare("INSERT INTO reviews (signal_id, ts_ms, market_context, requested_decision, effective_decision, reasoning, decision_ts, override_refused, reason, payload) VALUES (?,?,?,?,?,?,?,?,?,?)")
    .run("e3-old-timeout", NOW, "{}", "timeout", "skip", "decision timeout; fail closed", "", 0, "decision_timeout", "{}");
  db.prepare("INSERT INTO reviews (signal_id, ts_ms, market_context, requested_decision, effective_decision, reasoning, decision_ts, override_refused, reason, payload) VALUES (?,?,?,?,?,?,?,?,?,?)")
    .run("e3-sol-open", NOW, "{}", "open", "open", "daily close held above the 50-day", "", 0, "reviewer_open", "{}");
  db.prepare("INSERT INTO decision_journal (ts_ms, signal_id, event, payload) VALUES (?,?,?,?)")
    .run(NOW, "e3-sol-open", "decision", JSON.stringify({ effective: "open", reasoning: "daily close held above the 50-day", book: "e3", symbol: "SOLUSDT" }));
  db.prepare("INSERT INTO vision_days VALUES (?,?,?,?)").run("BTCUSDT", "1d", "2026-11-10", 1);
  db.close();
  return dbPath;
}

test("snapshot matches the paper ledger and refuses to echo a secret", () => {
  const dir = mkdtempSync(path.join(tmpdir(), "desk-ts-"));
  const sqlitePath = seed(dir);
  const settingsPath = path.join(dir, "settings.yaml");
  writeFileSync(settingsPath, `
mode: paper
account: { news_status: null, webhook_url: "https://hooks.example/super-secret-path" }
paper: { initial_equity: 800, clock_days: 14, max_data_age_seconds: 5400, loop_seconds: 60, kill_switch_path: ${JSON.stringify(path.join(dir, "NO-KILL"))} }
risk: { max_open_risk_pct: 0.06, max_cluster_risk_pct: 0.03 }
decision: { timeout_seconds: 3600 }
data: { request_weight_per_minute: 300, used_weight_pause: 1200 }
strategies:
  e2_residual_momentum: { enabled: true, variant: e2_r15_b60_v30_top25 }
  e3_slow_trend: { enabled: true, variant: e3a_sma50_trail3_top40 }
  e6_liquidation_proxy: { enabled: false }
setups:
  squeeze_breakout: { enabled: false }
`);
  writeFileSync(path.join(dir, "refresh_status.json"), JSON.stringify({
    updated_ms: NOW - 4000, paused: false, pause_reason: null,
    totals: { vision_files: 48, fapi_requests: 6, pauses: 2, http_429: 1, http_418: 0 },
  }));
  const snap = readDesk({
    sqlitePath, settingsPath, refreshPath: path.join(dir, "refresh_status.json"),
    killPath: path.join(dir, "NO-KILL"), now: NOW, review: { ok: true, status: 200 },
  });
  assert.equal(snap.orders, false);
  assert.equal(snap.readOnly, true);
  assert.equal(snap.health.newsBlocking, true);
  assert.match(snap.health.newsPlain, /Entries refused/);
  assert.equal(snap.health.killOn, false);
  assert.equal(snap.health.staleBlocking, false);
  assert.equal(snap.clock.day, 1);
  assert.match(snap.clock.plain, /13:13:38 MYT/);
  assert.equal(snap.windows.find((w) => w.book === "e2")?.state, "upcoming");
  assert.match(snap.windows.find((w) => w.book === "e2")?.plain ?? "", /Mon 16 Nov 2026, 08:00–09:00 MYT/);
  assert.match(snap.windows.find((w) => w.book === "e3")?.plain ?? "", /Thu 12 Nov 2026/);
  assert.ok(Math.abs((snap.equity ?? 0) - 807.2) < 1e-6);
  const btc = snap.positions.find((p) => p.symbol === "BTCUSDT");
  assert.equal(btc?.side, "long");
  assert.ok(Math.abs((btc?.unrealized ?? 0) - 2.8) < 1e-9);
  assert.ok(Math.abs((btc?.r ?? 0) - 2.8 / 7.2) < 1e-9);
  assert.ok(Math.abs((snap.openRisk.fraction ?? 0) - 29.2 / (0.06 * 807.2)) < 1e-9);
  assert.equal(snap.health.refresh.http429, 1);
  assert.equal(snap.health.weightPerMinute, 300);
  assert.equal(snap.signals.find((s) => s.id === "e2-rebalance-pending")?.status, "pending");
  assert.equal(snap.signals.find((s) => s.id === "e3-sol-open")?.status, "open");
  assert.equal(snap.signals.find((s) => s.id === "e3-sol-open")?.reasoning, "daily close held above the 50-day");
  assert.equal(snap.scorecard.trades, 1);
  assert.equal(snap.scorecard.expectancy, 0.45);
  const blob = JSON.stringify(snap);
  assert.equal(blob.includes("super-secret-path"), false);
  const ro = new Database(sqlitePath, { readonly: true, fileMustExist: true });
  assert.throws(() => ro.prepare("INSERT INTO paper_equity (ts_ms, equity) VALUES (1, 1)").run());
  ro.close();
});

test("snapshot klines queries use the primary key and freshness is cached", () => {
  const dir = mkdtempSync(path.join(tmpdir(), "desk-ts-plan-"));
  const sqlitePath = seed(dir);
  const settingsPath = path.join(dir, "settings.yaml");
  writeFileSync(settingsPath, `
mode: paper
paper: { initial_equity: 800, clock_days: 14, max_data_age_seconds: 5400, loop_seconds: 60, kill_switch_path: ${JSON.stringify(path.join(dir, "NO-KILL"))} }
strategies:
  e2_residual_momentum: { enabled: true }
  e3_slow_trend: { enabled: true }
setups:
  squeeze_breakout: { enabled: false }
`);
  const dailyOpen = Math.floor(NOW / 86_400_000) * 86_400_000 - 86_400_000;
  const dailyClose = dailyOpen + 86_400_000 - 1;
  const db = new Database(sqlitePath);
  db.prepare("UPDATE klines SET close_time = close_time - ? WHERE interval = '1d'").run(3 * 86_400_000);
  db.prepare("INSERT INTO klines VALUES (?,?,?,?,?,?,?,?,?,?)").run("ZZZUSDT", "1d", dailyOpen, dailyClose, 1, 1, 1, 1, 1, 1);
  db.prepare("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?)").run("ZZZUSDT", "TRADING", "PERPETUAL", "ZZZ", "USDT", "USDT", START, NOW);
  db.close();
  const input = {
    sqlitePath,
    settingsPath,
    refreshPath: path.join(dir, "missing-refresh.json"),
    killPath: path.join(dir, "NO-KILL"),
    now: NOW,
    review: { ok: true, status: 200 },
  };
  const first: { sql: string; params: unknown[] }[] = [];
  const snap = readDesk({ ...input, trace: (sql, params) => first.push({ sql, params }) });
  assert.equal(snap.health.freshness.find((row) => row.interval === "1d")?.fresh, false);
  assert.equal(snap.health.freshness.find((row) => row.interval === "4h")?.fresh, false);
  assert.equal(snap.health.staleBlocking, true);
  assert.match(snap.health.stalePlain, /BTCUSDT/);
  const explain = new Database(sqlitePath, { readonly: true, fileMustExist: true });
  const klines = first.filter((query) => query.sql.includes("klines"));
  assert.ok(klines.length > 0);
  for (const query of klines) {
    assert.equal(/COUNT\s*\(\s*DISTINCT/i.test(query.sql), false, query.sql);
    assert.match(query.sql, /symbol = \?/);
    const plan = explain.prepare(`EXPLAIN QUERY PLAN ${query.sql}`).all(...query.params) as { detail: string }[];
    for (const row of plan) {
      assert.equal(/SCAN\s+klines/i.test(row.detail), false, `${query.sql} -> ${row.detail}`);
      assert.match(row.detail, /sqlite_autoindex_klines_1/);
    }
  }
  explain.close();
  const second: unknown[][] = [];
  readDesk({ ...input, trace: (sql, params) => { if (sql.includes("klines")) second.push(params); } });
  assert.equal(second.some((params) => params.includes("4h")), false);
});
