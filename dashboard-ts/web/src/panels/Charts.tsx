import type { CurvePoint } from "../../../shared/types";
import { fmtUsd, useCanvas } from "../lib";

const MONO = "ui-monospace, SFMono-Regular, Menlo, monospace";

/** Desk equity (green) against BTC buy-and-hold (amber). No chart library. */
export function EquityChart({ desk, btc }: { desk: CurvePoint[]; btc: CurvePoint[] }) {
  const ref = useCanvas((ctx, w, h) => {
    ctx.clearRect(0, 0, w, h);
    const series = [
      { pts: desk, color: "#4ade80" },
      { pts: btc, color: "#f5b942" },
    ].filter((s) => s.pts.length > 1);
    if (!series.length) {
      ctx.fillStyle = "rgba(215,219,232,0.5)";
      ctx.font = `11px ${MONO}`;
      ctx.fillText("waiting for marks…", 12, 22);
      return;
    }
    const all = series.flatMap((s) => s.pts);
    const t0 = Math.min(...all.map((p) => p.t));
    const t1 = Math.max(...all.map((p) => p.t));
    let lo = Math.min(...all.map((p) => p.equity));
    let hi = Math.max(...all.map((p) => p.equity));
    const pad = (hi - lo) * 0.12 || 1;
    lo -= pad; hi += pad;
    const top = 16, bot = 18;
    const X = (t: number) => ((t - t0) / (t1 - t0 || 1)) * (w - 8) + 4;
    const Y = (v: number) => top + (1 - (v - lo) / (hi - lo)) * (h - top - bot);
    ctx.strokeStyle = "rgba(255,255,255,0.07)";
    ctx.lineWidth = 1;
    for (let g = 0; g <= 3; g++) {
      const y = top + (g / 3) * (h - top - bot);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }
    for (const s of series) {
      const g = ctx.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, s.color + "55");
      g.addColorStop(1, s.color + "00");
      ctx.fillStyle = g;
      ctx.beginPath();
      s.pts.forEach((p, i) => { const x = X(p.t), y = Y(p.equity); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
      ctx.lineTo(X(s.pts[s.pts.length - 1].t), h - bot);
      ctx.lineTo(X(s.pts[0].t), h - bot);
      ctx.closePath();
      ctx.fill();
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      s.pts.forEach((p, i) => { const x = X(p.t), y = Y(p.equity); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
      ctx.stroke();
    }
    ctx.font = `10px ${MONO}`;
    ctx.fillStyle = "rgba(215,219,232,0.55)";
    ctx.fillText(fmtUsd(hi - pad, 0), 6, 12);
    ctx.fillText(fmtUsd(lo + pad, 0), 6, h - 4);
  });
  return (
    <div className="cvbox">
      <canvas ref={ref} className="cv" role="img" aria-label="Desk equity against BTC buy and hold since the paper clock started" />
    </div>
  );
}

export function Spark({ data, color }: { data: number[]; color: string }) {
  const ref = useCanvas((ctx, w, h) => {
    ctx.clearRect(0, 0, w, h);
    if (data.length < 2) return;
    const lo = Math.min(...data), hi = Math.max(...data), span = hi - lo || 1;
    const X = (i: number) => (i / (data.length - 1)) * w;
    const Y = (v: number) => 2 + (1 - (v - lo) / span) * (h - 4);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    data.forEach((v, i) => { if (i) ctx.lineTo(X(i), Y(v)); else ctx.moveTo(X(i), Y(v)); });
    ctx.stroke();
  });
  return <canvas ref={ref} className="spark" aria-hidden="true" />;
}
