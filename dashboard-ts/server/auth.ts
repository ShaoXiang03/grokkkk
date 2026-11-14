/**
 * Login cookie for the public dashboard.
 *
 * The token is compared in constant time and never written to a response.
 * The cookie is HttpOnly and SameSite=Strict. It is not marked Secure:
 * this process is reached on plain HTTP :8099, and a Secure cookie would
 * never be stored. The signature is an HMAC of the payload under the token,
 * so a restart does not drop every session and the server stores no session table.
 */

import { createHmac, timingSafeEqual } from "node:crypto";
import type { Request, Response, NextFunction } from "express";

export const COOKIE = "desk_ts";
const WINDOW_MS = 10 * 60_000;
const MAX_FAILS = 8;
const SESSION_MS = 12 * 3600_000;

const fails = new Map<string, { n: number; start: number }>();

export function clientKey(req: Request): string {
  return req.socket.remoteAddress || "unknown";
}

export function rateLimit(key: string, now: number): { ok: true } | { ok: false; retryMs: number } {
  const row = fails.get(key);
  if (!row || now - row.start > WINDOW_MS) return { ok: true };
  if (row.n >= MAX_FAILS) return { ok: false, retryMs: WINDOW_MS - (now - row.start) };
  return { ok: true };
}

export function noteFailure(key: string, now: number): void {
  const row = fails.get(key);
  if (!row || now - row.start > WINDOW_MS) fails.set(key, { n: 1, start: now });
  else row.n += 1;
}

export function noteSuccess(key: string): void {
  fails.delete(key);
}

export function tokensEqual(given: string, expected: string): boolean {
  const a = Buffer.from(given);
  const b = Buffer.from(expected);
  if (a.length !== b.length || a.length === 0) return false;
  return timingSafeEqual(a, b);
}

export function signSession(token: string, now: number): string {
  const body = Buffer.from(JSON.stringify({ exp: now + SESSION_MS })).toString("base64url");
  const mac = createHmac("sha256", token).update(body).digest("base64url");
  return `${body}.${mac}`;
}

export function sessionValid(token: string, raw: string | undefined, now: number): boolean {
  if (!raw || !token) return false;
  const dot = raw.lastIndexOf(".");
  if (dot <= 0) return false;
  const body = raw.slice(0, dot);
  const mac = raw.slice(dot + 1);
  const expect = createHmac("sha256", token).update(body).digest("base64url");
  const a = Buffer.from(mac);
  const b = Buffer.from(expect);
  if (a.length !== b.length || !timingSafeEqual(a, b)) return false;
  try {
    const payload = JSON.parse(Buffer.from(body, "base64url").toString("utf8")) as { exp?: number };
    return typeof payload.exp === "number" && payload.exp > now;
  } catch {
    return false;
  }
}

export function readCookie(header: string | undefined, name: string): string | undefined {
  if (!header) return undefined;
  for (const part of header.split(";")) {
    const [k, ...rest] = part.trim().split("=");
    if (k === name) return decodeURIComponent(rest.join("="));
  }
  return undefined;
}

export function setSessionCookie(res: Response, value: string): void {
  const maxAge = Math.floor(SESSION_MS / 1000);
  res.setHeader(
    "Set-Cookie",
    `${COOKIE}=${encodeURIComponent(value)}; HttpOnly; SameSite=Strict; Path=/; Max-Age=${maxAge}`,
  );
}

export function clearSessionCookie(res: Response): void {
  res.setHeader("Set-Cookie", `${COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0`);
}

export function requireAuth(token: string) {
  return (req: Request, res: Response, next: NextFunction): void => {
    const raw = readCookie(req.headers.cookie, COOKIE);
    if (!sessionValid(token, raw, Date.now())) {
      res.status(401).json({ ok: false, error: "unauthorized" });
      return;
    }
    next();
  };
}
