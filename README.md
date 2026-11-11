# Chart Analyst scanner and backtester

Deterministic scanner for Shaoxiang's desk, rules **v0.3** (10 Nov 2026, Asia/Kuala_Lumpur). It reads **Binance USDT-M public market data only**, writes a signal packet, and can replay the same functions on history.

This build does **not** place orders, set leverage, or withdraw. `mode: live` is refused on startup. There is no trading API key.

The working partial is **50% at 2R**. `exits.partial_at_r` can be set to `1.0` to compare the open question. That is a research switch, not a claim that either level is an edge.

## Layout

| Path | Role |
| --- | --- |
| `config/settings.yaml` | Thresholds, indicator settings, risk caps, webhook URL. No secrets. |
| `config/secrets.env.example` | `WEBHOOK_URL`, `WEBHOOK_SECRET`, `REVIEW_SECRET`. Copy it on the server; do not commit the copy. |
| `src/desk/data/` | Public REST client and SQLite cache (klines, funding, exchangeInfo, signals, outbox, journal). |
| `src/desk/indicators/` | Bollinger (20, 2), EMA 20/50/200, RSI 14, ATR 14 Wilder, volume SMA, bandwidth. |
| `src/desk/rules/` | Universe filters, BTC regime, setups A–D, sizing, exits, hard gates, on-chain checks. |
| `src/desk/scanner/` | Closed-bar evaluation and the v0.3 packet. It does not name a broker. |
| `src/desk/delivery/` | HMAC webhook, durable outbox, reviewer decision. Setup-agnostic. |
| `src/desk/journal/` | Append-only decision journal and the Portfolio Journal export. |
| `src/desk/paper/` | Simulated fills, kill switch, 14-day clock, scorecard. No order client. |
| `src/desk/backtest/` | Same evaluation path, costs, metrics, walk-forward split. |
| `docs/deploy.md` | systemd, env file, log rotation. |
| `docs/dashboard.md` | Read-only monitoring dashboard on `127.0.0.1:8788`. |
| `scripts/` | Backfill, scanner, backtest, paper, review, export, outbox drain. |

`src/desk/execution/` is absent on purpose. There is no order placement. A new setup becomes eligible by emitting a packet whose hard gates pass; the webhook, journal, and paper loop do not need a code change for that.

## Setup

Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Data

Public futures REST only (`https://fapi.binance.com`): `exchangeInfo`, `klines` (1h, 4h, 1d), 24h ticker, book ticker, premium index (predicted funding), and funding history. The client refuses any other path.

```bash
python3 scripts/backfill.py --symbols BTCUSDT ETHUSDT SOLUSDT --days 120 --daily-days 400
```

If `fapi.binance.com` returns HTTP 451 (restricted location), use the public monthly archive instead. It is the same kline and funding history, not an order API:

```bash
python3 scripts/backfill.py --source vision --symbols BTCUSDT ETHUSDT SOLUSDT --days 120 --daily-days 400
```

The full archive, including contracts Vision still hosts after they stop trading:

```bash
python3 scripts/backfill.py --source vision --all-universe --start 2019-09 --end 2026-10 --workers 12
```

That command lists every USD-M prefix on `data.binance.vision`, keeps names that end in `USDT` and have no delivery suffix, and downloads the monthly files in parallel. Completed months are recorded in SQLite and the zip bytes are cached under `data/vision-cache/`, so a second run resumes. After the daily files, a name is kept for 1h, 4h, and funding only when its best UTC day is at least half of the loosest quote-volume gate (`--min-daily-quote-volume`, default 500000). A 24h window sits inside two UTC days, so a name below that ceiling cannot pass the gate. The per-bar volume, spread, listing-age, and funding gates still apply during the backtest.

Vision does not include `exchangeInfo`. The listing time is the first archived daily open. A name whose last daily bar is more than three days before the archive's latest daily bar is stored as `DELISTED`; the others are stored as `TRADING`. That is an inference, not an exchange status. On a historical bar the product gate treats a stored USDT-M perpetual as listed, because the bar itself is the evidence it was trading then. `delisted_symbols_included` counts the non-`TRADING` rows that were actually loaded.

`--all-universe` against REST still needs `exchangeInfo` and only sees contracts listed at download time. REST klines update incrementally: a later run continues from the last stored open time and replaces that candle.

SQLite defaults to `data/desk.sqlite` (gitignored).

**Survivorship.** A REST cache is biased toward contracts that are still listed. The Vision `--all-universe` path keeps delisted names the archive still publishes, and the report counts how many of those were scanned. Volume, listing age, and the last **settled** funding rate are applied as of each bar. Historical books are not in klines; the backtest uses `backtest.assumed_spread_pct` and labels that assumption. The predicted funding rate is not archived, so the backtest reuses the last settled rate (`funding_predicted_model: persist_last_settled`) and says so. News vetoes are not modeled.

## Scanner

```bash
python3 scripts/run_scanner.py --symbols BTCUSDT ETHUSDT --refresh
```

On each run it evaluates the latest **closed** 1H bar. A 4H setup is remembered only while its 1H trigger window is still open (4, 8, or 12 hours depending on the setup). It does not act on a forming candle.

The packet is the scanner's own pass/fail. With the committed config, `account.news_status` is null, so the news gate **fails closed** and packets are `rejected`. For a local paper dry run, set `account.news_status: ALLOW` and keep `mode: paper`. That is an assumption, not a News Watcher connection. Missing equity, missing funding, or an unreadable blocklist also fail closed.

Webhook delivery stays off until it is enabled and both the URL and `WEBHOOK_SECRET` are set. The URL may be `webhook.url` or the `WEBHOOK_URL` environment variable. The secret is only the environment variable. A missing secret does not post and does not drop the packet: the body stays in `webhook_outbox` until a later `scripts/drain_outbox.py` or paper cycle can sign it.

```yaml
webhook:
  enabled: true
  url: "https://example.invalid/market-bot/webhook"  # or set WEBHOOK_URL
```

The signature is hex HMAC-SHA256 of `{X-Desk-Timestamp}.{raw body}`. `Idempotency-Key` is the signal id and is stable across retries. Backoff is stored on the outbox row. HTTP 4xx (other than 409 and 429) dead-letters the row and keeps the body. 409 counts as already delivered. Rejected packets are not posted unless `webhook.send_rejects` is true. The same symbol + setup + direction + 4H setup bar is not emitted twice. A hard-stop exit recorded in the store blocks that symbol for 7 days.

### Signal schema (v0.3 plus the Market Bot change)

Market Bot does **not** vote. `votes` contains only `scanner` (`pass` or `fail`). Two placeholders are left empty for later services:

* `market_context`: `{summary, strengtheners[], weakeners[], onchain_checks{}, sources[], ts}`
* `master_decision`: `{decision: open|skip|null, reasoning, ts}`

The scanner never fills those. `decision_path` is `pending_master` when every scanner gate passes, otherwise `hard_gate_reject`. `margin_mode` is always `isolated`. `tp2` is null.

A reviewer fills them later, by id:

```bash
python3 scripts/attach_decision.py --signal-id SIGNAL_ID --decision skip --reasoning "no room" --summary "range intact"
python3 scripts/serve_review.py --listen 127.0.0.1:8787
```

`POST /signals/<id>/review` requires the same HMAC scheme, using `REVIEW_SECRET` (or `WEBHOOK_SECRET` if that is unset). `open` is stored as `skip` when any hard gate is not pass, when reasoning is blank, when equity cannot be read, or when the signal was already rejected. `market_context.onchain_checks` is recorded and does not replace `hard_gates.on_chain`. A second decision for the same id is refused. If nobody decides before `decision.timeout_seconds` (default 3600, measured from the signal bar's close), the signal is skip. An `open` that arrives after that window is also skip. `timeout_action` cannot be set to open.

Setup D on-chain checks live on the scanner gate `hard_gates.on_chain.checks`, each `PASS`, `FAIL`, or `MISSING`:

* `top10_holders_le_25`
* `no_single_wallet_ge_10`
* `lp_locked_or_burned`
* `not_honeypot`
* `mint_disabled_or_renounced`
* `tax_le_10`

Any `FAIL` or `MISSING` fails the gate. This build has no on-chain source, so a scanner leaves them `MISSING` and does not take Setup D. `market_context.onchain_checks` stays `{}` so it is not confused with the scanner gate.

## Backtest

```bash
python3 scripts/run_backtest.py --symbols BTCUSDT ETHUSDT SOLUSDT --days 120
python3 scripts/run_backtest.py --symbols BTCUSDT ETHUSDT SOLUSDT --days 120 --partial-at-r 1 --allow-below-min-rr
```

The second command is the 1R comparison. `--allow-below-min-rr` turns off the v0.3 "at least 2R after costs" entry skip so the same setups can be held to a 1R partial. Default `partial_at_r` is 2, and the cost bump pushes TP1 out until the cost-adjusted reward/risk equals that multiple (fee both sides plus the spread). Without the bump, a target placed at exactly 2R raw always fails the cost check, because costs are strictly positive.

Costs in every trade: 0.05% taker fee per side, half the spread plus `costs.slippage_rate` against the trader on entries and market exits, and funding at each settlement while the position is open. A stop and a target in the same bar fill the stop. A stop that gaps through fills at the open. The hard stop is never widened. After the partial, the stop moves to breakeven when that tightens it, then the 4H trail (20 EMA and close, both ATR-buffered) may only tighten. Time stops and invalidations match the rules page, including Setup D's 16-bar hard max. Open trades are marked out at the last close (`end_of_data`).

The report, written to `reports/backtest.json`, has per-setup and overall trade count, win rate, average R, expectancy (mean R), max drawdown, and Sharpe (daily returns, sample standard deviation, √365). Walk-forward uses a 182-day in-sample window and a 91-day out-of-sample window with **frozen** parameters. If the sample is shorter than one full pair of windows, out-of-sample metrics are null and the report says so. Quote out-of-sample only after a sample actually covers a fold. Nothing here is a performance target.

The exit study compares the v0.3 4H trail with later arms, chandelier trails, and a 3-bar structure trail, at a 1R partial and a 2R partial. Setup B variants each change one filter (EMA separation, the caution regime, a 3-bar initial stop, or an EMA20 tag). The hard stop is never widened.

Strategy books E1–E6 live in `desk.strategies` and are not part of the 4h scanner. They read the same public archive (daily klines, funding, premium index, and the daily open-interest snapshot) and refuse `mode: live`. A book stays `enabled: false` unless its frozen hold-out, 2025-01 through 2026-10, finished above the start after fees, slippage, and funding.

```bash
python3 scripts/backfill_research.py --workers 16
python3 scripts/run_strategies.py
```

The report is `reports/strategies-YYYYMMDD.md`. Nothing in that path places an order.

Run the exit study after the archive backfill:

```bash
python3 scripts/run_research.py --workers 3
```

It writes `reports/backtest-YYYYMMDD.md` and matching CSVs. A variant becomes the default in `config/settings.yaml` only when it wins a strict majority of in-sample folds and its frozen out-of-sample record is positive, beats the baseline, and is not materially deeper in drawdown. Otherwise the v0.3 switches stay. The rule is in `desk.backtest.selection`. In-sample luck is not an edge.

## Rules coded here

* Universe: live USDT-M perpetual, 14-day listing age, 24h quote volume (5M for A–C, 1M for D), spread (0.15% / 0.50%), funding sanity at ±0.05% per 8h. Missing data fails closed.
* BTC daily regime uses the 50- and 200-day SMAs. A close equal to the 200-day is short-favored. A close equal to the 50-day while above the 200-day is caution. Range reclaim may take the unfavored side. Caution blocks new squeezes only.
* Setups A–D, long and short, 4H setup and 1H trigger, as written on the rules page. No extra chart patterns.
* Sizing is formula (1). Leverage is at most 3x, and 1x on Setup D. It does not change dollar risk. Liquidation uses the isolated approximation `entry * (1 ± 1/leverage ± maintenance margin)` and must sit at least one 4H ATR beyond the stop. Open risk 6%, cluster 3%, Setup D 2%. Notional caps 3x equity (1x on D). A cap that cuts risk below 0.5% of equity skips the trade.
* Cluster tags: base asset `BTC` → btc, `ETH` → eth, everything else → alts. `ETHFI` is an alt.

## Tests

`pytest` covers indicator values, setup conditions on synthetic bars, the worked sizing example (equity 800, entry 2.00, stop 1.90, size 160), liquidation and notional caps, trail stops that cannot widen, on-chain fail-closed behavior, the signal schema, webhook signatures and the outbox, reviewer decisions that cannot override a gate, the append-only journal export, paper fills using the backtest cost model, and the stale-data, equity, kill-switch, and timeout fail-closed paths. A passing test is not evidence of an edge.

## Paper loop

```bash
python3 scripts/run_paper.py --once --refresh --symbols BTCUSDT ETHUSDT
python3 scripts/run_paper.py --report
python3 scripts/export_journal.py --csv reports/portfolio-journal.csv --json reports/portfolio-journal.json
```

The loop evaluates closed bars, enqueues proposed packets, and simulates a fill only after an effective `open`. Fills use the backtester's taker fee, half-spread plus slippage, and funding. The hard stop is never widened. If slippage crosses the stop, the entry is cancelled. Open risk is checked again at fill time against the paper book. `data/KILL` (or `KILL_SWITCH_PATH`) blocks new entries. A stale closed 1h bar, or an equity figure the ledger cannot compute, blocks new entries. The scorecard reports the 14-day Asia/Kuala_Lumpur clock, trades, win rate, expectancy R, max drawdown, rule-violation count, and reviewer opens versus skips.

Paper equity is the local ledger, starting at `paper.initial_equity`. This process cannot read exchange equity. There is no account endpoint and no API key. `docs/deploy.md` is the server runbook.

The committed `account.news_status` is null, so an `open` is refused until you set `ALLOW` yourself. Setup D stays refused while its on-chain checks are `MISSING`.

## Monitoring dashboard

A separate read-only process serves the desk on `127.0.0.1:8788`. It does not place orders or attach reviews. From a checkout with the package installed:

```bash
python3 scripts/serve_dashboard.py
```

On the server, install `deploy/chart-analyst-dashboard.service` and open it through an SSH tunnel. The steps, the unit limits, and the optional authenticated bind are in `docs/dashboard.md`.

## What is still not in this repo

Do not point this process at a live account.

* **M6 — first live, only if Shaoxiang turns it on.** Trade-only key, withdrawals disabled, IP-restricted to the server, secret stored in the env file on that server and nowhere else. Risk scale 0.5. Go-live checks come from the journal, not from P&L. This repository should still not contain the key. A testnet check that an isolated order and its hard stop are accepted together is an order path, so it is not implemented here.

Also still open for a faithful backtest: historical predicted funding, historical books, News Watcher history, and an on-chain source for Setup D. The Vision path reduces survivorship bias only for contracts the archive still hosts. Lot size and tick size filters matter before any order path exists. The liquidation price here is the simplified isolated formula, not Binance's tiered cumulative maintenance table.
