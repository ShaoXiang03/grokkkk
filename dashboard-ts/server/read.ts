/**
 * Read-only snapshot of the paper desk.
 *
 * better-sqlite3 is opened with readonly and fileMustExist, then query_only.
 * The queries follow scripts/serve_dashboard.py (desk.dashboard.read): daily
 * marks, ledger equity, review status, the MYT clock, and the E2/E3 windows.
 * Nothing here writes, and settings values that look like secrets are not copied out.
 */

import { closeSync, existsSync, openSync, readFileSync, readSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import Database from "better-sqlite3";
import { parse as parseYaml } from "yaml";
import type { BookCard, CurvePoint, DeskSnapshot, FeedEvent, PositionRow, SignalRow, TradeRow, WindowRow } from "../shared/types.js";

const DAY = 86_400_000;
const HOUR = 3_600_000;
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export interface ReadInput {
  sqlitePath: string;
  settingsPath: string;
  journalPath?: string;
  refreshPath?: string;
  killPath?: string;
  now?: number;
  review?: { ok: boolean; status: number | null } | null;
  /** Test hook. Records each statement the snapshot actually runs. */
  trace?: (sql: string, params: unknown[]) => void;
}

type Row = Record<string, unknown>;

export function readDesk(input: ReadInput): DeskSnapshot {
  const now = input.now ?? Date.now();
  const settings = loadSettings(input.settingsPath);
  const denylist = secretValues(settings);
  if (!existsSync(input.sqlitePath)) {
    return scrub(emptySnapshot(now, settings, "The sqlite database is not there. This process does not create it.", input), denylist);
  }
  const previous = traceQuery;
  traceQuery = input.trace ?? null;
  const db = new Database(input.sqlitePath, { readonly: true, fileMustExist: true, timeout: 2000 });
  try {
    try { db.pragma("query_only = ON"); } catch { /* readonly already refuses writes */ }
    return scrub(assemble(db, settings, input, now), denylist);
  } finally {
    db.close();
    traceQuery = previous;
  }
}

const settingsCache = new Map<string, { mtimeMs: number; size: number; value: Record<string, unknown> }>();

function loadSettings(file: string): Record<string, unknown> {
  if (!file || !existsSync(file)) return {};
  const st = statSync(file);
  const hit = settingsCache.get(file);
  if (hit && hit.mtimeMs === st.mtimeMs && hit.size === st.size) return hit.value;
  const parsed = parseYaml(readFileSync(file, "utf8"));
  const value = parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : {};
  settingsCache.set(file, { mtimeMs: st.mtimeMs, size: st.size, value });
  return value;
}

function secretValues(settings: Record<string, unknown>): string[] {
  const found: string[] = [];
  const walk = (node: unknown, key = ""): void => {
    if (typeof node === "string" && node.length >= 8 && /secret|token|password|api_key|webhook/i.test(key)) found.push(node);
    else if (node && typeof node === "object") {
      for (const [k, v] of Object.entries(node as Record<string, unknown>)) walk(v, k);
    }
  };
  walk(settings);
  return found;
}

function scrub<T>(value: T, denylist: string[]): T {
  const raw = JSON.stringify(value);
  let out = raw;
  for (const secret of denylist) out = out.split(secret).join("[redacted]");
  return JSON.parse(out) as T;
}

function assemble(db: Database.Database, settings: Record<string, unknown>, input: ReadInput, now: number): DeskSnapshot {
  const paper = dict(settings.paper);
  const risk = dict(settings.risk);
  const account = one(db, "SELECT * FROM paper_account WHERE id = 1");
  const initial = num(account?.initial_equity) ?? num(paper.initial_equity) ?? 800;
  const positions = loadPositions(db, now);
  const trades = loadTrades(db);
  const realized = trades.reduce((s, t) => s + (t.pnl ?? 0), 0);
  const openPnl = positions.every((p) => p.unrealized !== null)
    ? positions.reduce((s, p) => s + ledger(db, p, now), 0)
    : null;
  const marked = openPnl === null ? null : initial + realized + openPnl;
  const equity = marked !== null && marked > 0 ? marked : num(account?.equity_usdt);
  const base = equity && equity > 0 ? equity : initial;
  const curve = downsample(all(db, "SELECT ts_ms AS t, equity FROM paper_equity ORDER BY id").map((r) => ({ t: Number(r.t), equity: Number(r.equity) })));
  const started = account?.started_at_ms == null ? null : Number(account.started_at_ms);
  const days = Number(paper.clock_days ?? 14);
  const clock = clockView(started, now, days);
  const timeout = Number(dict(settings.decision).timeout_seconds ?? 3600) * 1000;
  const news = newsView(dict(settings.account).news_status);
  const killPath = input.killPath ?? resolveKill(settings, input.settingsPath);
  const kill = killView(killPath);
  const market = loadMarket(db, settings, now);
  const updated = account?.updated_ms == null ? null : Number(account.updated_ms);
  const loopS = Number(paper.loop_seconds ?? 60);
  const recent = updated !== null && Math.abs(now - updated) <= Math.max(loopS * 3, 180) * 1000;
  const lastBlock = account?.last_block == null ? null : String(account.last_block);
  const refresh = readRefresh(input.refreshPath, now);
  const weight = clampWeight(Number(dict(settings.data).request_weight_per_minute ?? 300));
  const pauseAbove = Number(dict(settings.data).used_weight_pause ?? 1200);
  const btc = btcCurve(db, started, initial, now);
  const books = bookCards(db, settings, positions, trades, initial, base, now, started);
  const signals = loadSignals(db, now, timeout);
  const journal = readJournalTail(input.journalPath);
  const events = loadEvents(db, journal.recent);
  const dd = maxDrawdown(curve.map((p) => p.equity));
  const openRiskUsdt = positions.reduce((s, p) => s + p.risk, 0);
  const capPct = Number(risk.max_open_risk_pct ?? 0.06);
  const clusterPct = Number(risk.max_cluster_risk_pct ?? 0.03);
  const cap = base ? capPct * base : null;
  const clusters = clusterRows(positions, base ? clusterPct * base : null);
  const notionals = positions.reduce((s, p) => s + (p.notional ?? 0), 0);
  const review = input.review;
  const counts: Record<string, number> = {};
  for (const row of signals) counts[row.status] = (counts[row.status] ?? 0) + 1;
  return {
    now,
    readOnly: true,
    orders: false,
    mode: String(settings.mode ?? "paper"),
    error: null,
    linkLabel: recent ? "DESK LIVE" : "CYCLE LATE",
    clock,
    windows: windows(now, timeout),
    health: {
      newsPlain: news.plain,
      newsBlocking: news.blocking,
      killPlain: kill.plain,
      killOn: kill.on,
      stalePlain: market.stale.plain,
      staleBlocking: market.stale.blocking,
      ordersPlain: "Orders are off. There is no order client. This page cannot place, cancel, or approve anything.",
      equityPlain: equity ? `Paper equity ${equity.toFixed(2)} USDT from the local ledger. Binance equity is not read.` : "Paper equity could not be marked.",
      cyclePlain: cyclePlain(lastBlock, news.blocking, recent),
      cycleRecent: recent,
      roster: roster(settings),
      freshness: market.freshness,
      refreshPlain: refreshPlain(refresh, weight, pauseAbove),
      weightPerMinute: weight,
      pauseAbove,
      refresh: refresh.totals,
      archiveDays: archiveDays(db),
      reviewPlain: review == null
        ? "The review API was not checked."
        : review.ok
          ? "Review API on 127.0.0.1:8787 answered /health."
          : "Review API on 127.0.0.1:8787 did not answer /health. This page does not submit reviews.",
      reviewOk: Boolean(review?.ok),
      journalLines: journal.lines,
    },
    equity,
    initialEquity: initial,
    openPnl,
    deskReturn: equity == null || !initial ? null : equity / initial - 1,
    drawdown: dd,
    grossLeverage: base ? notionals / base : null,
    openRisk: { usdt: openRiskUsdt, cap, fraction: cap ? openRiskUsdt / cap : null, capPct },
    clusters,
    deskCurve: curve,
    btcCurve: btc.curve,
    btcReturn: btc.ret,
    books,
    positions,
    signals,
    signalCounts: counts,
    trades,
    events,
    scorecard: scorecard(db, settings, trades, curve, started, now, input.settingsPath),
  };
}

function ledger(db: Database.Database, pos: PositionRow, now: number): number {
  const row = one(db, "SELECT payload FROM paper_book_positions WHERE book = ? AND symbol = ?", pos.book, pos.symbol);
  if (!row) return pos.unrealized ?? 0;
  const payload = json(row.payload);
  const qty = Number(payload.qty ?? 0);
  const mark = pos.mark ?? Number(payload.last_mark ?? 0);
  return Number(payload.cash_impact ?? 0) + qty * mark;
}

function loadPositions(db: Database.Database, now: number): PositionRow[] {
  const rows = all(db, "SELECT book, symbol, payload FROM paper_book_positions ORDER BY book, symbol");
  return rows.map((row) => {
    const p = json(row.payload);
    const qty = Number(p.qty ?? 0);
    const entry = Number(p.entry_price ?? 0);
    const mark = latestClose(db, String(row.symbol), "1d", now) ?? (p.last_mark == null ? null : Number(p.last_mark));
    const unrealized = mark == null ? null : qty * (mark - entry);
    const risk = Number(p.risk_usdt ?? 0);
    const initial = Number(p.initial_risk ?? 0) || risk;
    return {
      book: String(row.book),
      symbol: String(row.symbol),
      side: Number(p.side ?? 1) > 0 ? "long" : "short",
      size: Math.abs(qty),
      entry,
      stop: Number(p.stop_price ?? 0),
      mark,
      r: unrealized == null || initial <= 0 ? null : unrealized / initial,
      unrealized,
      risk,
      notional: mark == null ? null : Math.abs(qty) * mark,
      cluster: String(p.cluster ?? "alts"),
    };
  });
}

function latestClose(db: Database.Database, symbol: string, interval: string, now: number): number | null {
  const row = one(
    db,
    `SELECT close FROM klines
     WHERE symbol = ? AND interval = ? AND open_time <= ? AND close_time <= ?
     ORDER BY open_time DESC LIMIT 1`,
    symbol, interval, now, now,
  );
  const close = num(row?.close);
  return close && close > 0 ? close : null;
}

function loadTrades(db: Database.Database): TradeRow[] {
  return all(db, "SELECT payload FROM paper_trades ORDER BY id").map((row) => {
    const t = json(row.payload);
    return {
      id: String(t.signal_id ?? ""),
      symbol: String(t.symbol ?? ""),
      book: t.book == null ? null : String(t.book),
      side: String(t.direction ?? ""),
      pnl: num(t.pnl),
      r: num(t.r_multiple),
      reason: t.exit_reason == null ? null : String(t.exit_reason),
      exit: t.exit_time == null ? null : Number(t.exit_time),
      exitPrice: num(t.exit_price),
    };
  }).sort((a, b) => (b.exit ?? 0) - (a.exit ?? 0));
}

function loadSignals(db: Database.Database, now: number, timeout: number): SignalRow[] {
  const packets = all(db, "SELECT payload FROM signals ORDER BY ts_ms DESC LIMIT 80").map((r) => json(r.payload));
  const reviews = new Map<string, Row>();
  for (const row of all(db, "SELECT signal_id, requested_decision, effective_decision, reason, reasoning FROM reviews ORDER BY id")) {
    reviews.set(String(row.signal_id), row);
  }
  const outbox = new Map<string, string>();
  for (const row of all(db, "SELECT signal_id, status FROM webhook_outbox ORDER BY id DESC")) {
    if (!outbox.has(String(row.signal_id))) outbox.set(String(row.signal_id), String(row.status));
  }
  const fills = new Map<string, number>();
  for (const row of all(db, "SELECT signal_id, payload FROM decision_journal WHERE event = 'fill' ORDER BY id")) {
    const payload = json(row.payload);
    if (payload.fill_price != null) fills.set(String(row.signal_id), Number(payload.fill_price));
  }
  return packets.map((packet) => {
    const id = String(packet.signal_id ?? "");
    const review = reviews.get(id);
    const status = reviewStatus(packet, review, now, timeout);
    const kind = packet.packet_kind == null ? null : String(packet.packet_kind);
    return {
      id,
      ts: Number(packet.ts_ms ?? 0),
      symbol: String(packet.symbol ?? ""),
      book: packet.book == null ? null : String(packet.book),
      kind,
      direction: String(packet.direction ?? ""),
      status,
      hint: hint(status, kind),
      requested: review ? String(review.requested_decision ?? "") || null : null,
      effective: review ? String(review.effective_decision ?? "") || null : null,
      reason: review?.reason == null ? null : String(review.reason),
      reasoning: review?.reasoning == null ? null : String(review.reasoning),
      outbox: outbox.get(id) ?? "absent",
      fillPrice: fills.get(id) ?? null,
    };
  });
}

function reviewStatus(packet: Row, review: Row | undefined, now: number, timeout: number): SignalRow["status"] {
  if (review) {
    if (review.reason === "decision_timeout" || review.requested_decision === "timeout") return "timeout";
    if (review.effective_decision === "open") return "open";
    if (review.effective_decision === "skip") return "skip";
    return "pending";
  }
  if (packet.status === "rejected") return "rejected";
  if (now - Number(packet.ts_ms ?? 0) >= timeout) return "timeout";
  return "pending";
}

function hint(status: SignalRow["status"], kind: string | null): string {
  if (status === "pending" && kind === "rebalance") return "Waiting. Silence keeps the current book.";
  if (status === "pending") return "Waiting. Silence becomes a skip when the hour ends.";
  if (status === "timeout") return "The hour passed. Stored as skip.";
  if (status === "open") return "Opened. The paper loop may simulate the fill.";
  if (status === "skip") return "Skipped. This packet will not open.";
  return "Rejected by a hard gate.";
}

function loadEvents(db: Database.Database, recent: Row[]): FeedEvent[] {
  const rows = all(db, "SELECT id, ts_ms, signal_id, event, payload FROM decision_journal ORDER BY id DESC LIMIT 80");
  if (rows.length) {
    return rows.map((row) => {
      const payload = json(row.payload);
      const packet = payload.packet && typeof payload.packet === "object" ? payload.packet as Row : {};
      return {
        id: String(row.id),
        t: Number(row.ts_ms),
        kind: String(row.event),
        book: str(payload.book ?? packet.book),
        symbol: str(payload.symbol ?? packet.symbol),
        text: summary(String(row.event), payload, packet),
      };
    });
  }
  return [...recent].reverse().map((row, i) => ({
    id: String(row.id ?? i),
    t: Number(row.ts_ms ?? 0),
    kind: String(row.event ?? "event"),
    book: null,
    symbol: null,
    text: summary(String(row.event ?? ""), (row.payload as Row) ?? {}, {}),
  }));
}

function summary(event: string, payload: Row, packet: Row): string {
  if (event === "signal") return [packet.symbol, packet.setup, packet.direction, packet.status].filter(Boolean).join(" ");
  if (event === "decision") return `${payload.effective ?? "undecided"}${payload.reason ? ` (${payload.reason})` : ""}${payload.reasoning ? `: ${payload.reasoning}` : ""}`;
  if (event === "fill") return `${payload.fill_price} size ${payload.size}`;
  if (event === "exit") return `${payload.reason ?? "exit"} pnl ${payload.pnl} R ${payload.r_multiple}`;
  if (event === "violation") return `${payload.kind ?? "violation"}: ${payload.detail ?? ""}`;
  return event;
}

interface JournalTail {
  ino: number;
  offset: number;
  lines: number;
  recent: Row[];
}

const journalTails = new Map<string, JournalTail>();

/** Byte offset into journal.jsonl. A later call reads only the bytes appended since the last one. */
export function readJournalTail(file: string | undefined): { lines: number; recent: Row[] } {
  if (!file || !existsSync(file)) return { lines: 0, recent: [] };
  const st = statSync(file);
  let state = journalTails.get(file);
  if (!state || state.ino !== st.ino || st.size < state.offset) {
    state = { ino: st.ino, offset: 0, lines: 0, recent: [] };
    journalTails.set(file, state);
  }
  if (st.size === state.offset) return { lines: state.lines, recent: state.recent };
  const fd = openSync(file, "r");
  try {
    const buf = Buffer.alloc(st.size - state.offset);
    const n = readSync(fd, buf, 0, buf.length, state.offset);
    const slice = buf.subarray(0, n);
    const last = slice.lastIndexOf(0x0a);
    if (last < 0) return { lines: state.lines, recent: state.recent };
    const text = slice.subarray(0, last + 1).toString("utf8");
    state.offset += last + 1;
    for (const line of text.split("\n")) {
      if (!line.trim()) continue;
      state.lines += 1;
      try {
        state.recent.push(JSON.parse(line) as Row);
        if (state.recent.length > 80) state.recent.shift();
      } catch { /* a torn line is not an event */ }
    }
  } finally {
    closeSync(fd);
  }
  return { lines: state.lines, recent: state.recent };
}

function bookCards(db: Database.Database, settings: Record<string, unknown>, positions: PositionRow[], trades: TradeRow[], initial: number, base: number, now: number, started: number | null): BookCard[] {
  const specs = [
    { book: "e2", name: "E2", key: "e2_residual_momentum", cadence: "weekly" },
    { book: "e3", name: "E3", key: "e3_slow_trend", cadence: "daily" },
  ];
  const strategies = dict(settings.strategies);
  return specs.filter((spec) => dict(strategies[spec.key]).enabled !== false).map((spec) => {
    const cfg = dict(strategies[spec.key]);
    const held = positions.filter((p) => p.book === spec.book);
    const mine = trades.filter((t) => t.book === spec.book);
    const pnl = mine.reduce((s, t) => s + (t.pnl ?? 0), 0) + held.reduce((s, p) => s + ledger(db, p, now), 0);
    const notional = held.reduce((s, p) => s + (p.notional ?? 0), 0);
    const points: CurvePoint[] = [{ t: started ?? now, equity: initial }];
    let running = 0;
    for (const trade of [...mine].sort((a, b) => (a.exit ?? 0) - (b.exit ?? 0))) {
      running += trade.pnl ?? 0;
      points.push({ t: trade.exit ?? now, equity: initial + running });
    }
    points.push({ t: now, equity: initial + pnl });
    return {
      book: spec.book,
      name: spec.name,
      variant: String(cfg.variant ?? ""),
      cadence: spec.cadence,
      pnl,
      ret: initial ? pnl / initial : null,
      drawdown: maxDrawdown(points.map((p) => p.equity)),
      leverage: base ? notional / base : null,
      trades: mine.length,
      curve: points,
    };
  });
}

function btcCurve(db: Database.Database, started: number | null, initial: number, now: number): { curve: CurvePoint[]; ret: number | null } {
  if (started == null || !(initial > 0)) return { curve: [], ret: null };
  let series = closes(db, "BTCUSDT", "1h", now, started);
  if (!series.length) series = closes(db, "BTCUSDT", "1d", now, started);
  if (!series.length) return { curve: [], ret: null };
  let startPx: number | null = null;
  for (const bar of series) if (bar.t <= started) startPx = bar.px;
  if (startPx == null) startPx = series[0].px;
  const curve: CurvePoint[] = [{ t: started, equity: initial }];
  for (const bar of series) if (bar.t > started) curve.push({ t: bar.t, equity: initial * bar.px / startPx });
  return { curve: downsample(curve), ret: curve[curve.length - 1].equity / initial - 1 };
}

function closes(db: Database.Database, symbol: string, interval: string, now: number, started: number): { t: number; px: number }[] {
  const width = interval === "1d" ? DAY : interval === "4h" ? 4 * HOUR : HOUR;
  const seed = one(
    db,
    `SELECT close_time AS t, close AS px FROM klines
     WHERE symbol = ? AND interval = ? AND open_time <= ? AND close_time <= ? AND close > 0
     ORDER BY open_time DESC LIMIT 1`,
    symbol, interval, started, started,
  );
  const rows = all(
    db,
    `SELECT close_time AS t, close AS px FROM klines
     WHERE symbol = ? AND interval = ? AND open_time > ? AND close_time <= ? AND close > 0
     ORDER BY open_time`,
    symbol, interval, started - width, now,
  );
  const out: { t: number; px: number }[] = [];
  if (seed?.t != null) out.push({ t: Number(seed.t), px: Number(seed.px) });
  for (const row of rows) {
    const point = { t: Number(row.t), px: Number(row.px) };
    if (point.t > started) out.push(point);
  }
  return out;
}

/** Names we can price from the (symbol, interval, open_time) primary key. */
const REFERENCE_SYMBOLS = ["BTCUSDT", "ETHUSDT"];
const FRESHNESS_MS = 60_000;
const INTERVALS = ["1h", "4h", "1d"] as const;

interface FreshnessRow {
  interval: string;
  fresh: boolean;
  plain: string;
  symbols: number;
}

interface MarketView {
  freshness: FreshnessRow[];
  stale: { blocking: boolean; plain: string };
}

const marketCache = new Map<string, MarketView>();

function loadMarket(db: Database.Database, settings: Record<string, unknown>, now: number): MarketView {
  const maxAge = Number(dict(settings.paper).max_data_age_seconds ?? 5400) * 1000;
  const setups = Object.values(dict(settings.setups)).some((cfg) => dict(cfg).enabled === true);
  const key = `${db.name}|${Math.floor(now / FRESHNESS_MS)}|${maxAge}|${setups ? 1 : 0}`;
  const hit = marketCache.get(key);
  if (hit) return hit;
  const symbols = watchSymbols(db);
  const closeAt = new Map<string, number | null>();
  for (const interval of INTERVALS) {
    for (const symbol of symbols) closeAt.set(`${symbol}|${interval}`, latestCloseTime(db, symbol, interval, now));
  }
  const view = { freshness: freshnessRows(closeAt, symbols, maxAge, now), stale: staleFrom(closeAt, symbols, setups, maxAge, now) };
  marketCache.set(key, view);
  if (marketCache.size > 8) marketCache.delete(marketCache.keys().next().value ?? "");
  return view;
}

function watchSymbols(db: Database.Database): string[] {
  const held = all(db, "SELECT symbol FROM paper_book_positions").map((row) => String(row.symbol));
  const names = [...new Set([...REFERENCE_SYMBOLS, ...held])].sort();
  const listed = all(db, "SELECT symbol, status FROM symbols");
  if (!listed.length) return names;
  const status = new Map(listed.map((row) => [String(row.symbol), String(row.status ?? "")]));
  return names.filter((name) => status.get(name) !== "DELISTED");
}

function latestCloseTime(db: Database.Database, symbol: string, interval: string, now: number): number | null {
  const row = one(
    db,
    `SELECT close_time AS t FROM klines
     WHERE symbol = ? AND interval = ? AND open_time <= ? AND close_time <= ?
     ORDER BY open_time DESC LIMIT 1`,
    symbol, interval, now, now,
  );
  return row?.t == null ? null : Number(row.t);
}

function freshnessRows(closeAt: Map<string, number | null>, symbols: string[], maxAge: number, now: number): FreshnessRow[] {
  const expected = Math.floor(now / DAY) * DAY;
  return INTERVALS.map((interval) => {
    let latest: number | null = null;
    let count = 0;
    for (const symbol of symbols) {
      const close = closeAt.get(`${symbol}|${interval}`) ?? null;
      if (close == null) continue;
      count += 1;
      if (latest == null || close > latest) latest = close;
    }
    const fresh = latest == null ? false : interval === "1d" ? latest + maxAge >= expected : now - latest <= maxAge;
    const plain = latest == null
      ? `No ${interval} bars are stored for ${symbols.join(", ") || "BTCUSDT, ETHUSDT"}.`
      : `Latest ${interval} close ${formatMyt(latest)}.`;
    return { interval, fresh, plain, symbols: count };
  });
}

function staleFrom(
  closeAt: Map<string, number | null>,
  symbols: string[],
  setups: boolean,
  maxAge: number,
  now: number,
): { blocking: boolean; plain: string } {
  if (setups) {
    const stale = symbols.filter((symbol) => {
      const close = closeAt.get(`${symbol}|1h`) ?? null;
      return close == null || now - close > maxAge;
    });
    if (stale.length) return { blocking: true, plain: `New entries are refused. Stale 1h bars: ${stale.slice(0, 8).join(",")}.` };
  }
  if (!symbols.length) return { blocking: true, plain: "New entries are refused. No daily names are stored to check." };
  const expected = Math.floor(now / DAY) * DAY;
  const stale = symbols.filter((symbol) => {
    const close = closeAt.get(`${symbol}|1d`) ?? null;
    return close == null || close + maxAge < expected;
  });
  if (stale.length) return { blocking: true, plain: `New entries are refused because stored daily bars are stale (${stale.slice(0, 8).join(",")}). Stops still run.` };
  return { blocking: false, plain: "New entries are not blocked by stale data. Closed 1d bars are fresh." };
}

function clockView(started: number | null, now: number, days: number) {
  const duration = days * DAY;
  if (started == null) {
    return { day: 0, days, fraction: 0, started: null, plain: "The paper clock has not started.", complete: false };
  }
  const elapsed = Math.max(0, now - started);
  const day = Math.round((Date.parse(mytDay(now)) - Date.parse(mytDay(started))) / DAY) + 1;
  const complete = elapsed >= duration;
  return {
    day, days, fraction: Math.min(1, elapsed / duration), started, complete,
    plain: `Day ${day} of ${days}. Started ${formatMyt(started, true)}.${complete ? " The clock window has elapsed." : ""}`,
  };
}

function windows(now: number, timeout: number): WindowRow[] {
  const e2 = mondayWindow(now, timeout);
  const e3 = dailyWindow(now, timeout);
  return [
    { book: "e2", name: "E2", state: e2.state, opens: e2.open, deadline: e2.open + timeout, plain: windowPlain("E2", e2.state, e2.open, timeout, true) },
    { book: "e3", name: "E3", state: e3.state, opens: e3.open, deadline: e3.open + timeout, plain: windowPlain("E3", e3.state, e3.open, timeout, false) },
  ];
}

function dailyWindow(now: number, timeout: number): { open: number; state: "open" | "upcoming" } {
  const midnight = Math.floor(now / DAY) * DAY;
  if (now < midnight + timeout) return { open: midnight, state: "open" };
  return { open: midnight + DAY, state: "upcoming" };
}

function mondayWindow(now: number, timeout: number): { open: number; state: "open" | "upcoming" } {
  const midnight = Math.floor(now / DAY) * DAY;
  const utcDay = new Date(now).getUTCDay();
  const weekday = (utcDay + 6) % 7;
  let monday = midnight + ((7 - weekday) % 7) * DAY;
  if (now >= monday + timeout) monday += 7 * DAY;
  const state = monday <= now && now < monday + timeout ? "open" as const : "upcoming" as const;
  return { open: monday, state };
}

function windowPlain(name: string, state: "open" | "upcoming", open: number, timeout: number, weekly: boolean): string {
  const span = formatSpan(open, timeout);
  if (state === "open") {
    return weekly
      ? `${name} window is open until ${formatMyt(open + timeout)}. Silence keeps the current book.`
      : `${name} entry window is open until ${formatMyt(open + timeout)}. The trail and stops do not wait.`;
  }
  return weekly
    ? `Next ${name} rebalance is ${span}. Silence keeps the current book.`
    : `Next ${name} entry window is ${span}. One packet per new entry.`;
}

function newsView(status: unknown): { blocking: boolean; plain: string } {
  if (status == null || status === "") {
    return { blocking: true, plain: "Entries refused. account.news_status is empty, so the news gate fails closed. Stops still run." };
  }
  if (String(status).toUpperCase() === "ALLOW") {
    return { blocking: false, plain: "News gate is ALLOW. That is a config assumption, not a News Watcher feed." };
  }
  return { blocking: true, plain: `Entries refused. account.news_status is ${status}, not ALLOW.` };
}

function killView(file: string): { on: boolean; plain: string } {
  if (!existsSync(file)) return { on: false, plain: `Kill switch is off. ${file} is not present.` };
  let text = "";
  try { text = readFileSync(file, "utf8").slice(0, 200).trim(); } catch { text = "unreadable"; }
  return { on: true, plain: `Kill switch is on (${text || "present"}). New entries are blocked. This file does not cancel an exchange order.` };
}

function cyclePlain(block: string | null, newsBlocking: boolean, recent: boolean): string {
  let text = block === "kill_switch" ? "The last cycle was blocked by the kill switch."
    : block === "stale_data" ? "The last cycle was blocked because stored bars were stale."
      : block ? `The last cycle recorded a block: ${block}.`
        : newsBlocking ? "The last cycle did not record a block. New entries are still refused because the news gate is closed."
          : "The last cycle did not record a block.";
  if (!recent) text += " That cycle is older than three loop intervals.";
  return text;
}

function roster(settings: Record<string, unknown>): string {
  const strategies = dict(settings.strategies);
  const on: string[] = [];
  const off: string[] = [];
  const map: [string, string][] = [
    ["e2_residual_momentum", "E2"], ["e3_slow_trend", "E3"], ["e1_cs_momentum", "E1"],
    ["e4_funding_carry", "E4"], ["e5_positioning_trend", "E5"], ["e6_liquidation_proxy", "E6"],
  ];
  for (const [key, name] of map) (dict(strategies[key]).enabled ? on : off).push(name);
  const setupsOn = Object.values(dict(settings.setups)).some((cfg) => dict(cfg).enabled === true);
  if (!setupsOn) off.push("setups A–D");
  return `${on.join(", ") || "none"} on. ${off.join(", ")} off.`;
}

function readRefresh(file: string | undefined, now: number) {
  const empty = { visionFiles: 0, fapiRequests: 0, pauses: 0, http429: 0, http418: 0, paused: false, reason: null as string | null, stale: true };
  if (!file || !existsSync(file)) return { totals: empty, stale: true };
  try {
    const payload = JSON.parse(readFileSync(file, "utf8")) as Row;
    const totals = dict(payload.totals);
    const updated = num(payload.updated_ms);
    return {
      stale: updated == null || now - updated > 15 * 60_000,
      totals: {
        visionFiles: Number(totals.vision_files ?? 0),
        fapiRequests: Number(totals.fapi_requests ?? 0),
        pauses: Number(totals.pauses ?? 0),
        http429: Number(totals.http_429 ?? 0),
        http418: Number(totals.http_418 ?? 0),
        paused: Boolean(payload.paused),
        reason: payload.pause_reason == null ? null : String(payload.pause_reason),
      },
    };
  } catch {
    return { totals: empty, stale: true };
  }
}

function refreshPlain(refresh: ReturnType<typeof readRefresh>, weight: number, pauseAbove: number): string {
  const t = refresh.totals;
  const state = t.paused
    ? `fapi is paused (${t.reason ?? "budget"}). The loop does not retry in a tight loop.`
    : "Refresh is not paused.";
  return `${state} Since the status file: ${t.visionFiles} vision files, ${t.fapiRequests} fapi requests, ${t.pauses} pauses, ${t.http429} HTTP 429, ${t.http418} HTTP 418. Budget ${weight} weight/min, pause above ${pauseAbove}.`;
}

function archiveDays(db: Database.Database): number {
  const row = one(db, "SELECT COUNT(*) AS n FROM vision_days");
  return Number(row?.n ?? 0);
}

function scorecard(db: Database.Database, settings: Record<string, unknown>, trades: TradeRow[], curve: CurvePoint[], started: number | null, now: number, settingsPath: string) {
  const rs = trades.map((t) => t.r).filter((r): r is number => r != null);
  const wins = rs.filter((r) => r > 0).length;
  const reviews = all(db, "SELECT requested_decision, effective_decision, reason FROM reviews");
  const elapsedDays = started == null ? 0 : Math.max(0, (now - started) / DAY);
  const minDays = Number(dict(dict(settings.paper).drift).min_days ?? 7);
  const report = loadReport(findStrategyReport(settingsPath));
  const books = [
    { book: "e2", name: "E2", key: "e2_residual_momentum" },
    { book: "e3", name: "E3", key: "e3_slow_trend" },
  ].filter((b) => dict(dict(settings.strategies)[b.key]).enabled !== false).map((b) => {
    const expected = dict(dict(dict(settings.strategies)[b.key]).expected);
    const hold = report[b.name];
    const plain = elapsedDays < minDays
      ? `Drift alerts start after ${minDays} days. This clock has run ${elapsedDays.toFixed(1)} days, so a gap is not an alert yet.`
      : "Inside the window where drift is scored against the hold-out pace.";
    const holdText = hold
      ? `Full hold-out, not the 14-day pace: CAGR ${(hold.cagr * 100).toFixed(1)}%, Sharpe ${hold.sharpe.toFixed(2)}, max DD ${(hold.maxDd * 100).toFixed(1)}%, expectancy ${hold.expectancy >= 0 ? "+" : ""}${hold.expectancy.toFixed(2)}R, ${hold.trades} trades.`
      : `Configured hold-out turnover ${expected.turnover_per_year ?? "—"}×/year.`;
    return { book: b.book, name: b.name, variant: String(dict(dict(settings.strategies)[b.key]).variant ?? ""), plain, hold: holdText };
  });
  const e6 = report.E6;
  const e6On = dict(dict(settings.strategies).e6_liquidation_proxy).enabled === true;
  return {
    trades: trades.length,
    winRate: trades.length ? wins / trades.length : null,
    expectancy: rs.length ? rs.reduce((s, r) => s + r, 0) / rs.length : null,
    maxDrawdown: maxDrawdown(curve.map((p) => p.equity)),
    opened: reviews.filter((r) => r.effective_decision === "open").length,
    skipped: reviews.filter((r) => r.effective_decision === "skip").length,
    timeouts: reviews.filter((r) => r.reason === "decision_timeout").length,
    books,
    e6Note: e6 && !e6On ? `The study's E6 hold-out was ${e6.trades} trades. The running config leaves E6 off. This page does not turn it on.` : null,
  };
}

function loadReport(file: string | null): Record<string, { cagr: number; sharpe: number; maxDd: number; expectancy: number; trades: number }> {
  if (!file || !existsSync(file)) return {};
  const text = readFileSync(file, "utf8");
  const out: Record<string, { cagr: number; sharpe: number; maxDd: number; expectancy: number; trades: number }> = {};
  const re = /Hold-out: CAGR ([-0-9.]+)%, Sharpe ([-0-9.]+), max DD ([-0-9.]+)%, total return [-0-9.]+%, turnover [-0-9.]+[×x]\/year, fees \$[0-9,]+, slippage \$[0-9,]+,.*?trades ([0-9,]+), expectancy R ([-0-9.]+)/;
  for (const section of text.split("\n### ")) {
    const family = section.match(/^(E[1-6])\b/)?.[1];
    const hold = section.match(re);
    if (!family || !hold) continue;
    out[family] = {
      cagr: Number(hold[1]) / 100,
      sharpe: Number(hold[2]),
      maxDd: Number(hold[3]) / 100,
      trades: Number(hold[4].replace(/,/g, "")),
      expectancy: Number(hold[5]),
    };
  }
  return out;
}

export function findStrategyReport(settingsPath: string): string | null {
  const dir = path.join(path.resolve(path.dirname(settingsPath), ".."), "reports");
  if (!existsSync(dir)) return null;
  const files = readdirSync(dir).filter((name) => name.startsWith("strategies-") && name.endsWith(".md")).sort();
  return files.length ? path.join(dir, files[files.length - 1]) : null;
}

function clusterRows(positions: PositionRow[], cap: number | null) {
  const groups = new Map<string, number>();
  for (const pos of positions) groups.set(pos.cluster, (groups.get(pos.cluster) ?? 0) + pos.risk);
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([cluster, risk]) => ({
    cluster, risk, cap, fraction: cap ? risk / cap : null,
  }));
}

function resolveKill(settings: Record<string, unknown>, settingsPath: string): string {
  if (process.env.KILL_SWITCH_PATH) return process.env.KILL_SWITCH_PATH;
  const raw = String(dict(settings.paper).kill_switch_path ?? "data/KILL");
  if (path.isAbsolute(raw)) return raw;
  return path.resolve(path.dirname(settingsPath), "..", raw);
}

function clampWeight(raw: number): number {
  if (!Number.isFinite(raw) || raw < 1) return 300;
  return Math.min(600, raw);
}

function maxDrawdown(values: number[]): number | null {
  if (!values.length) return null;
  let peak = values[0];
  let worst = 0;
  for (const value of values) {
    if (value > peak) peak = value;
    if (peak > 0) worst = Math.max(worst, (peak - value) / peak);
  }
  return worst;
}

function downsample(points: CurvePoint[], limit = 480): CurvePoint[] {
  if (points.length <= limit) return points;
  const step = points.length / limit;
  const picked = Array.from({ length: limit }, (_, i) => points[Math.min(points.length - 1, Math.floor(i * step))]);
  if (picked[picked.length - 1] !== points[points.length - 1]) picked.push(points[points.length - 1]);
  return picked;
}

function emptySnapshot(now: number, settings: Record<string, unknown>, error: string, input: ReadInput): DeskSnapshot {
  const days = Number(dict(settings.paper).clock_days ?? 14);
  return {
    now, readOnly: true, orders: false, mode: String(settings.mode ?? "paper"), error,
    linkLabel: "NO DATABASE",
    clock: clockView(null, now, days),
    windows: windows(now, 3600_000),
    health: {
      newsPlain: newsView(dict(settings.account).news_status).plain,
      newsBlocking: newsView(dict(settings.account).news_status).blocking,
      killPlain: "Kill switch was not checked.",
      killOn: false,
      stalePlain: error,
      staleBlocking: true,
      ordersPlain: "Orders are off.",
      equityPlain: error,
      cyclePlain: error,
      cycleRecent: false,
      roster: roster(settings),
      freshness: [],
      refreshPlain: "No database, so refresh counters were not read.",
      weightPerMinute: 300,
      pauseAbove: 1200,
      refresh: { visionFiles: 0, fapiRequests: 0, pauses: 0, http429: 0, http418: 0, paused: false, reason: null },
      archiveDays: 0,
      reviewPlain: "Not checked.",
      reviewOk: false,
      journalLines: readJournalTail(input.journalPath).lines,
    },
    equity: null, initialEquity: null, openPnl: null, deskReturn: null, drawdown: null, grossLeverage: null,
    openRisk: { usdt: 0, cap: null, fraction: null, capPct: 0.06 },
    clusters: [], deskCurve: [], btcCurve: [], btcReturn: null, books: [], positions: [], signals: [],
    signalCounts: {}, trades: [], events: [],
    scorecard: { trades: 0, winRate: null, expectancy: null, maxDrawdown: null, opened: 0, skipped: 0, timeouts: 0, books: [], e6Note: null },
  };
}

function mytParts(ms: number): { weekday: number; day: number; month: number; year: number; h: number; m: number; s: number } {
  const shifted = new Date(ms + 8 * HOUR);
  return {
    weekday: (shifted.getUTCDay() + 6) % 7,
    day: shifted.getUTCDate(),
    month: shifted.getUTCMonth(),
    year: shifted.getUTCFullYear(),
    h: shifted.getUTCHours(),
    m: shifted.getUTCMinutes(),
    s: shifted.getUTCSeconds(),
  };
}

function mytDay(ms: number): string {
  const p = mytParts(ms);
  return `${p.year}-${String(p.month + 1).padStart(2, "0")}-${String(p.day).padStart(2, "0")}T00:00:00Z`;
}

export function formatMyt(ms: number, seconds = false): string {
  const p = mytParts(ms);
  const clock = seconds
    ? `${pad(p.h)}:${pad(p.m)}:${pad(p.s)}`
    : `${pad(p.h)}:${pad(p.m)}`;
  return `${WEEKDAYS[p.weekday]} ${pad(p.day)} ${MONTHS[p.month]} ${p.year}, ${clock} MYT`;
}

function formatSpan(open: number, timeout: number): string {
  const start = mytParts(open);
  const end = mytParts(open + timeout);
  const utc0 = new Date(open);
  const utc1 = new Date(open + timeout);
  const utc = (d: Date) => `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  return `${WEEKDAYS[start.weekday]} ${pad(start.day)} ${MONTHS[start.month]} ${start.year}, ${pad(start.h)}:${pad(start.m)}–${pad(end.h)}:${pad(end.m)} MYT (${utc(utc0)}–${utc(utc1)} UTC)`;
}

function pad(n: number): string { return String(n).padStart(2, "0"); }

let traceQuery: ((sql: string, params: unknown[]) => void) | null = null;

function all(db: Database.Database, sql: string, ...params: unknown[]): Row[] {
  traceQuery?.(sql, params);
  try { return db.prepare(sql).all(...params) as Row[]; } catch { return []; }
}
function one(db: Database.Database, sql: string, ...params: unknown[]): Row | undefined {
  traceQuery?.(sql, params);
  try { return db.prepare(sql).get(...params) as Row | undefined; } catch { return undefined; }
}
function json(value: unknown): Row {
  if (typeof value !== "string") return {};
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === "object" ? parsed as Row : {};
  } catch { return {}; }
}
function dict(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function num(value: unknown): number | null {
  if (value == null || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
function str(value: unknown): string | null {
  return value == null || value === "" ? null : String(value);
}
