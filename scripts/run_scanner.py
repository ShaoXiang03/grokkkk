#!/usr/bin/env python3
"""Evaluate the latest closed 1H bar for each symbol in the local store.

Prints signal JSON. Webhooks fire only when webhook.enabled is true.
This process cannot place orders. mode=live is refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from desk.config import load_settings  # noqa: E402
from desk.scanner.service import open_store, refresh_symbols, scan_once  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Chart Analyst scanner once")
    parser.add_argument("--settings", default=str(ROOT / "config" / "settings.yaml"))
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--refresh", action="store_true", help="Pull public klines before scanning")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--daily-days", type=int, default=400)
    args = parser.parse_args()
    settings = load_settings(args.settings)
    store = open_store(settings)
    symbols = args.symbols or None
    if args.refresh:
        target = symbols or [row["symbol"] for row in store.list_symbols(trading_only=True)]
        if not target:
            target = ["BTCUSDT", "ETHUSDT"]
        refresh_symbols(settings, store, target, days=args.days, daily_days=args.daily_days)
    packets = scan_once(settings, store, symbols=symbols)
    print(json.dumps(packets, indent=2, default=str))
    print(f"packets: {len(packets)}", file=sys.stderr)
    store.close()


if __name__ == "__main__":
    main()
