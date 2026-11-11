# Monitoring dashboard

Read-only view of the paper desk. It runs as its own process on `127.0.0.1:8788`. It does not place orders, attach a review, edit `config/settings.yaml`, set `account.news_status`, or create or remove the kill-switch file.

The paper engine stays `chart-analyst-paper.service` (`scripts/run_paper.py --loop --refresh --serve` on `127.0.0.1:8787`). This page only reads.

## What it shows

* Service, mode (`paper`), and orders off. The last cycle time and block reason come from `paper_account`.
* Hard gates in plain language: `account.news_status` (empty means entries are refused), the kill-switch file, stale daily bars, and whether the ledger can be marked.
* Data freshness for 1h, 4h, and 1d. E2 and E3 decide on the daily close. A missing 4h bar does not block them while setups A–D are off.
* Refresh: vision and fapi counts, pauses, HTTP 429 and 418, the 300 weight/minute budget, and the pause above used-weight 1200. Archive day counts come from `vision_days`. The live counters come from `refresh_status.json`, which the paper loop writes beside the sqlite file when it is started with `--refresh`. That file has no secrets. If the observer cannot write it, the refresh continues.
* Paper clock: day X of 14, the start time, and the next E2 and E3 decision windows in Asia/Kuala_Lumpur. E2 is Monday 08:00–09:00. E3 is every day 08:00–09:00. Silence on E2 keeps the current book. A timeout is a skip.
* E2 (`e2_r15_b60_v30_top25`) and E3 (`e3a_sma50_trail3_top40`): equity, PnL, drawdown, open positions (side, size, entry, stop, R, unrealized PnL), gross leverage, open risk against the 6% cap, cluster risk against 3%, and name notional against 3×. BTC buy-and-hold is marked from the clock start.
* Recent signal packets, review status (`pending`, `open`, `skip`, `timeout`), the stored decision and reasoning, fills, and outbox status. Webhook bodies are not rendered.
* Journal and closed trades, with filters. The scorecard uses the same function as `scripts/run_paper.py --report` and sets it next to the hold-out rates in `reports/strategies-*.md` and `config/settings.yaml`. Drift alerts start after 7 days. This page does not write the journal.

The page refreshes itself every 15 seconds. "Pause page refresh" only stops that reload. It does not pause the paper loop.

## Run it locally

Python 3.11 or newer, from the repo root, after `pip install -e .`:

```bash
python3 scripts/serve_dashboard.py
```

Open `http://127.0.0.1:8788/`. The default listen address is `127.0.0.1:8788`. The process reads `config/settings.yaml` and the sqlite path in `data.sqlite_path` (committed default `data/desk.sqlite`, relative to the working directory). It does not create the database. If the file is missing, the page says so.

```bash
python3 scripts/serve_dashboard.py --settings config/settings.yaml --listen 127.0.0.1:8788
```

Point `--secrets-env` at the server env file only if `KILL_SWITCH_PATH` is set there. The dashboard reads that one name so it watches the same file as the paper loop. Every other value in the file is a denylist: if it would have appeared in the JSON, it is replaced and not shown. Do not pass a token on the command line.

Tests:

```bash
pytest tests/test_dashboard.py
pytest
```

## Deploy on the server

The paper loop is already installed as in `docs/deploy.md`. The dashboard uses the same checkout, the same virtualenv, and the same user, so it can read `/var/lib/chart-analyst/desk.sqlite` (and the WAL index) without taking a write lock. SQLite is opened with `mode=ro` and `PRAGMA query_only`.

```bash
cd /opt/chart-analyst
sudo -u chartanalyst git pull
sudo -u chartanalyst .venv/bin/pip install -e .
sudo cp deploy/chart-analyst-dashboard.service /etc/systemd/system/chart-analyst-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now chart-analyst-dashboard.service
sudo systemctl status chart-analyst-dashboard.service
journalctl -u chart-analyst-dashboard -f
```

`WorkingDirectory` is `/opt/chart-analyst`, matching the paper unit, so a relative `data.sqlite_path` and `paper.kill_switch_path` resolve the same way. If those paths in `config/settings.yaml` are absolute (`/var/lib/chart-analyst/desk.sqlite`), that is what the dashboard opens.

The unit sets `Nice=10` and `MemoryMax=512M`. It does not load `/etc/chart-analyst/secrets.env`. It does not get `WEBHOOK_SECRET`, `REVIEW_SECRET`, or an exchange key. Restarting this unit does not restart the paper loop.

Confirm, on the server:

```bash
curl -sS http://127.0.0.1:8788/health
```

The body is `{"ok": true, "read_only": true, "orders": false}`.

## View it from your laptop

The process binds to loopback. The way to open it remotely is an SSH tunnel. SSH as yourself. The `chartanalyst` user has no login shell. The tunnel still reaches loopback on the server.

```bash
ssh -L 8788:127.0.0.1:8788 <you>@<server>
```

Leave that session open and browse `http://127.0.0.1:8788/` on the laptop. Traffic stays inside SSH. No token is required for this path, because the socket is not reachable off the machine except through the tunnel.

## Optional public bind

Leave this off. If you do turn it on, it requires a token and it is not the default.

Generate a token of at least 16 characters and keep it out of the repo and off the command line. A systemd drop-in:

```ini
# /etc/systemd/system/chart-analyst-dashboard.service.d/remote.conf
[Service]
Environment=DASHBOARD_TOKEN=<long random token>
ExecStart=
ExecStart=/opt/chart-analyst/.venv/bin/python scripts/serve_dashboard.py --listen 0.0.0.0:8788 --allow-remote
```

Then `sudo systemctl daemon-reload` and `sudo systemctl restart chart-analyst-dashboard.service`. The page asks for the token and sends it as `Authorization: Bearer`. Without the token, `/api/snapshot` returns 401. `--allow-remote` without `DASHBOARD_TOKEN` refuses to start. Binding `0.0.0.0` without `--allow-remote` refuses to start.

Prefer the SSH tunnel. A public bind exposes positions, signals, and the journal to anyone who has the token.

## What this process will not do

* No order client, no API key, no `mode: live`.
* No button that approves a signal, posts a webhook, edits config, sets `news_status`, or touches the kill file.
* No write to `desk.sqlite`. A `POST` is rejected with 405.
* Outbox bodies and environment values are not rendered. `refresh_status.json` stores counters only.

The paper loop's decisions are unchanged. The only addition on that process is the refresh counter file, written after a slice when `--refresh` is on. If that write fails, the slice result is unchanged.
