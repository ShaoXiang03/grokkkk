import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { createApp, type AppOptions } from "./index.js";
import { COOKIE } from "./auth.js";

const TOKEN = "0123456789abcdef";

function options(dir: string): AppOptions {
  const settings = path.join(dir, "settings.yaml");
  writeFileSync(settings, "mode: paper\naccount: { news_status: null }\n");
  return {
    token: TOKEN,
    sqlitePath: path.join(dir, "missing.sqlite"),
    settingsPath: settings,
    reviewUrl: "",
  };
}

async function listen(app: ReturnType<typeof createApp>): Promise<{ url: string; close: () => Promise<void> }> {
  const server = createServer(app);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("no port");
  return {
    url: `http://127.0.0.1:${address.port}`,
    close: () => new Promise((resolve) => server.close(() => resolve())),
  };
}

test("login sets an HttpOnly cookie and every api route requires it", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "desk-auth-"));
  const app = createApp(options(dir));
  const server = await listen(app);
  try {
    const denied = await fetch(`${server.url}/api/state`);
    assert.equal(denied.status, 401);
    const bad = await fetch(`${server.url}/login`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token: "wrong-token-value" }),
    });
    assert.equal(bad.status, 401);
    const body = await bad.text();
    assert.equal(body.includes(TOKEN), false);
    const ok = await fetch(`${server.url}/login`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token: TOKEN }),
    });
    assert.equal(ok.status, 200);
    const cookie = ok.headers.get("set-cookie") ?? "";
    assert.match(cookie, /HttpOnly/i);
    assert.match(cookie, /SameSite=Strict/i);
    assert.equal(cookie.includes(TOKEN), false);
    const raw = cookie.split(";")[0];
    const authed = await fetch(`${server.url}/api/state`, { headers: { cookie: raw } });
    assert.equal(authed.status, 200);
    const snap = await authed.json() as { orders: boolean; readOnly: boolean; error: string };
    assert.equal(snap.orders, false);
    assert.equal(snap.readOnly, true);
    assert.match(snap.error, /not there/);
    const stream = await fetch(`${server.url}/api/stream`, { headers: { cookie: raw } });
    assert.equal(stream.status, 200);
    assert.match(stream.headers.get("content-type") ?? "", /text\/event-stream/);
    await stream.body?.cancel();
    const posted = await fetch(`${server.url}/api/state`, { method: "POST", headers: { cookie: raw } });
    assert.equal(posted.status, 405);
    const loggedOut = await fetch(`${server.url}/logout`, { method: "POST" });
    assert.match(loggedOut.headers.get("set-cookie") ?? "", new RegExp(`${COOKIE}=;`));
  } finally {
    await server.close();
  }
});

test("failed logins are rate limited and the token is not in the body", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "desk-auth-"));
  const app = createApp(options(dir));
  const server = await listen(app);
  try {
    let status = 0;
    for (let i = 0; i < 9; i++) {
      const response = await fetch(`${server.url}/login`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ token: "not-the-token-at-all" }),
      });
      status = response.status;
      assert.equal((await response.text()).includes(TOKEN), false);
    }
    assert.equal(status, 429);
  } finally {
    await server.close();
  }
});
