/** Snapshot the page paints. No secrets, no order path. */

export interface CurvePoint {
  t: number;
  equity: number;
}

export interface PositionRow {
  book: string;
  symbol: string;
  side: "long" | "short";
  size: number;
  entry: number;
  stop: number;
  mark: number | null;
  r: number | null;
  unrealized: number | null;
  risk: number;
  notional: number | null;
  cluster: string;
}

export interface BookCard {
  book: string;
  name: string;
  variant: string;
  cadence: string;
  pnl: number;
  ret: number | null;
  drawdown: number | null;
  leverage: number | null;
  trades: number;
  curve: CurvePoint[];
}

export interface SignalRow {
  id: string;
  ts: number;
  symbol: string;
  book: string | null;
  kind: string | null;
  direction: string;
  status: "pending" | "open" | "skip" | "timeout" | "rejected";
  hint: string;
  requested: string | null;
  effective: string | null;
  reason: string | null;
  reasoning: string | null;
  outbox: string;
  fillPrice: number | null;
}

export interface TradeRow {
  id: string;
  symbol: string;
  book: string | null;
  side: string;
  pnl: number | null;
  r: number | null;
  reason: string | null;
  exit: number | null;
  exitPrice: number | null;
}

export interface FeedEvent {
  id: string;
  t: number;
  kind: string;
  book: string | null;
  symbol: string | null;
  text: string;
}

export interface WindowRow {
  book: "e2" | "e3";
  name: string;
  state: "open" | "upcoming";
  plain: string;
  opens: number;
  deadline: number;
}

export interface DeskSnapshot {
  now: number;
  readOnly: true;
  orders: false;
  mode: string;
  error: string | null;
  linkLabel: string;
  clock: {
    day: number;
    days: number;
    fraction: number;
    started: number | null;
    plain: string;
    complete: boolean;
  };
  windows: WindowRow[];
  health: {
    newsPlain: string;
    newsBlocking: boolean;
    killPlain: string;
    killOn: boolean;
    stalePlain: string;
    staleBlocking: boolean;
    ordersPlain: string;
    equityPlain: string;
    cyclePlain: string;
    cycleRecent: boolean;
    roster: string;
    freshness: { interval: string; fresh: boolean; plain: string; symbols: number }[];
    refreshPlain: string;
    weightPerMinute: number;
    pauseAbove: number;
    refresh: {
      visionFiles: number;
      fapiRequests: number;
      pauses: number;
      http429: number;
      http418: number;
      paused: boolean;
      reason: string | null;
    };
    archiveDays: number;
    reviewPlain: string;
    reviewOk: boolean;
    journalLines: number;
  };
  equity: number | null;
  initialEquity: number | null;
  openPnl: number | null;
  deskReturn: number | null;
  drawdown: number | null;
  grossLeverage: number | null;
  openRisk: { usdt: number; cap: number | null; fraction: number | null; capPct: number };
  clusters: { cluster: string; risk: number; cap: number | null; fraction: number | null }[];
  deskCurve: CurvePoint[];
  btcCurve: CurvePoint[];
  btcReturn: number | null;
  books: BookCard[];
  positions: PositionRow[];
  signals: SignalRow[];
  signalCounts: Record<string, number>;
  trades: TradeRow[];
  events: FeedEvent[];
  scorecard: {
    trades: number;
    winRate: number | null;
    expectancy: number | null;
    maxDrawdown: number | null;
    opened: number;
    skipped: number;
    timeouts: number;
    books: { book: string; name: string; variant: string; plain: string; hold: string }[];
    e6Note: string | null;
  };
}
