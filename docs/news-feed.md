# News Watcher feed

The paper loop's news gate stays fail-closed while `account.news_status` is null. A News Watcher can post statuses to the review API. The gate does not read them until `account.news_feed_enabled` is set to `true`. That flag ships `false`.

The endpoint is `POST http://127.0.0.1:8787/news`. It accepts a connection only from loopback, and only when the review server is bound to loopback. The token is `NEWS_FEED_TOKEN` in `/etc/chart-analyst/secrets.env`. It is not in this file, not in `settings.yaml`, and it is not written to the journal or the response.

## Payload

One status, or up to 50 under `items`.

| Field | Required | Meaning |
| --- | --- | --- |
| `symbol` | no | USDT-M symbol such as `BTCUSDT`. Omit it, or send `*`, for the global status. |
| `status` | yes | `ALLOW`, `CAUTION`, or `VETO`. Anything except `ALLOW` blocks new entries. |
| `reason` | yes | Short text, at most 500 characters. Stored and shown on the gate. |
| `ts_ms` | yes | Unix milliseconds when News Watcher decided. Not more than 60 seconds in the future. |
| `ttl_seconds` | yes | Lifetime from `ts_ms`, from 1 second to 7 days. |

A symbol row wins over the global row while it is unexpired. A missing row, an expired row, and a feed that is turned off all fail closed: the gate behaves as it does today with `news_status: null`. `CAUTION` and `VETO` fail closed too. They do not open a trade.

```json
{
  "symbol": "BTCUSDT",
  "status": "VETO",
  "reason": "halt headline on the venue",
  "ts_ms": 1763683200000,
  "ttl_seconds": 3600
}
```

Global status:

```json
{
  "symbol": "*",
  "status": "ALLOW",
  "reason": "no blocking headline",
  "ts_ms": 1763683200000,
  "ttl_seconds": 900
}
```

## curl

Replace the token locally. Do not put it in the command history on a shared shell if that matters to you; the process never logs it.

```bash
curl --silent --show-error \
  --header "Authorization: Bearer ${NEWS_FEED_TOKEN}" \
  --header "Content-Type: application/json" \
  --data '{"symbol":"BTCUSDT","status":"VETO","reason":"halt headline on the venue","ts_ms":1763683200000,"ttl_seconds":3600}' \
  http://127.0.0.1:8787/news
```

`X-News-Token` is accepted in place of the bearer header. A wrong token is `401` and stores nothing. A missing token in the environment is `503`. A post from anything other than `127.0.0.1` is `403`.

## Turning it on

After a post looks right, set `account.news_feed_enabled: true` in `config/settings.yaml` and restart only `chart-analyst-paper.service`. Until that restart, posts are stored and the gate still ignores them.
