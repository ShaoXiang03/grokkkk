import { useEffect, useRef, useState } from "react";
import type { DeskSnapshot } from "../../shared/types";

export const clock = { skew: 0 };

export const coin = (s: string) => s.replace(/USDT$/, "");
export const fmtPx = (p: number | null | undefined) =>
  p == null || Number.isNaN(p) ? "—" : Math.abs(p) >= 1000 ? p.toLocaleString("en-US", { maximumFractionDigits: 1 }) : Math.abs(p) >= 1 ? p.toFixed(2) : p.toPrecision(4);
export const fmtUsd = (v: number | null | undefined, dp = 2) =>
  v == null || Number.isNaN(v) ? "—" : `${v < 0 ? "-" : ""}$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp })}`;
export const pct = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? "—" : `${(v * 100).toFixed(2)}%`);
export const signed = (v: number, dp = 2) => `${v >= 0 ? "+" : "-"}${Math.abs(v).toFixed(dp)}`;
export const ago = (t: number, now: number) => {
  const s = Math.max(0, Math.round((now - t) / 1000));
  return s < 90 ? `${s}s` : s < 5400 ? `${Math.floor(s / 60)}m` : `${Math.floor(s / 3600)}h`;
};
export const hhmmss = (t: number, zone: "UTC" | "MYT") => {
  const ms = zone === "MYT" ? t + 8 * 3600_000 : t;
  const d = new Date(ms);
  return [d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
};

export function liveStream(url: string, onData: (data: string) => void, onLink: (up: boolean) => void, quietMs = 12_000): () => void {
  let es: EventSource | null = null;
  let last = Date.now();
  let retry: ReturnType<typeof setTimeout> | null = null;
  let done = false;
  const open = () => {
    if (done) return;
    if (retry) { clearTimeout(retry); retry = null; }
    es?.close();
    last = Date.now();
    const mine = (es = new EventSource(url));
    mine.onopen = () => { last = Date.now(); };
    mine.onmessage = (m) => { last = Date.now(); onLink(true); onData(m.data); };
    mine.onerror = () => {
      onLink(false);
      if (mine === es && mine.readyState === EventSource.CLOSED && !retry) retry = setTimeout(open, 3_000);
    };
  };
  const check = () => { if (Date.now() - last > quietMs) { onLink(false); open(); } };
  const wake = () => { if (document.visibilityState === "visible") check(); };
  open();
  const id = setInterval(check, 3_000);
  document.addEventListener("visibilitychange", wake);
  window.addEventListener("focus", wake);
  window.addEventListener("online", open);
  return () => {
    done = true;
    clearInterval(id);
    if (retry) clearTimeout(retry);
    document.removeEventListener("visibilitychange", wake);
    window.removeEventListener("focus", wake);
    window.removeEventListener("online", open);
    es?.close();
  };
}

export function useDesk(authed: boolean): { state: DeskSnapshot | null; connected: boolean; denied: boolean } {
  const [state, setState] = useState<DeskSnapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const [denied, setDenied] = useState(false);
  useEffect(() => {
    if (!authed) return;
    let stop = () => {};
    fetch("/api/state", { cache: "no-store" }).then(async (r) => {
      if (r.status === 401) { setDenied(true); return; }
      const j = await r.json() as DeskSnapshot;
      clock.skew = j.now - Date.now();
      setState((s) => s ?? j);
    }).catch(() => { /* the stream retries */ });
    stop = liveStream("/api/stream", (data) => {
      try {
        const j = JSON.parse(data) as DeskSnapshot;
        clock.skew = j.now - Date.now();
        setState(j);
        setDenied(false);
      } catch { /* torn frame */ }
    }, setConnected);
    return () => stop();
  }, [authed]);
  return { state, connected, denied };
}

type Draw = (ctx: CanvasRenderingContext2D, w: number, h: number) => void;

export function useCanvas(draw: Draw) {
  const ref = useRef<HTMLCanvasElement>(null);
  const drawRef = useRef(draw);
  drawRef.current = draw;
  const dims = useRef({ w: 0, h: 0 });
  useEffect(() => {
    const cv = ref.current;
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    const size = () => {
      const r = cv.getBoundingClientRect();
      const d = window.devicePixelRatio || 1;
      dims.current = { w: r.width, h: r.height };
      cv.width = Math.max(1, Math.round(r.width * d));
      cv.height = Math.max(1, Math.round(r.height * d));
      ctx.setTransform(d, 0, 0, d, 0, 0);
    };
    const paint = () => drawRef.current(ctx, dims.current.w, dims.current.h);
    const ro = new ResizeObserver(() => { size(); paint(); });
    ro.observe(cv);
    size();
    paint();
    return () => ro.disconnect();
  }, []);
  useEffect(() => {
    const cv = ref.current;
    const ctx = cv?.getContext("2d");
    if (ctx && dims.current.w) drawRef.current(ctx, dims.current.w, dims.current.h);
  });
  return ref;
}
