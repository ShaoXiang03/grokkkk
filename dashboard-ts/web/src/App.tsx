import { FormEvent, useEffect, useState, type ReactNode } from "react";
import type { DeskSnapshot, PositionRow } from "../../shared/types";
import { ago, clock, coin, fmtPx, fmtUsd, hhmmss, pct, useDesk } from "./lib";
import { EquityChart, Spark } from "./panels/Charts";

function Panel({ title, sub, right, children }: { title: string; sub?: string; right?: string; children: ReactNode }) {
  return (
    <section className="panel">
      <header className="panel-h">
        <h2>{title}</h2>
        {sub && <span className="sub">{sub}</span>}
        {right && <span className="panel-r">{right}</span>}
      </header>
      <div className="panel-b">{children}</div>
    </section>
  );
}

function meter(fraction: number | null) {
  const pctValue = fraction == null ? 0 : Math.max(0, fraction * 100);
  const tone = pctValue >= 100 ? "bad" : pctValue >= 80 ? "warn" : "";
  return { width: Math.min(100, pctValue), tone, label: fraction == null ? "—" : `${pctValue.toFixed(0)}%` };
}

export function App() {
  const [authed, setAuthed] = useState(false);
  const [checked, setChecked] = useState(false);
  useEffect(() => {
    fetch("/api/state", { cache: "no-store" }).then((r) => {
      setAuthed(r.ok);
      setChecked(true);
    }).catch(() => setChecked(true));
  }, []);
  if (!checked) return <div className="boot"><span className="orb" /> connecting</div>;
  if (!authed) return <Login onIn={() => setAuthed(true)} />;
  return <Desk onOut={() => setAuthed(false)} />;
}

function Login({ onIn }: { onIn: () => void }) {
  const [token, setToken] = useState("");
  const [err, setErr] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    const response = await fetch("/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token }),
    });
    if (response.ok) { setToken(""); onIn(); return; }
    setErr(response.status === 429 ? "Too many attempts. Wait and try again." : "That token was refused.");
  }
  return (
    <div className="login">
      <form onSubmit={submit}>
        <div className="brand"><span className="orb" /><h1>PAPER DESK</h1></div>
        <p>Read-only view of the chart-analyst paper books. The token is not stored in the page and is not sent back by the server.</p>
        <input type="password" autoComplete="current-password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="DASHBOARD_TOKEN" aria-label="Dashboard token" />
        <button type="submit">UNLOCK</button>
        {err && <p className="err">{err}</p>}
      </form>
    </div>
  );
}

function Desk({ onOut }: { onOut: () => void }) {
  const { state, connected, denied } = useDesk(true);
  const [book, setBook] = useState<"all" | "e2" | "e3">("all");
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(id); }, []);
  useEffect(() => { if (denied) onOut(); }, [denied, onOut]);
  if (!state) return <div className="boot"><span className="orb" /> loading the ledger</div>;
  const serverNow = now + clock.skew;
  const ok = connected && state.health.cycleRecent && !state.error;
  const positions = state.positions.filter((p) => book === "all" || p.book === book);
  const signals = state.signals.filter((s) => book === "all" || s.book === book);
  const trades = state.trades.filter((t) => book === "all" || t.book === book);
  return (
    <div className="desk">
      <header className="top">
        <div className="brand">
          <span className="orb" aria-hidden="true" />
          <div>
            <strong>PAPER DESK</strong>
            <small>binance usdt-m · e2 weekly · e3 daily · read only</small>
          </div>
        </div>
        <nav className="booktabs" aria-label="Pages"><button type="button" className="on">DESK</button></nav>
        <div className="mode">PAPER · NO ORDERS</div>
        <div className="booktabs" role="tablist" aria-label="Book">
          {(["all", "e2", "e3"] as const).map((id) => (
            <button type="button" key={id} className={book === id ? "on" : ""} onClick={() => setBook(id)}>{id.toUpperCase()}</button>
          ))}
        </div>
        <dl className="kpis">
          <div><dt>equity</dt><dd className={(state.deskReturn ?? 0) >= 0 ? "up" : "dn"}>{fmtUsd(state.equity)}</dd></div>
          <div><dt>open P&L</dt><dd className={(state.openPnl ?? 0) >= 0 ? "up" : "dn"}>{fmtUsd(state.openPnl)}</dd></div>
          <div><dt>open risk</dt><dd>{state.openRisk.fraction == null ? "—" : `${(state.openRisk.fraction * 100).toFixed(0)}%`}</dd></div>
        </dl>
        <div className="clocks">
          <span className="clock">{hhmmss(serverNow, "UTC")}<small>UTC</small></span>
          <span className="clock myt">{hhmmss(serverNow, "MYT")}<small>MYT</small></span>
        </div>
        <div className={`link ${ok ? "ok" : "bad"}`}><span className="dot" />{connected ? state.linkLabel : "RECONNECTING"}</div>
        <button type="button" className="signout" onClick={async () => { await fetch("/logout", { method: "POST" }); onOut(); }}>SIGN OUT</button>
      </header>

      <div className="wire" aria-label="Latest journal events">
        <span className="wire-tag">LIVE WIRE</span>
        <div className="wire-row">
          {state.events.slice(0, 8).map((event) => (
            <span key={event.id} className="wire-item">
              <b>{event.kind.toUpperCase()}</b>
              <span>{event.text}</span>
              <time>{ago(event.t, serverNow)}</time>
            </span>
          ))}
          {!state.events.length && <span className="dim">no journal rows yet</span>}
        </div>
      </div>

      {state.health.newsBlocking && <div className="banner">{state.health.newsPlain}</div>}
      {state.error && <div className="banner">{state.error}</div>}

      <div className="row3">
        <Panel title="SAFETY" sub={state.health.roster}>
          <div className="switches">
            <div className={`sw ${state.health.newsBlocking ? "bad" : ""}`}><b>NEWS</b><p>{state.health.newsPlain}</p></div>
            <div className={`sw ${state.health.killOn ? "bad" : ""}`}><b>KILL</b><p>{state.health.killPlain}</p></div>
            <div className={`sw ${state.health.staleBlocking ? "bad" : ""}`}><b>DATA</b><p>{state.health.stalePlain}</p></div>
            <div className="sw"><b>ORDERS</b><p>{state.health.ordersPlain}</p></div>
          </div>
          <div className="pills" style={{ marginTop: 8 }}>
            {state.health.freshness.map((row) => (
              <span key={row.interval} className={`chip ${row.fresh ? "st-open" : "st-rejected"}`}>{row.interval} {row.fresh ? "fresh" : "stale"}</span>
            ))}
          </div>
          <p className="note">{state.health.reviewPlain} Weight {state.health.weightPerMinute}/min · pause above {state.health.pauseAbove} · 429 {state.health.refresh.http429} · vision {state.health.refresh.visionFiles} · fapi {state.health.refresh.fapiRequests}</p>
        </Panel>
        <Panel title="PAPER CLOCK" sub={state.clock.plain} right={`day ${state.clock.day} / ${state.clock.days}`}>
          <div className="meter"><i style={{ width: `${state.clock.fraction * 100}%` }} /></div>
          {state.windows.map((w) => (
            <p key={w.book} style={{ marginTop: 10 }}>
              <span className={`chip ${w.state === "open" ? "st-open" : "st-pending"}`}>{w.state}</span>{" "}
              <b>{w.name}</b> {w.plain}
            </p>
          ))}
          <p className="note">{state.health.cyclePlain}</p>
        </Panel>
        <Panel title="THE BOOK" sub="shared ledger · from 800 USDT" right={state.mode}>
          <div className="pnl-big">
            <span className={`eq ${(state.deskReturn ?? 0) >= 0 ? "up" : "dn"}`}>{fmtUsd(state.equity)}</span>
            <span className={(state.deskReturn ?? 0) >= 0 ? "up" : "dn"}>{pct(state.deskReturn)}</span>
          </div>
          <div className="legend" style={{ margin: "6px 0" }}>
            <span><i style={{ background: "#4ade80" }} />DESK</span>
            <span><i style={{ background: "#f5b942" }} />BTC HOLD {pct(state.btcReturn)}</span>
          </div>
          <EquityChart desk={state.deskCurve} btc={state.btcCurve} />
          <p className="note">Marks on E2 and E3 use the latest daily close. BTC buy-and-hold uses stored closes since the clock start.</p>
        </Panel>
      </div>

      <div className="books">
        {state.books.filter((b) => book === "all" || b.book === book).map((b) => (
          <Panel key={b.book} title={b.name} sub={`${b.variant} · ${b.cadence}`} right={`${b.trades} closed`}>
            <div className="pnl-stats">
              <div><dt>PnL</dt><dd className={b.pnl >= 0 ? "up" : "dn"}>{fmtUsd(b.pnl)}</dd></div>
              <div><dt>return</dt><dd className={(b.ret ?? 0) >= 0 ? "up" : "dn"}>{pct(b.ret)}</dd></div>
              <div><dt>leverage</dt><dd>{b.leverage == null ? "—" : `${b.leverage.toFixed(2)}×`}</dd></div>
            </div>
            <Spark data={b.curve.map((p) => p.equity)} color={b.book === "e3" ? "#4ade80" : "#f5b942"} />
          </Panel>
        ))}
      </div>

      <Panel title="RISK" sub="6% open · 3% cluster · 3× per name">
        <div className="meter-l"><span>open risk {fmtUsd(state.openRisk.usdt)} / {fmtUsd(state.openRisk.cap)}</span><strong>{meter(state.openRisk.fraction).label}</strong></div>
        <div className={`meter ${meter(state.openRisk.fraction).tone}`}><i style={{ width: `${meter(state.openRisk.fraction).width}%` }} /></div>
        {state.clusters.map((c) => {
          const m = meter(c.fraction);
          return (
            <div key={c.cluster}>
              <div className="meter-l"><span>{c.cluster} {fmtUsd(c.risk)} / {fmtUsd(c.cap)}</span><strong>{m.label}</strong></div>
              <div className={`meter ${m.tone}`}><i style={{ width: `${m.width}%` }} /></div>
            </div>
          );
        })}
        <p className="note">Gross leverage {state.grossLeverage == null ? "—" : `${state.grossLeverage.toFixed(2)}×`}. Book clusters sum both directions. Weight budget {state.health.weightPerMinute}/min, pause above {state.health.pauseAbove}. {state.health.refreshPlain}</p>
      </Panel>

      <div className="row2">
        <Panel title="POSITIONS" sub="open on the shared book" right={`${positions.length} names`}>
          <div className="scroll">
            <table>
              <thead><tr><th>coin</th><th>side</th><th>size</th><th>entry</th><th>stop</th><th>mark</th><th>R</th><th>unreal</th><th>risk</th></tr></thead>
              <tbody>
                {positions.map((p) => <Pos key={`${p.book}-${p.symbol}`} p={p} />)}
                {!positions.length && <tr><td colSpan={9} className="empty">Flat.</td></tr>}
              </tbody>
            </table>
          </div>
        </Panel>
        <Panel title="SIGNALS" sub="review hour is 08:00–09:00 MYT" right="read only">
          <div className="pills">
            {Object.entries(state.signalCounts).map(([name, n]) => <span key={name} className={`chip st-${name}`}>{name} {n}</span>)}
          </div>
          <div className="scroll">
            {signals.map((s) => (
              <article key={s.id} className="signal">
                <header>
                  <b>{(s.book ?? "").toUpperCase()} {coin(s.symbol)} {s.direction}</b>
                  <span className={`chip st-${s.status}`}>{s.status}</span>
                </header>
                <div className="dim">{s.hint}</div>
                <div>{s.reasoning || s.reason || "no decision yet"}</div>
                <div className="dim">outbox {s.outbox}{s.fillPrice != null ? ` · fill ${fmtPx(s.fillPrice)}` : ""}</div>
              </article>
            ))}
            {!signals.length && <p className="empty">No packets.</p>}
          </div>
        </Panel>
      </div>

      <div className="row2">
        <Panel title="TRADES" sub="closed paper fills">
          <div className="scroll">
            <table>
              <thead><tr><th>book</th><th>coin</th><th>side</th><th>exit</th><th>pnl</th><th>R</th><th>reason</th></tr></thead>
              <tbody>
                {trades.map((t) => (
                  <tr key={t.id}>
                    <td>{t.book}</td><td>{coin(t.symbol)}</td><td>{t.side}</td><td>{fmtPx(t.exitPrice)}</td>
                    <td className={(t.pnl ?? 0) >= 0 ? "up" : "dn"}>{fmtUsd(t.pnl)}</td>
                    <td className={(t.r ?? 0) >= 0 ? "up" : "dn"}>{t.r == null ? "—" : `${t.r >= 0 ? "+" : ""}${t.r.toFixed(2)}R`}</td>
                    <td>{t.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
        <Panel title="JOURNAL" sub={`${state.health.journalLines} jsonl lines · sqlite is the record`} right="live">
          <ul className="log">
            {state.events.slice(0, 40).map((event) => (
              <li key={event.id}>
                <time>{hhmmss(event.t, "MYT")}</time>
                <div><b>{event.kind}</b>{event.text}</div>
              </li>
            ))}
          </ul>
        </Panel>
      </div>

      <Panel title="SCORECARD" sub="against the 2025–26 hold-out, not a new backtest">
        <div className="pnl-stats">
          <div><dt>trades</dt><dd>{state.scorecard.trades}</dd></div>
          <div><dt>win rate</dt><dd>{pct(state.scorecard.winRate)}</dd></div>
          <div><dt>expectancy</dt><dd>{state.scorecard.expectancy == null ? "—" : `${state.scorecard.expectancy >= 0 ? "+" : ""}${state.scorecard.expectancy.toFixed(2)}R`}</dd></div>
          <div><dt>max DD</dt><dd>{pct(state.scorecard.maxDrawdown)}</dd></div>
          <div><dt>opened</dt><dd>{state.scorecard.opened}</dd></div>
          <div><dt>timeouts</dt><dd>{state.scorecard.timeouts}</dd></div>
        </div>
        {state.scorecard.e6Note && <p className="note">{state.scorecard.e6Note}</p>}
        {state.scorecard.books.map((b) => (
          <p key={b.book} className="note"><b>{b.name}</b> {b.variant}. {b.plain} {b.hold}</p>
        ))}
      </Panel>

      <p className="foot">read only · no orders · no reviews · no kill switch · day {state.clock.day} of {state.clock.days}</p>
    </div>
  );
}

function Pos({ p }: { p: PositionRow }) {
  return (
    <tr>
      <td>{coin(p.symbol)}<div className="dim">{p.cluster}</div></td>
      <td className={p.side === "long" ? "up" : "dn"}>{p.side}</td>
      <td>{p.size}</td>
      <td>{fmtPx(p.entry)}</td>
      <td>{fmtPx(p.stop)}</td>
      <td>{fmtPx(p.mark)}</td>
      <td className={(p.r ?? 0) >= 0 ? "up" : "dn"}>{p.r == null ? "—" : p.r.toFixed(2)}</td>
      <td className={(p.unrealized ?? 0) >= 0 ? "up" : "dn"}>{fmtUsd(p.unrealized)}</td>
      <td>{fmtUsd(p.risk)}</td>
    </tr>
  );
}
