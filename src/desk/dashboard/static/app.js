"use strict";

const VIEWS = ["health", "books", "signals", "journal", "scorecard"];

function viewFromHash() {
  const name = (location.hash || "#health").replace("#", "");
  return VIEWS.indexOf(name) === -1 ? "health" : name;
}

const state = {
  view: viewFromHash(),
  snap: null,
  pause: false,
  timer: null,
  updated: null,
  error: null,
  filters: { book: "", symbol: "", event: "", q: "", review: "" },
};

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("nav").addEventListener("click", (event) => {
    const button = event.target.closest("[data-view]");
    if (!button) return;
    location.hash = button.dataset.view;
  });
  window.addEventListener("hashchange", () => {
    state.view = viewFromHash();
    syncNav();
    document.getElementById("main").dataset.view = "";
    render();
  });
  const main = document.getElementById("main");
  main.addEventListener("click", onMainClick);
  main.addEventListener("change", onFilter);
  main.addEventListener("input", onFilter);
  document.getElementById("pause").addEventListener("change", (event) => {
    state.pause = event.target.checked;
    if (state.pause) clearTimeout(state.timer);
    else schedule();
  });
  document.getElementById("token-save").addEventListener("click", () => {
    sessionStorage.setItem("deskToken", document.getElementById("token").value.trim());
    document.getElementById("auth").hidden = true;
    load();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(state.timer);
    else if (!state.pause) {
      load();
      schedule();
    }
  });
  setInterval(tickAge, 1000);
  syncNav();
  load();
});

function syncNav() {
  for (const item of document.querySelectorAll("#nav button")) {
    item.classList.toggle("active", item.dataset.view === state.view);
  }
}

function schedule() {
  clearTimeout(state.timer);
  if (state.pause) return;
  state.timer = setTimeout(async () => {
    await load();
    schedule();
  }, 15000);
}

async function load() {
  const headers = { Accept: "application/json" };
  const token = sessionStorage.getItem("deskToken");
  if (token) headers.Authorization = "Bearer " + token;
  try {
    const response = await fetch("/api/snapshot", { headers, cache: "no-store" });
    if (response.status === 401) {
      document.getElementById("auth").hidden = false;
      state.error = "This page needs the read token.";
      return;
    }
    const payload = await response.json();
    if (!response.ok) {
      state.error = (payload && payload.detail) || "The dashboard could not read the desk.";
      renderBanner();
      schedule();
      return;
    }
    state.snap = payload;
    state.error = null;
    state.updated = Date.now();
    tickAge();
    document.getElementById("auth").hidden = true;
    render();
    schedule();
  } catch (err) {
    state.error = "The dashboard request failed.";
    renderBanner();
    schedule();
  }
}

function tickAge() {
  const node = document.getElementById("age");
  if (!node) return;
  if (!state.updated) {
    node.textContent = state.error ? "not updated" : "waiting";
    return;
  }
  const seconds = Math.max(0, Math.round((Date.now() - state.updated) / 1000));
  node.textContent = "Updated " + seconds + "s ago";
}

function renderBanner() {
  const banner = document.getElementById("banner");
  if (!state.error) {
    banner.hidden = true;
    banner.textContent = "";
    return;
  }
  banner.hidden = false;
  banner.textContent = state.error;
}

function render() {
  renderBanner();
  renderHeader();
  const main = document.getElementById("main");
  if (main.dataset.view !== state.view) {
    main.dataset.view = state.view;
    main.innerHTML = shell(state.view);
  }
  const region = document.getElementById("region");
  if (region) region.innerHTML = regionHtml(state.view);
}

function renderHeader() {
  const snap = state.snap;
  const clock = document.getElementById("clock-line");
  const fill = document.getElementById("clock-fill");
  const pills = document.getElementById("pills");
  if (!snap) {
    clock.textContent = "Loading the paper clock…";
    return;
  }
  clock.textContent = snap.clock.plain;
  const fraction = Math.max(0, Math.min(1, Number(snap.clock.fraction || 0)));
  fill.style.width = (fraction * 100).toFixed(1) + "%";
  const service = snap.health.service || {};
  const news = snap.health.gates.news || {};
  pills.innerHTML = [
    pill(snap.health.mode || "paper", snap.health.paper ? "ok" : "bad"),
    pill("orders off", "ok"),
    pill("read-only", "ok"),
    pill(news.blocking ? "entries refused" : "news open", news.blocking ? "bad" : "ok"),
    pill(service.tone === "bad" ? "cycle late" : "cycle recent", service.tone || "warn"),
  ].join("");
}

function shell(view) {
  if (view === "journal") {
    return `<section class="card">${filterBar()}<div id="region"></div></section>`;
  }
  return `<div id="region"></div>`;
}

function regionHtml(view) {
  const snap = state.snap;
  if (!snap) return `<p class="empty">Waiting for the first read.</p>`;
  if (view === "books") return renderBooks(snap);
  if (view === "signals") return renderSignals(snap);
  if (view === "journal") return renderJournal(snap);
  if (view === "scorecard") return renderScorecard(snap);
  return renderHealth(snap);
}

function renderHealth(snap) {
  const health = snap.health;
  const gates = health.gates;
  const service = health.service;
  const refresh = health.refresh;
  const process = refresh.process;
  const totals = (process && process.totals) || {};
  const last = (process && process.last_slice) || {};
  return `
    <section class="card tone-${esc(service.tone || "warn")}" style="margin-bottom:12px">
      <div class="lead">${esc(service.headline)}</div>
      <p>${esc(service.detail)}</p>
      <p style="margin-top:8px">${esc(health.roster.plain)}</p>
    </section>
    <div class="grid gates">
      ${gateCard("News", gates.news)}
      ${gateCard("Kill switch", gates.kill)}
      ${gateCard("Stale data", gates.stale_data)}
      ${gateCard("Orders", { tone: health.paper ? "ok" : "bad", plain: health.orders_plain })}
    </div>
    <div class="grid two" style="margin-top:12px">
      <section class="card">
        <h2>Last cycle</h2>
        <p>${esc(health.cycle.plain)}</p>
        <p style="margin-top:8px">Last write ${esc(health.cycle.last_myt || "—")}.</p>
        <p style="margin-top:8px">${esc(gates.equity.plain)}</p>
      </section>
      <section class="card">
        <h2>Data freshness</h2>
        <div class="table-wrap"><table>
          <thead><tr><th>Bar</th><th>State</th><th>Latest</th><th>Names</th></tr></thead>
          <tbody>
            ${health.freshness.map((row) => `<tr>
              <td>${esc(row.interval)}</td>
              <td><span class="chip ${row.fresh ? "ok" : "bad"}">${row.fresh ? "fresh" : "stale"}</span></td>
              <td>${esc(row.latest_close_myt || "none")}<div class="muted">${esc(row.plain)}</div></td>
              <td>${esc(row.symbols)}</td>
            </tr>`).join("")}
          </tbody>
        </table></div>
      </section>
    </div>
    <section class="card" style="margin-top:12px">
      <h2>Refresh</h2>
      <p>${esc(refresh.plain)}</p>
      <div class="grid stats" style="margin-top:12px">
        ${stat("Vision files", num(totals.vision_files, 0))}
        ${stat("fapi requests", num(totals.fapi_requests, 0))}
        ${stat("Pauses", num(totals.pauses, 0))}
        ${stat("HTTP 429", num(totals.http_429, 0))}
        ${stat("HTTP 418", num(totals.http_418, 0))}
      </div>
      <p style="margin-top:10px">Last slice: vision ${esc(last.vision_files ?? "—")}, fapi ${esc(last.fapi_requests ?? "—")}, pending names ${esc(last.pending ?? "—")}, errors ${esc(last.errors ?? "—")}.</p>
      <p style="margin-top:6px">Archive days in sqlite: ${esc(refresh.archive.days)} ${refresh.archive.latest_day ? "(latest " + esc(refresh.archive.latest_day) + ")" : ""}. Weight budget ${esc(refresh.weight_per_minute)} / min, pause above ${esc(refresh.pause_above)}.</p>
    </section>
    <section class="card" style="margin-top:12px">
      <h2>Decision windows</h2>
      ${snap.windows.map((row) => `<p style="margin-top:8px"><span class="chip ${row.state === "open" ? "open" : "pending"}">${esc(row.state)}</span> <strong>${esc(row.name)}</strong> ${esc(row.plain)}</p>`).join("")}
    </section>`;
}

function renderBooks(snap) {
  const books = snap.books;
  const series = [
    { name: "Desk", color: "#7aa2f7", points: books.desk_curve || [] },
  ];
  if (books.btc && books.btc.available) {
    series.push({ name: "BTC hold", color: "#e6b450", points: books.btc.curve || [] });
  }
  const compare = books.vs_btc
    ? `Desk ${pct(books.vs_btc.desk_return)} versus BTC ${pct(books.vs_btc.btc_return)} since the clock start (${pct(books.vs_btc.difference)} apart).`
    : (books.btc && books.btc.plain) || "";
  const cards = Object.values(books.by_book || {}).map(renderBookCard).join("");
  const setups = (books.positions || []).filter((row) => row.kind === "setup");
  const setupCard = setups.length ? renderPositionTable("Setup positions", setups) : "";
  return `
    <section class="card">
      <h2>Desk equity and BTC buy-and-hold</h2>
      <div class="chart">${lineChart(series)}</div>
      <p style="margin-top:8px">${esc(compare)}</p>
      <p style="margin-top:6px">${esc(books.plain)}</p>
    </section>
    <div class="grid stats" style="margin-top:12px">
      ${stat("Equity", money(books.equity))}
      ${stat("Return", pct(books.desk_return), books.desk_return)}
      ${stat("Max drawdown", pct(books.drawdown))}
      ${stat("Gross leverage", multiple(books.gross_leverage))}
      ${stat("Open risk", books.open_risk.cap_usdt == null ? "—" : money(books.open_risk.usdt))}
    </div>
    <div class="grid two" style="margin-top:12px">
      <section class="card">
        <h2>Open risk vs 6%</h2>
        ${meter(books.open_risk.fraction_of_cap, `${money(books.open_risk.usdt)} of ${money(books.open_risk.cap_usdt)} cap`)}
        <h3 style="margin-top:14px">Clusters vs 3%</h3>
        ${(books.clusters || []).map((row) => meter(row.fraction_of_cap, `${esc(row.cluster)} ${money(row.risk_usdt)} / ${money(row.cap_usdt)}`)).join("") || `<p class="empty">No open risk.</p>`}
        <p style="margin-top:8px">Book clusters sum both directions. A setup key keeps the side in the name.</p>
      </section>
      <section class="card">
        <h2>Name notional vs 3×</h2>
        <p>Gross leverage is mark notional divided by equity. The 3× cap is per name, not on the sum.</p>
        ${(books.names || []).map((row) => meter(row.fraction_of_cap, `${esc(row.symbol)} ${money(row.notional)} / ${money(row.cap_usdt)}`)).join("") || `<p class="empty">No open names.</p>`}
      </section>
    </div>
    <div class="grid books" style="margin-top:12px">${cards}</div>
    ${setupCard}`;
}

function renderBookCard(book) {
  const series = [{ name: book.name, color: book.book === "e3" ? "#3dcc8a" : "#7aa2f7", points: book.curve || [] }];
  return `<section class="card">
    <h2>${esc(book.name)} <span class="muted">${esc(book.variant)}</span></h2>
    <p>${esc(book.cadence)} · ${esc(book.trades)} closed · leverage ${esc(multiple(book.gross_leverage))}</p>
    <div class="grid stats" style="margin:10px 0">
      ${stat("PnL", money(book.pnl), book.pnl)}
      ${stat("Return", pct(book.return), book.return)}
      ${stat("Drawdown", pct(book.drawdown))}
    </div>
    <div class="chart">${lineChart(series, 140)}</div>
    ${positionTable(book.positions || [])}
  </section>`;
}

function renderPositionTable(title, rows) {
  return `<section class="card" style="margin-top:12px"><h2>${esc(title)}</h2>${positionTable(rows)}</section>`;
}

function positionTable(rows) {
  if (!rows.length) return `<p class="empty">Flat. The book stays flat until the next reviewed entry.</p>`;
  return `<div class="table-wrap"><table>
    <thead><tr><th>Name</th><th>Side</th><th>Size</th><th>Entry</th><th>Stop</th><th>Mark</th><th>R</th><th>Unrealized</th><th>Risk</th></tr></thead>
    <tbody>
      ${rows.map((row) => `<tr>
        <td>${esc(row.symbol)}<div class="muted">${esc(row.cluster)}</div></td>
        <td class="${row.side === "long" ? "up" : "down"}">${esc(row.side)}</td>
        <td>${esc(qty(row.size))}</td>
        <td>${esc(price(row.entry))}</td>
        <td>${esc(price(row.stop))}</td>
        <td>${esc(price(row.mark))}</td>
        <td class="${pnlClass(row.r)}">${esc(rmul(row.r))}</td>
        <td class="${pnlClass(row.unrealized_pnl)}">${money(row.unrealized_pnl)}</td>
        <td>${money(row.risk_usdt)}</td>
      </tr>`).join("")}
    </tbody>
  </table></div>`;
}

function renderSignals(snap) {
  const counts = (snap.signal_meta && snap.signal_meta.counts) || {};
  const order = ["pending", "open", "skip", "timeout", "rejected"];
  const chips = [`<button type="button" class="chip ${state.filters.review ? "" : "ok"}" data-review="">All ${esc(snap.signal_meta.returned)}</button>`]
    .concat(order.filter((name) => counts[name]).map((name) =>
      `<button type="button" class="chip ${name} ${state.filters.review === name ? "ok" : ""}" data-review="${esc(name)}">${esc(name)} ${esc(counts[name])}</button>`
    )).join("");
  const rows = (snap.signals || []).filter((row) => !state.filters.review || row.review_status === state.filters.review);
  const note = snap.signal_meta.truncated ? `<p class="muted">Showing the latest ${esc(snap.signal_meta.returned)} of ${esc(snap.signal_meta.total)}.</p>` : "";
  return `<section class="card">
    <h2>Signals</h2>
    <div class="row-gap" style="margin-bottom:8px">${chips}</div>
    <p>${esc(snap.outbox.plain)}</p>
    ${note}
    ${rows.map(renderSignal).join("") || `<p class="empty">No signals in this filter.</p>`}
  </section>
  ${outboxTable(snap.outbox)}`;
}

function renderSignal(row) {
  const diffs = row.diffs || {};
  const diffText = ["open", "close", "increase", "decrease", "flip"].filter((name) => diffs[name]).map((name) => `${name} ${diffs[name]}`).join(", ");
  const targets = (row.targets || []).map((leg) => `${leg.symbol} ${leg.side}`).join(", ");
  const failures = (row.gate_failures || []).map((gate) => gate.name).join(", ");
  return `<article class="signal">
    <header>
      <div><strong>${esc(row.book || row.setup || "")}</strong> ${esc(row.symbol || "")} ${esc(row.direction || "")} <span class="muted">${esc(row.packet_kind || "")}</span></div>
      <span class="chip ${esc(row.review_status)}">${esc(row.review_status)}</span>
    </header>
    <div class="id" title="${esc(row.signal_id)}">${esc(row.signal_id)}</div>
    <div>${esc(row.ts_myt || "")}${row.deadline_myt ? " · deadline " + esc(row.deadline_myt) : ""}</div>
    <div>${esc(row.hint)}</div>
    <div>Decision ${esc(row.effective || row.master_decision || "none")}${row.requested ? " (asked " + esc(row.requested) + ")" : ""}${row.reason ? " · " + esc(row.reason) : ""}</div>
    ${row.reasoning ? `<div>${esc(row.reasoning)}</div>` : ""}
    ${diffText ? `<div class="muted">Diff ${esc(diffText)}</div>` : ""}
    ${targets ? `<div class="muted">Targets ${esc(targets)}</div>` : ""}
    ${failures ? `<div class="down">Gates ${esc(failures)}</div>` : ""}
    <div class="muted">Outbox ${esc(row.outbox_status)}${row.fill ? " · fill " + esc(price(row.fill.price)) + " size " + esc(qty(row.fill.size)) : ""}${row.resolution ? " · " + esc(row.resolution) : ""}</div>
  </article>`;
}

function outboxTable(outbox) {
  const rows = outbox.rows || [];
  if (!rows.length) return "";
  return `<section class="card" style="margin-top:12px"><h2>Outbox</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>Signal</th><th>Status</th><th>Tries</th><th>Updated</th><th>Last error</th></tr></thead>
      <tbody>${rows.map((row) => `<tr>
        <td class="id">${esc(row.signal_id)}</td>
        <td><span class="chip ${esc(row.status)}">${esc(row.status)}</span></td>
        <td>${esc(row.attempts)}</td>
        <td>${esc(row.updated_myt || "")}</td>
        <td>${esc(row.last_error || "")}</td>
      </tr>`).join("")}</tbody>
    </table></div></section>`;
}

function filterBar() {
  const meta = (state.snap && state.snap.journal_meta) || { events: [], books: [], symbols: [] };
  return `<h2>Journal and trades</h2>
    <div class="filters">
      ${select("f-book", "book", "All books", meta.books)}
      ${select("f-symbol", "symbol", "All symbols", meta.symbols)}
      ${select("f-event", "event", "All events", meta.events)}
      <input id="f-q" data-filter="q" placeholder="Search the journal" value="${esc(state.filters.q)}">
    </div>`;
}

function select(id, key, label, options) {
  const current = state.filters[key] || "";
  const opts = [`<option value="">${esc(label)}</option>`].concat((options || []).map((value) =>
    `<option value="${esc(value)}"${value === current ? " selected" : ""}>${esc(value)}</option>`
  ));
  return `<select id="${id}" data-filter="${key}">${opts.join("")}</select>`;
}

function renderJournal(snap) {
  const trades = (snap.trades || []).filter(matchesTrade);
  const rows = (snap.journal || []).filter(matchesJournal);
  return `
    <h3>Trades</h3>
    ${trades.length ? `<div class="table-wrap"><table>
      <thead><tr><th>When</th><th>Book</th><th>Name</th><th>Side</th><th>Exit</th><th>PnL</th><th>R</th><th>Reason</th></tr></thead>
      <tbody>${trades.map((row) => `<tr>
        <td>${esc(row.exit_myt || "")}</td>
        <td>${esc(row.book || "")}</td>
        <td>${esc(row.symbol || "")}</td>
        <td>${esc(row.direction || "")}</td>
        <td>${esc(price(row.exit_price))}</td>
        <td class="${pnlClass(row.pnl)}">${money(row.pnl)}</td>
        <td class="${pnlClass(row.r_multiple)}">${esc(rmul(row.r_multiple))}</td>
        <td>${esc(row.exit_reason || "")}</td>
      </tr>`).join("")}</tbody>
    </table></div>` : `<p class="empty">No trades match these filters.</p>`}
    <h3 style="margin-top:16px">Decision journal</h3>
    <p class="muted">${snap.journal_meta.truncated ? "Latest " + esc(snap.journal_meta.returned) + " of " + esc(snap.journal_meta.total) + ". " : ""}${esc(rows.length)} rows in this filter. Source is the sqlite journal.</p>
    ${rows.length ? `<div class="table-wrap"><table>
      <thead><tr><th>When</th><th>Event</th><th>Book</th><th>Name</th><th>What happened</th></tr></thead>
      <tbody>${rows.map((row) => `<tr>
        <td>${esc(row.ts_myt || "")}</td>
        <td>${esc(row.event)}</td>
        <td>${esc(row.book || "")}</td>
        <td>${esc(row.symbol || "")}</td>
        <td>${esc(row.summary)}<div class="id">${esc(row.signal_id || "")}</div></td>
      </tr>`).join("")}</tbody>
    </table></div>` : `<p class="empty">No journal rows match these filters.</p>`}`;
}

function matchesTrade(row) {
  if (state.filters.book && String(row.book || "").toLowerCase() !== state.filters.book.toLowerCase()) return false;
  if (state.filters.symbol && String(row.symbol || "").toUpperCase() !== state.filters.symbol.toUpperCase()) return false;
  if (state.filters.event && state.filters.event !== "exit") return false;
  if (state.filters.q) {
    const blob = `${row.symbol} ${row.book} ${row.exit_reason} ${row.signal_id}`.toLowerCase();
    if (!blob.includes(state.filters.q.toLowerCase())) return false;
  }
  return true;
}

function matchesJournal(row) {
  if (state.filters.book && String(row.book || "").toLowerCase() !== state.filters.book.toLowerCase()) return false;
  if (state.filters.symbol && String(row.symbol || "").toUpperCase() !== state.filters.symbol.toUpperCase()) return false;
  if (state.filters.event && String(row.event || "").toLowerCase() !== state.filters.event.toLowerCase()) return false;
  if (state.filters.q) {
    const blob = `${row.summary} ${row.signal_id} ${row.symbol} ${row.book} ${row.event}`.toLowerCase();
    if (!blob.includes(state.filters.q.toLowerCase())) return false;
  }
  return true;
}

function renderScorecard(snap) {
  const card = snap.scorecard;
  const expectations = snap.expectations || {};
  if (!card) return `<section class="card"><h2>Scorecard</h2><p>The scorecard could not be read from the ledger.</p></section>`;
  const reviewer = card.reviewer || {};
  const books = Object.values(expectations.books || {});
  return `
    <div class="grid stats">
      ${stat("Trades", num(card.trades, 0))}
      ${stat("Win rate", card.win_rate == null ? "—" : pct(card.win_rate))}
      ${stat("Expectancy", card.expectancy_r == null ? "—" : rmul(card.expectancy_r))}
      ${stat("Max drawdown", pct(card.max_drawdown))}
      ${stat("Violations", num(card.rule_violations, 0))}
    </div>
    <section class="card" style="margin-top:12px">
      <h2>Reviews</h2>
      <p>Opens are effective opens. A timeout is a skip. An open that a gate refused is a forced skip.</p>
      <div class="grid stats" style="margin-top:10px">
        ${stat("Opened", num(reviewer.opened, 0))}
        ${stat("Skipped", num(reviewer.skipped, 0))}
        ${stat("Timeouts", num(reviewer.timeout_skip, 0))}
        ${stat("Forced skips", num(reviewer.forced_skip, 0))}
        ${stat("Safety blocks", num(card.safety_blocks, 0))}
      </div>
    </section>
    <section class="card" style="margin-top:12px">
      <h2>Against the hold-out</h2>
      <p>${esc(expectations.report_note || "")}</p>
      ${expectations.e6_note ? `<p style="margin-top:8px">${esc(expectations.e6_note)}</p>` : ""}
      ${books.map(renderExpectation).join("") || `<p class="empty">No live book to compare.</p>`}
    </section>`;
}

function renderExpectation(book) {
  const drift = book.drift || {};
  const hold = book.holdout || {};
  const actual = book.actual || {};
  return `<div style="margin-top:14px">
    <h3>${esc(book.name)} <span class="muted">${esc(book.variant || "")}</span></h3>
    <p>${esc(book.plain)}</p>
    <div class="table-wrap" style="margin-top:8px"><table>
      <thead><tr><th>Metric</th><th>Paper</th><th>Hold-out pace</th><th>Gap</th></tr></thead>
      <tbody>
        <tr><td>Turnover</td><td>${num(drift.actual_turnover, 3)}</td><td>${num(drift.expected_turnover, 3)}</td><td>${gap(drift.turnover_gap)}</td></tr>
        <tr><td>Slippage / equity</td><td>${pct(drift.actual_slippage_frac)}</td><td>${pct(drift.expected_slippage_frac)}</td><td>${gap(drift.slippage_gap)}</td></tr>
        <tr><td>Return</td><td>${pct(drift.actual_return)}</td><td>${pct(drift.expected_return)}</td><td>${gap(drift.return_gap)}</td></tr>
      </tbody>
    </table></div>
    <p style="margin-top:8px">Full hold-out, not the 14-day pace: CAGR ${pct(hold.cagr)}, Sharpe ${num(hold.sharpe, 2)}, max DD ${pct(hold.max_dd)}, expectancy ${hold.expectancy_r == null ? "—" : rmul(hold.expectancy_r)}, ${hold.trades == null ? "—" : esc(hold.trades)} trades. Paper book PnL ${money(actual.net_pnl)}.</p>
  </div>`;
}

function onMainClick(event) {
  const review = event.target.closest("[data-review]");
  if (!review) return;
  state.filters.review = review.dataset.review || "";
  const region = document.getElementById("region");
  if (region) region.innerHTML = regionHtml("signals");
}

function onFilter(event) {
  const node = event.target.closest("[data-filter]");
  if (!node) return;
  state.filters[node.dataset.filter] = node.value;
  const region = document.getElementById("region");
  if (region && state.view === "journal") region.innerHTML = regionHtml("journal");
}

function gateCard(title, gate) {
  const tone = (gate && gate.tone) || "warn";
  return `<section class="card tone-${esc(tone)}"><h2>${esc(title)}</h2><p>${esc((gate && gate.plain) || "")}</p></section>`;
}

function stat(label, value, signed) {
  const klass = signed == null || signed === "" ? "" : pnlClass(signed);
  return `<div class="card stat"><div class="k">${esc(label)}</div><div class="v ${klass}">${value}</div></div>`;
}

function meter(fraction, label) {
  const pctValue = fraction == null || Number.isNaN(Number(fraction)) ? 0 : Math.max(0, Number(fraction) * 100);
  const width = Math.min(100, pctValue);
  const tone = pctValue >= 100 ? "bad" : pctValue >= 80 ? "warn" : "ok";
  return `<div class="meter-label"><span>${label}</span><strong>${fraction == null ? "—" : pctValue.toFixed(0) + "%"}</strong></div>
    <div class="meter ${tone}"><span style="width:${width.toFixed(1)}%"></span></div>`;
}

function pill(text, tone) {
  return `<span class="pill ${esc(tone || "")}">${esc(text)}</span>`;
}

function lineChart(series, height) {
  const points = series.flatMap((item) => item.points || []);
  if (!points.length) return `<p class="empty">No marks yet.</p>`;
  const xs = points.map((point) => Number(point.ms));
  const ys = points.map((point) => Number(point.equity));
  let minX = Math.min(...xs);
  let maxX = Math.max(...xs);
  let minY = Math.min(...ys);
  let maxY = Math.max(...ys);
  if (minX === maxX) maxX = minX + 1;
  if (minY === maxY) {
    minY -= 1;
    maxY += 1;
  }
  const pad = (maxY - minY) * 0.1;
  minY -= pad;
  maxY += pad;
  const w = 640;
  const h = height || 220;
  const left = 52;
  const right = 12;
  const top = 12;
  const bottom = 22;
  const xOf = (ms) => left + ((ms - minX) / (maxX - minX)) * (w - left - right);
  const yOf = (value) => top + ((maxY - value) / (maxY - minY)) * (h - top - bottom);
  const lines = series.map((item) => {
    const coords = (item.points || []).map((point) => `${xOf(Number(point.ms)).toFixed(1)},${yOf(Number(point.equity)).toFixed(1)}`).join(" ");
    return `<polyline fill="none" stroke="${item.color}" stroke-width="2" points="${coords}"></polyline>`;
  }).join("");
  const legend = series.map((item) => `<span><i style="background:${item.color}"></i>${esc(item.name)}</span>`).join("");
  return `<svg viewBox="0 0 ${w} ${h}" role="img">
    <line x1="${left}" y1="${yOf(maxY)}" x2="${w - right}" y2="${yOf(maxY)}" stroke="#2c3646"></line>
    <line x1="${left}" y1="${yOf(minY)}" x2="${w - right}" y2="${yOf(minY)}" stroke="#2c3646"></line>
    <text x="${left - 6}" y="${yOf(maxY) + 4}" text-anchor="end">${esc(compact(maxY))}</text>
    <text x="${left - 6}" y="${yOf(minY)}" text-anchor="end">${esc(compact(minY))}</text>
    ${lines}
  </svg><div class="legend">${legend}</div>`;
}

function compact(value) {
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function money(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const num = Number(value);
  const abs = Math.abs(num).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (num < 0 ? "−" : "") + "$" + abs;
}

function gap(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const num = Number(value);
  if (num >= 1) return num.toFixed(1) + "×";
  return pct(num);
}

function pct(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return (Number(value) * 100).toFixed(2) + "%";
}

function rmul(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const num = Number(value);
  return (num > 0 ? "+" : "") + num.toFixed(2) + "R";
}

function num(value, digits) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function multiple(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toFixed(2) + "×";
}

function qty(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const num = Math.abs(Number(value));
  if (num >= 100) return num.toFixed(2);
  if (num >= 1) return num.toFixed(3);
  return num.toFixed(4);
}

function price(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const num = Number(value);
  if (Math.abs(num) >= 1000) return num.toLocaleString(undefined, { maximumFractionDigits: 1 });
  if (Math.abs(num) >= 1) return num.toFixed(2);
  return num.toPrecision(4);
}

function pnlClass(value) {
  if (value == null || Number(value) === 0 || Number.isNaN(Number(value))) return "";
  return Number(value) > 0 ? "up" : "down";
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}
