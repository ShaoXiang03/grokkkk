#!/usr/bin/env python3
"""Backfill funding, premium index, and daily open interest from data.binance.vision.

Reads the daily klines already stored by scripts/backfill.py. No API key is
read. No order route is called.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from desk.config import load_settings  # noqa: E402
from desk.data.backfill_research import (  # noqa: E402
    backfill_funding_and_premium,
    backfill_metrics,
    candidate_symbols,
    targeted_metric_days,
)
from desk.data.store import Store  # noqa: E402
from desk.strategies import require_research_mode  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill research archives (funding, premium, OI)")
    parser.add_argument("--settings", default=str(ROOT / "config" / "settings.yaml"))
    parser.add_argument("--cache-dir", default=str(ROOT / "data" / "vision-cache"))
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--skip-metrics", action="store_true")
    parser.add_argument("--metrics-only", action="store_true")
    parser.add_argument(
        "--metrics-top",
        type=int,
        default=80,
        help="Download open interest for the point-in-time top N by 30-day quote volume (0 = every candidate day)",
    )
    args = parser.parse_args()
    settings = load_settings(args.settings)
    require_research_mode(settings)
    path = Path(settings["data"]["sqlite_path"])
    if not path.is_absolute():
        path = ROOT / path
    store = Store(path)
    symbols = candidate_symbols(store)
    print(f"candidate symbols={len(symbols)}", flush=True)
    timeout = max(60.0, float(settings["data"]["request_timeout_seconds"]))
    if not args.metrics_only:
        backfill_funding_and_premium(
            store,
            symbols,
            cache_dir=Path(args.cache_dir),
            timeout=timeout,
            workers=max(1, args.workers),
        )
    if not args.skip_metrics:
        plan = None
        if args.metrics_top > 0:
            print(f"building point-in-time top-{args.metrics_top} metrics plan", flush=True)
            plan = targeted_metric_days(store, top_n=args.metrics_top)
            print(f"metrics plan symbols={len(plan)} days={sum(len(days) for days in plan.values())}", flush=True)
        backfill_metrics(store, symbols, timeout=timeout, workers=max(1, args.workers), plan=plan)
    print(f"non-trading rows={store.count_non_trading()}", flush=True)
    store.close()
    print(f"done in {time.time():.0f}", flush=True)


if __name__ == "__main__":
    main()
