#!/usr/bin/env python3
"""Replay stored klines through the scanner and write a metrics report.

The report contains only numbers this process computed. A short sample is not a proven edge.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from desk.backtest.replay import run_backtest  # noqa: E402
from desk.config import load_settings  # noqa: E402
from desk.scanner.service import open_store  # noqa: E402
from desk.timeutils import DAY_MS  # noqa: E402


def _fmt(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _print_metrics(label: str, metrics: dict) -> None:
    print(
        f"{label}: trades={metrics['trade_count']} win_rate={_fmt(metrics['win_rate'])} "
        f"avg_r={_fmt(metrics['avg_r'])} expectancy={_fmt(metrics['expectancy'])} "
        f"max_dd={_fmt(metrics['max_drawdown'])} sharpe={_fmt(metrics['sharpe'])} "
        f"net_pnl={_fmt(metrics['net_pnl'])}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the v0.3 rules on stored public data")
    parser.add_argument("--settings", default=str(ROOT / "config" / "settings.yaml"))
    parser.add_argument("--symbols", nargs="*", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--partial-at-r", type=float, default=None, help="Override the partial take-profit, for example 1 or 2")
    parser.add_argument(
        "--allow-below-min-rr",
        action="store_true",
        help="Keep entries when comparing a partial below the v0.3 minimum reward/risk",
    )
    parser.add_argument("--report", default=str(ROOT / "reports" / "backtest.json"))
    args = parser.parse_args()
    overrides = {}
    if args.partial_at_r is not None:
        overrides["exits"] = {"partial_at_r": args.partial_at_r}
    if args.allow_below_min_rr:
        overrides.setdefault("risk", {})["enforce_min_reward_risk"] = False
    settings = load_settings(args.settings, overrides=overrides or None)
    store = open_store(settings)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - args.days * DAY_MS
    symbols = {}
    for symbol in args.symbols:
        frame_1h = store.klines(symbol, "1h", start_ms=start_ms)
        frame_4h = store.klines(symbol, "4h", start_ms=start_ms - 40 * DAY_MS)
        frame_1d = store.klines(symbol, "1d")
        if frame_1h.empty or frame_4h.empty or frame_1d.empty:
            print(f"missing klines for {symbol}; run scripts/backfill.py first", file=sys.stderr)
            sys.exit(1)
        meta = store.symbol_row(symbol) or {"symbol": symbol, "base_asset": symbol.replace("USDT", "")}
        symbols[symbol] = {
            "1h": frame_1h,
            "4h": frame_4h,
            "1d": frame_1d,
            "funding": store.funding(symbol),
            "meta": meta,
        }
    btc = store.klines("BTCUSDT", "1d")
    if btc.empty:
        print("missing BTCUSDT daily klines", file=sys.stderr)
        sys.exit(1)
    report = run_backtest(
        symbols=symbols,
        btc_daily=btc,
        settings=settings,
        delisted_symbols_included=store.count_non_trading(),
    )
    # The trade list can be long; keep it in the JSON file and print the summary.
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"period: {report['period']}")
    print(f"symbols ({report['symbol_count']}): {', '.join(report['symbols'])}")
    print(f"delisted_symbols_included: {report['delisted_symbols_included']}")
    print(f"partial_at_r: {report['partial_at_r']}")
    print(f"proposals: {report['proposals']}")
    print(f"reject_counts: {report['reject_counts']}")
    for setup, metrics in report["per_setup"].items():
        _print_metrics(setup, metrics)
    _print_metrics("overall", report["overall"])
    walk = report["walk_forward"]
    print(f"walk_forward: {walk['note']}")
    if walk["out_of_sample"] is not None:
        _print_metrics("out_of_sample", walk["out_of_sample"])
    print(f"wrote {out}")
    store.close()


if __name__ == "__main__":
    main()
