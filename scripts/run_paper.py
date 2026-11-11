#!/usr/bin/env python3
"""Run the paper loop on closed public bars.

This process simulates fills. It does not place orders. mode=live is refused.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from desk.config import assert_no_live_orders, load_settings  # noqa: E402
from desk.data.paper_refresh import PaperRefresh, due_daily_symbols  # noqa: E402
from desk.delivery.http import serve_review  # noqa: E402
from desk.paper.engine import review_account, run_cycle  # noqa: E402
from desk.paper.freshness import daily_only  # noqa: E402
from desk.paper.scorecard import build_scorecard  # noqa: E402
from desk.scanner.service import open_store  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Chart Analyst paper engine")
    parser.add_argument("--settings", default=str(ROOT / "config" / "settings.yaml"))
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--once", action="store_true", help="Run a single cycle (the default)")
    parser.add_argument("--loop", action="store_true", help="Repeat until the process is stopped")
    parser.add_argument("--report", action="store_true", help="Print the scorecard without a cycle")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Incrementally refresh daily bars. Vision first; fapi stays inside the weight budget",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Ignored. The paper refresh continues from the last stored bar; it does not pull a 1h history",
    )
    parser.add_argument(
        "--daily-days",
        type=int,
        default=400,
        help="Ignored. Daily history comes from the vision backfill, then this process fills the days after it",
    )
    parser.add_argument("--serve", action="store_true", help="Serve the review endpoint when review.listen is set")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings(args.settings)
    assert_no_live_orders(settings)
    store = open_store(settings)
    symbols = args.symbols or None
    report_only = args.report and not args.loop and not args.once and not args.refresh and not args.serve
    if report_only:
        print(json.dumps(build_scorecard(store, settings, int(time.time() * 1000)), indent=2, default=str))
        store.close()
        return
    if (args.loop or args.serve) and str(settings["review"].get("listen") or "").strip():
        _start_review_server(store, settings)
    elif args.serve and not args.loop:
        logging.error("review.listen is empty; set it or use scripts/serve_review.py --listen 127.0.0.1:8787")
        store.close()
        sys.exit(1)
    elif args.serve:
        logging.warning("review.listen is empty; the paper loop will run without the review endpoint")

    refresher = PaperRefresh(settings) if args.refresh else None

    def symbol_source() -> list[str]:
        if symbols:
            return list(symbols)
        if daily_only(settings):
            return due_daily_symbols(store)
        configured = list(settings["data"].get("symbols") or [])
        if configured:
            return configured
        return store.symbols_with_interval("1d") or store.symbols_with_interval("1h")

    def cycle() -> None:
        # Exits run inside run_cycle before the staleness gate. Refresh does
        # not run on this thread in --loop, so a slow download cannot sit in
        # front of a stop. A one-shot refreshes first, and still cannot raise
        # out of a 418/429.
        if refresher is not None and not args.loop:
            try:
                summary = refresher.run_for(store, symbol_source(), max_ms=120_000)
                logging.info("paper refresh %s", summary)
            except Exception:
                logging.exception("paper refresh failed; cycle continues on stored bars")
        try:
            result = run_cycle(settings, store, now_ms=int(time.time() * 1000), symbols=symbols)
        except Exception:
            logging.exception("paper cycle failed")
            return
        logging.info(
            "paper cycle block=%s scanned=%s filled=%s violations=%s",
            result["block"],
            result["scanned"],
            len(result["filled"]),
            result["scorecard"]["rule_violations"],
        )
        print(json.dumps(result["scorecard"], indent=2, default=str), flush=True)

    if args.loop:
        if refresher is not None:
            refresher.start_background(store, symbol_source)
        interval = float(settings["paper"].get("loop_seconds") or 60)
        while True:
            cycle()
            time.sleep(interval)
    elif args.serve and not args.once and not args.refresh:
        threading.Event().wait()
    else:
        cycle()
        if args.serve:
            threading.Event().wait()
    store.close()


def _start_review_server(store, settings) -> None:
    def provider():
        now_ms = int(time.time() * 1000)
        snapshot = review_account(store, settings, now_ms)
        if snapshot is None:
            return {"account": None, "open_positions": []}
        return {"account": snapshot, "open_positions": snapshot.get("open_positions") or []}

    server = serve_review(store, settings, account_provider=provider)
    thread = threading.Thread(target=server.serve_forever, name="review-http", daemon=True)
    thread.start()
    logging.info("review endpoint listening on %s", settings["review"]["listen"])


if __name__ == "__main__":
    main()
