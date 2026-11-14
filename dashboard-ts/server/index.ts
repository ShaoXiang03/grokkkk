/**
 * Public read-only desk page on 0.0.0.0:8099.
 *
 * GET /api/state and GET /api/stream require the signed login cookie.
 * POST /login and POST /logout only set or clear that cookie. There is no
 * route that writes the desk, the kill file, or a review.
 */

import express, { type Request, type Response } from "express";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  clearSessionCookie,
  clientKey,
  noteFailure,
  noteSuccess,
  rateLimit,
  requireAuth,
  setSessionCookie,
  signSession,
  tokensEqual,
} from "./auth.js";
import { SnapshotHub } from "./hub.js";
import { cachedProbe } from "./probe.js";
import { readDesk, type ReadInput } from "./read.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIST = path.join(HERE, "..", "web", "dist");
const HOST = process.env.HOST ?? "0.0.0.0";
const PORT = Number(process.env.PORT ?? 8099);
const ENV_FILE = process.env.DASHBOARD_ENV ?? "/etc/chart-analyst/dashboard-ts.env";

export interface AppOptions {
  token: string;
  sqlitePath: string;
  settingsPath: string;
  journalPath?: string;
  refreshPath?: string;
  killPath?: string;
  reviewUrl?: string;
  now?: () => number;
}

export function loadToken(envFile = ENV_FILE): string {
  const fromEnv = process.env.DASHBOARD_TOKEN?.trim();
  if (fromEnv) return fromEnv;
  if (!existsSync(envFile)) return "";
  for (const line of readFileSync(envFile, "utf8").split("\n")) {
    const trimmed = line.trim();
    if (trimmed.startsWith("DASHBOARD_TOKEN=")) return trimmed.slice("DASHBOARD_TOKEN=".length).trim();
  }
  return "";
}

export function defaultOptions(token: string): AppOptions {
  return {
    token,
    sqlitePath: process.env.DESK_SQLITE ?? "/var/lib/chart-analyst/desk.sqlite",
    settingsPath: process.env.DESK_SETTINGS ?? "/opt/chart-analyst/config/settings.yaml",
    journalPath: process.env.DESK_JOURNAL ?? "/var/lib/chart-analyst/journal.jsonl",
    refreshPath: process.env.DESK_REFRESH ?? "/var/lib/chart-analyst/refresh_status.json",
    reviewUrl: process.env.REVIEW_HEALTH ?? "http://127.0.0.1:8787/health",
  };
}

function snapshotInput(opts: AppOptions, review: { ok: boolean; status: number | null }): ReadInput {
  return {
    sqlitePath: opts.sqlitePath,
    settingsPath: opts.settingsPath,
    journalPath: opts.journalPath,
    refreshPath: opts.refreshPath,
    killPath: opts.killPath,
    now: opts.now?.(),
    review,
  };
}

export function createApp(opts: AppOptions) {
  const hub = new SnapshotHub(async () => {
    const review = await cachedProbe(opts.reviewUrl);
    return readDesk(snapshotInput(opts, review));
  });
  const app = express();
  app.disable("x-powered-by");
  app.use(express.json({ limit: "2kb" }));
  app.use((_req, res, next) => {
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("Referrer-Policy", "no-referrer");
    res.setHeader("X-Frame-Options", "DENY");
    next();
  });

  app.post("/login", (req, res) => {
    const now = Date.now();
    const key = clientKey(req);
    const limited = rateLimit(key, now);
    if (!limited.ok) {
      res.status(429).json({ ok: false, error: "rate_limited" });
      return;
    }
    const given = typeof req.body?.token === "string" ? req.body.token : "";
    if (!opts.token || !tokensEqual(given, opts.token)) {
      noteFailure(key, now);
      res.status(401).json({ ok: false, error: "unauthorized" });
      return;
    }
    noteSuccess(key);
    setSessionCookie(res, signSession(opts.token, now));
    res.json({ ok: true });
  });

  app.post("/logout", (_req, res) => {
    clearSessionCookie(res);
    res.json({ ok: true });
  });

  const guard = requireAuth(opts.token);

  app.get("/api/health", guard, async (_req, res) => {
    const snap = await hub.current();
    res.setHeader("Cache-Control", "no-store");
    res.json({
      ok: snap.error == null,
      readOnly: true,
      orders: false,
      mode: snap.mode,
      cycleRecent: snap.health.cycleRecent,
      reviewOk: snap.health.reviewOk,
    });
  });

  app.get("/api/state", guard, async (_req, res) => {
    res.setHeader("Cache-Control", "no-store");
    res.json(await hub.current());
  });

  app.get("/api/stream", guard, (req, res) => {
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-store",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    });
    hub.add(res);
    req.on("close", () => hub.remove(res));
  });

  app.use((req, res, next) => {
    if (req.method !== "GET" && req.method !== "HEAD") {
      res.status(405).json({ ok: false, error: "read_only" });
      return;
    }
    next();
  });

  app.use("/api", (_req, res) => {
    res.status(404).json({ ok: false, error: "not_found" });
  });

  if (existsSync(WEB_DIST)) {
    app.use(express.static(WEB_DIST, { index: false, maxAge: "1h" }));
    app.get("*", (_req, res) => {
      res.setHeader("Cache-Control", "no-store");
      res.sendFile(path.join(WEB_DIST, "index.html"));
    });
  } else {
    app.get("*", (_req: Request, res: Response) => {
      res.status(503).type("text/plain").send("web/dist is missing. Run npm run build.");
    });
  }
  return app;
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const token = loadToken();
  if (!token || token.length < 16) {
    console.error("DASHBOARD_TOKEN is missing or shorter than 16 characters. Refusing to listen.");
    process.exit(1);
  }
  const app = createApp(defaultOptions(token));
  app.listen(PORT, HOST, () => {
    console.log(`dashboard-ts http://${HOST}:${PORT} read-only`);
  });
}
