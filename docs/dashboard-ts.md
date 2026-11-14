# TypeScript desk dashboard

A second, public view of the paper desk. It does not replace the Python dashboard on `127.0.0.1:8788`. It does not place orders, attach reviews, edit config, or touch the kill file.

The page listens on `0.0.0.0:8099` (override with `HOST` and `PORT`). Every `/api` route and the SSE stream require a login. The token lives in an env file, default `/etc/chart-analyst/dashboard-ts.env`. It is never written into a response.

## Build

On the server, Node is v20 and npm is 10. There is no pnpm.

```bash
cd /opt/chart-analyst
sudo -u chartanalyst git pull
sudo mkdir -p /opt/chart-analyst/dashboard-ts
sudo rsync -a --delete --exclude node_modules --exclude web/dist dashboard-ts/ /opt/chart-analyst/dashboard-ts/
sudo chown -R chartanalyst:chartanalyst /opt/chart-analyst/dashboard-ts
cd /opt/chart-analyst/dashboard-ts
sudo -u chartanalyst npm ci
sudo -u chartanalyst npm run build
```

`npm ci` installs the server and the Vite app. `npm run build` writes `web/dist`. Express serves that directory. Node runs `tsx server/index.ts` directly. Nothing else on the box is edited: no nginx site, no reload of the paper unit.

## Token

```bash
sudo install -o root -g chartanalyst -m 640 /dev/null /etc/chart-analyst/dashboard-ts.env
echo 'DASHBOARD_TOKEN=choose-a-long-random-token' | sudo tee /etc/chart-analyst/dashboard-ts.env >/dev/null
sudo chown root:chartanalyst /etc/chart-analyst/dashboard-ts.env
sudo chmod 640 /etc/chart-analyst/dashboard-ts.env
```

The value is unquoted, at least 16 characters, and is not the webhook secret. The process reads only the `DASHBOARD_TOKEN` line. A missing or short token refuses to listen, so the port does not open without a login.

## Install the unit

```bash
sudo cp deploy/chart-analyst-dashboard-ts.service /etc/systemd/system/chart-analyst-dashboard-ts.service
sudo systemctl daemon-reload
sudo systemctl enable --now chart-analyst-dashboard-ts.service
sudo systemctl status chart-analyst-dashboard-ts.service
```

The unit runs as `chartanalyst`, `Nice=10`, `MemoryMax=512M`, `ProtectSystem=strict`, with `/var/lib/chart-analyst` and `/opt/chart-analyst` read-only. SQLite is opened `readonly` and `fileMustExist`. Restart is `on-failure`.

Open `http://<server>:8099/`, enter the token, and the desk loads. The cookie is HttpOnly and SameSite=Strict. Failed logins are limited to 8 per ten minutes per address. Sign out clears the cookie. There is no route that changes the desk.

Data is the same ledger the Python page reads: `desk.sqlite`, `journal.jsonl`, `refresh_status.json`, the kill file, `config/settings.yaml`, and `GET http://127.0.0.1:8787/health`. The stream is `GET /api/stream`. One snapshot is built about every 5 seconds while a browser is connected, and every open tab receives that same frame. `journal.jsonl` is read from the last offset, `settings.yaml` is re-read only when its mtime changes, and `/health` is fetched at most every 10 seconds with `Connection: close` after the body is read. Freshness is the latest bar for BTCUSDT and ETHUSDT, plus any open position, read through the `(symbol, interval, open_time)` primary key and cached for 60 seconds. The snapshot does not scan `klines`. The page marks the link down after 12 seconds of silence.
