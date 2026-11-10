# Deploy the paper loop

The process on the server is the Chart Analyst paper engine. It reads public Binance USDT-M market data, writes an HMAC-signed webhook, records reviewer decisions, and simulates fills. It does not place orders, set leverage, or withdraw. `mode: live` exits on startup. Do not put a Binance API key on this host for this repository.

The full-universe backtest did not show an out-of-sample edge. The v0.3 setup defaults are unchanged. This deploy runs the plumbing around those setups. A later strategy change should be a new setup packet, not a change to the webhook, journal, or paper loop.

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

Backfill once before the first loop if the store is empty. Public data only:

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

The scorecard is the 14-day Asia/Kuala_Lumpur clock plus trades, win rate, expectancy in R, max drawdown, rule-violation count, and how many reviews were opened versus skipped (including timeouts). The clock starts the first time the paper process runs and is stored in the database. Restarting the service does not reset it.

Attach a decision from the server:

```bash
sudo -u chartanalyst .venv/bin/python scripts/attach_decision.py \
  --signal-id SIGNAL_ID \
  --decision skip \
  --reasoning "no room to the next level" \
  --summary "4h range still intact"
```

`--decision` is `open` or `skip`. `open` without reasoning is stored as skip. `open` while a hard gate is failing is stored as skip. The reviewer's `market_context.onchain_checks` does not replace Setup D's scanner gate. A signal with no decision before `decision.timeout_seconds` (default 3600, counted from the signal bar's close) becomes skip, and an `open` that arrives after that window is stored as skip. Editing `timeout_action` does not change that. The paper loop has to be running inside that window too: a fill is not simulated for a signal older than the timeout, even if the data feed recovers later.

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
- open risk, cluster risk, or the Setup D cap would be exceeded
- a packet gate is anything other than pass, including Setup D on-chain checks
- the kill-switch file exists
- the decision is missing, late, or has no reasoning on an open

Equity is the paper ledger (starting capital plus simulated P&L). This build never reads account equity from Binance. There is no key to read it with. `account.news_status` in the committed file is null, so every open is refused until you set it. Set it to `ALLOW` only when you accept that this host is not connected to News Watcher. That is an assumption you are making, not a feed.

Setup D stays ineligible until an on-chain source fills the six scanner checks. Putting those checks in `market_context` does not make them pass.

## What this host must not do

- Do not set `mode: live`.
- Do not add an order client, a futures API key, or a withdrawal key.
- Do not treat the paper scorecard as evidence of an edge. The backtest did not find one.
