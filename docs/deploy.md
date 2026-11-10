# Deploy the paper loop

The process on the server is the Chart Analyst paper engine. It reads public Binance USDT-M market data, writes an HMAC-signed webhook, records reviewer decisions, and simulates fills. It does not place orders, set leverage, or withdraw. `mode: live` exits on startup. Do not put a Binance API key on this host for this repository.

Two books are on. **E2** is the residual BTC-neutral momentum book (`e2_r15_b60_v30_top25`), rebalanced once a week. **E3** is the 50-day trend with a 3× ATR trail (`e3a_sma50_trail3_top40`), managed at each daily close. They share one paper ledger and the existing caps: 6% open risk, 3% inside a cluster, 3× notional on a name. Fills use the same taker fee (0.05%), 4 bp / 8 bp slippage, and archived funding as the strategy backtest.

E6 stays off. Its hold-out was 11 trades, which is not enough evidence. The E6 code and tests are still in the tree. E1, E4, and E5 stay off. The v0.3 setups (A–D) stay off in `config/settings.yaml`. The webhook, the journal, and the reviewer flow are the same ones the setups already used.

## Schedule

Binance daily bars are UTC. A bar opens at 00:00 UTC and closes at the next 00:00 UTC. Asia/Kuala_Lumpur is UTC+8 all year.

| | UTC | Asia/Kuala_Lumpur |
| --- | --- | --- |
| E2 rebalance | Monday 00:00, the close of Sunday's daily bar | Monday 08:00 |
| E2 decision deadline | Monday 01:00 | Monday 09:00 |
| E3 daily close | every day at 00:00 | every day at 08:00 |
| E3 entry deadline | every day at 01:00 | every day at 09:00 |

Sunday is weekday 6 on the bar's UTC open, which is the same clock the backtest uses. The packet is stamped at the close. `decision.timeout_seconds` is 3600, measured from that stamp, so the reviewer has until 01:00 UTC / 09:00 KL. A timeout is a skip. Editing `timeout_action` does not change that.

E2 sends **one** rebalance packet. It lists the target book and the diff against the book you hold now (opens, closes, increases, decreases, flips). **Open** applies that book at the next daily open, or at Sunday's close if that open is not in the store yet. **Skip**, or no decision, **keeps the current book**. If the process is down through the hour, it does not emit a stale rebalance when it comes back. The book stays as it was.

E3 sends **one packet per new entry** (or add). The 3× ATR trail, a stop-out, and any other cut to an existing E3 position fill without a packet. A hard gate still cannot be overridden: if you answer open while a gate is failing, the stored decision is skip.

Risk-reducing exits are not blocked by a missing review, a skip on some other packet, a stale 1h bar, a news veto, or a book that is already over the cap. The loop does have to be up to see the closed daily bar.

## Owner checklist

Do these on the server. The process still has no order path, so none of these steps install a key.

1. Pull this tree to `/opt/chart-analyst` and reinstall the editable package (`pip install -e .`). Leave `mode: paper`.
2. Confirm `config/settings.yaml`: `e2_residual_momentum` and `e3_slow_trend` are `enabled: true` with the variants above. `e6_liquidation_proxy`, `e1_cs_momentum`, `e4_funding_carry`, `e5_positioning_trend`, and all four v0.3 setups are `enabled: false`.
3. Do not add `BINANCE_API_KEY`, `BINANCE_SECRET_KEY`, or any other trading credential. `mode: live` must stay unset.
4. Load daily bars for the liquid USDT-M universe, not a two-name 1h cache. E2 needs about 60 daily closes and a book of names with $20m average quote volume. Public data only:

   ```bash
   cd /opt/chart-analyst
   sudo -u chartanalyst .venv/bin/python scripts/backfill.py --source vision --all-universe --start 2024-01 --end 2026-10 --workers 8
   sudo -u chartanalyst .venv/bin/python scripts/backfill_research.py
   ```

   `backfill_research.py` pulls funding, premium, and open interest from the public archive. If `fapi.binance.com` answers, a REST backfill is the same public market data. A missing open-interest print does not drop a name. A missing daily bar does.
5. Set `account.news_status: ALLOW` only if you accept that this host is not connected to News Watcher. Until you do, every new entry is rejected. Stops and other exits still run.
6. Fill `/etc/chart-analyst/secrets.env` with `WEBHOOK_URL`, `WEBHOOK_SECRET`, and `REVIEW_SECRET`. No exchange key goes in that file. Lock it to `root:chartanalyst` mode `640`.
7. Set `review.listen` to `127.0.0.1:8787` and restart the unit (`systemctl restart chart-analyst-paper.service`). The review port stays on loopback.
8. On Monday, be at the machine from 08:00 to 09:00 Asia/Kuala_Lumpur. Read the E2 packet (target book and diffs) and attach `open` or `skip` before 09:00. Silence keeps the book you already have.
9. Each day, from 08:00 to 09:00 Asia/Kuala_Lumpur, review the E3 entry packets the same way. You do not review the trail or a stop.
10. After the loop has been up for a week, read the scorecard (`scripts/run_paper.py --report`). It has a section per book and a combined section. If paper turnover, slippage, or return leaves the hold-out band (`paper.drift`), the journal gets one `drift` event. A drift alert is a gap versus the 2025–26 hold-out rates, not a new backtest.
11. The kill switch (`data/KILL`, or `KILL_SWITCH_PATH`) still flattens the paper book. It does not cancel an exchange order, because this process never sent one.

## What you install

Python 3.11 or newer, on a Linux host you control.

```bash
sudo useradd --system --create-home --home-dir /opt/chart-analyst --shell /usr/sbin/nologin chartanalyst
sudo mkdir -p /opt/chart-analyst /var/lib/chart-analyst /var/log/chart-analyst /etc/chart-analyst
sudo chown -R chartanalyst:chartanalyst /opt/chart-analyst /var/lib/chart-analyst /var/log/chart-analyst
```

Copy the repository to `/opt/chart-analyst` (git clone or rsync). As `chartanalyst`:

```bash
cd /opt/chart-analyst
python3 -m venv .venv
.venv/bin/pip install -e .
```

Point `data.sqlite_path` and `journal.jsonl_path` at `/var/lib/chart-analyst/` if you do not want the database inside the git tree. The committed `config/settings.yaml` uses `data/desk.sqlite` and `data/journal.jsonl`, which is fine when the working directory is `/opt/chart-analyst`. Both paths are gitignored.

Keep `mode: paper`.

## Environment file

Copy this to `/etc/chart-analyst/secrets.env` and fill the two secrets on the server. Do not commit that file. Do not add `BINANCE_API_KEY`, `BINANCE_SECRET_KEY`, or any trading credential. The same template is `config/secrets.env.example`.

```bash
# /etc/chart-analyst/secrets.env
# Public webhook only. No exchange API key belongs in this file.
WEBHOOK_URL=
WEBHOOK_SECRET=
REVIEW_SECRET=
# KILL_SWITCH_PATH=/var/lib/chart-analyst/KILL
```

`WEBHOOK_URL` overrides `webhook.url` in the YAML. `WEBHOOK_SECRET` is the HMAC key for outbound signal posts. `REVIEW_SECRET` is the HMAC key for `POST /signals/<id>/review`. If `REVIEW_SECRET` is empty, the review endpoint uses `WEBHOOK_SECRET`. If both are empty, the endpoint refuses the request and the outbox holds every packet instead of posting it.

Lock the file down:

```bash
sudo chown root:chartanalyst /etc/chart-analyst/secrets.env
sudo chmod 640 /etc/chart-analyst/secrets.env
```

systemd `EnvironmentFile=` does not expand shell quotes the way a login shell does. Leave values unquoted.

## systemd

```bash
sudo cp deploy/chart-analyst-paper.service /etc/systemd/system/chart-analyst-paper.service
sudo systemctl daemon-reload
sudo systemctl enable --now chart-analyst-paper.service
sudo systemctl status chart-analyst-paper.service
journalctl -u chart-analyst-paper -f
```

The unit runs `scripts/run_paper.py --loop --refresh --serve`. `--refresh` pulls public klines, funding, and the book. `--serve` listens only if `review.listen` is set, and it should be loopback:

```yaml
review:
  listen: "127.0.0.1:8787"
```

The unit restarts on failure. It does not open a trading port.

## Logs

Stdout goes to journald, which rotates itself:

```bash
sudo journalctl -u chart-analyst-paper --since today
sudo mkdir -p /var/log/journal
# journald's own limit is SystemMaxUse= in /etc/systemd/journald.conf
```

If you switch the unit to a file (`StandardOutput=append:/var/log/chart-analyst/paper.log`), install the logrotate snippet:

```bash
sudo cp deploy/chart-analyst.logrotate /etc/logrotate.d/chart-analyst
sudo logrotate --debug /etc/logrotate.d/chart-analyst
```

That snippet keeps 14 daily files, compresses them, and uses `copytruncate` so the process can keep the file open. Do not point logrotate at `data/journal.jsonl` or the SQLite file. Those are the decision record, not logs.

## Operating it

Backfill the daily universe before the first loop. The commands are in the checklist above. E2 and E3 read every stored daily name that passes the liquidity filter. A three-name cache cannot form the E2 book (it needs at least three names on each side). Public data only. A short liveness check, after that backfill, is:

```bash
cd /opt/chart-analyst
sudo -u chartanalyst .venv/bin/python scripts/backfill.py --symbols BTCUSDT ETHUSDT SOLUSDT --days 120 --daily-days 400
```

If the host gets HTTP 451 from `fapi.binance.com`, use `--source vision` as described in the README. Vision is still public market data.

One cycle, then the scorecard:

```bash
sudo -u chartanalyst .venv/bin/python scripts/run_paper.py --once --refresh --symbols BTCUSDT ETHUSDT
sudo -u chartanalyst .venv/bin/python scripts/run_paper.py --report
```

The scorecard is the 14-day Asia/Kuala_Lumpur clock plus trades, win rate, expectancy in R, max drawdown, rule-violation count, and how many reviews were opened versus skipped (including timeouts). It also has E2, E3, and the combined book: turnover, slippage, and return against the hold-out rates in `config/settings.yaml`. The clock starts the first time the paper process runs and is stored in the database. Restarting the service does not reset it. The books do not replay history on the first start; they go flat until the next closed daily bar.

Attach a decision from the server:

```bash
sudo -u chartanalyst .venv/bin/python scripts/attach_decision.py \
  --signal-id SIGNAL_ID \
  --decision skip \
  --reasoning "no room to the next level" \
  --summary "4h range still intact"
```

`--decision` is `open` or `skip`. `open` without reasoning is stored as skip. `open` while a hard gate is failing is stored as skip. That includes a book packet whose open risk, cluster, or per-name notional does not fit the shared caps. The reviewer's `market_context.onchain_checks` does not replace Setup D's scanner gate. A signal with no decision before `decision.timeout_seconds` (default 3600, counted from the signal bar's close) becomes skip, and an `open` that arrives after that window is stored as skip. Editing `timeout_action` does not change that. For an E2 rebalance, that skip leaves the current book in place. The paper loop has to be running inside that window too: a fill is not simulated for a signal older than the timeout, even if the data feed recovers later. A stop or another risk-reducing exit does not use this window.

The remote endpoint is `POST /signals/<signal_id>/review` with `Content-Type: application/json`, `X-Desk-Timestamp` (unix milliseconds), and `X-Desk-Signature` (hex HMAC-SHA256 of `{timestamp}.{raw_body}` using `REVIEW_SECRET`). A bad signature is rejected and not written.

Outbound posts use the same signature scheme. The `Idempotency-Key` header is the signal id and does not change across retries. Failed posts stay in the SQLite outbox (`webhook_outbox`) until they are delivered or dead-lettered. A missing URL or secret leaves the row pending and does not drop the body.

```bash
sudo -u chartanalyst .venv/bin/python scripts/drain_outbox.py
sudo -u chartanalyst .venv/bin/python scripts/export_journal.py \
  --csv /var/lib/chart-analyst/portfolio-journal.csv \
  --json /var/lib/chart-analyst/portfolio-journal.json
```

## Kill switch

```bash
echo "halt" | sudo tee /opt/chart-analyst/data/KILL
```

While that file exists, the loop does not simulate new entries. An open decision already in the queue is refused. Open paper positions are marked out on the next fresh closed bar. Removing the file allows later signals. The file does not cancel an exchange order, because this process never sent one.

The default path is `paper.kill_switch_path` (`data/KILL`). `KILL_SWITCH_PATH` in the environment overrides it.

## Fail closed

New entries are not simulated when any of these are true:

- the latest closed 1h bar is older than `paper.max_data_age_seconds` (default 90 minutes)
- paper equity or the daily or weekly P&L cannot be computed from the local ledger
- `account.news_status` is missing or is not `ALLOW`
- open risk, cluster risk, or the per-name notional cap would be exceeded across E2, E3, and any leftover setup position
- a packet gate is anything other than pass, including Setup D on-chain checks
- the kill-switch file exists
- the decision is missing, late, or has no reasoning on an open

A trail stop, a catastrophe stop, an E3 state exit, or the kill-switch flatten still runs. Those cuts do not wait for a review. A stale 1h bar blocks a new entry; it does not block a stop on a daily bar the store already has.

Equity is the paper ledger (starting capital plus simulated P&L). This build never reads account equity from Binance. There is no key to read it with. `account.news_status` in the committed file is null, so every open is refused until you set it. Set it to `ALLOW` only when you accept that this host is not connected to News Watcher. That is an assumption you are making, not a feed.

Setup D stays ineligible until an on-chain source fills the six scanner checks. Putting those checks in `market_context` does not make them pass.

## What this host must not do

- Do not set `mode: live`.
- Do not add an order client, a futures API key, or a withdrawal key.
- Do not turn E6, E1, E4, E5, or the v0.3 setups back on from a short paper sample. E6's hold-out was 11 trades. The v0.3 full-universe study did not show an out-of-sample edge.
- Do not treat the paper scorecard as a fresh out-of-sample test. The drift band compares the live book with the 2025–26 hold-out rates that were already used to switch E2 and E3 on.
