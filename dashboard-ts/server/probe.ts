import http from "node:http";
import https from "node:https";

export interface ProbeResult {
  ok: boolean;
  status: number | null;
}

/**
 * GET a tiny health URL and read the whole body before finishing.
 * Connection: close and no keep-alive agent, so the paper server on :8787
 * is not reset by an abort that arrives before it finishes writing.
 */
export function probeHealth(url: string, timeoutMs = 4000): Promise<ProbeResult> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result: ProbeResult): void => {
      if (settled) return;
      settled = true;
      resolve(result);
    };
    let parsed: URL;
    try {
      parsed = new URL(url);
    } catch {
      finish({ ok: false, status: null });
      return;
    }
    const lib = parsed.protocol === "https:" ? https : http;
    const agent = new lib.Agent({ keepAlive: false });
    const req = lib.request(
      {
        protocol: parsed.protocol,
        hostname: parsed.hostname,
        port: parsed.port,
        path: `${parsed.pathname}${parsed.search}`,
        method: "GET",
        agent,
        headers: { Connection: "close", Accept: "application/json" },
      },
      (res) => {
        res.on("data", () => { /* the body has to be consumed, even when it is only {"ok":true} */ });
        res.on("end", () => {
          agent.destroy();
          finish({ ok: (res.statusCode ?? 500) < 400, status: res.statusCode ?? null });
        });
        res.on("error", () => {
          agent.destroy();
          finish({ ok: false, status: res.statusCode ?? null });
        });
      },
    );
    req.setTimeout(timeoutMs, () => {
      req.destroy();
      agent.destroy();
      finish({ ok: false, status: null });
    });
    req.on("error", () => {
      agent.destroy();
      finish({ ok: false, status: null });
    });
    req.end();
  });
}

const reviewCache = new Map<string, ProbeResult & { at: number }>();

/** /health is a liveness bit. One shared result, refreshed at most every 10s. */
export async function cachedProbe(url: string | undefined, everyMs = 10_000): Promise<ProbeResult> {
  if (!url) return { ok: false, status: null };
  const hit = reviewCache.get(url);
  const now = Date.now();
  if (hit && now - hit.at < everyMs) return hit;
  const result = await probeHealth(url);
  const stored = { at: Date.now(), ...result };
  reviewCache.set(url, stored);
  return stored;
}
