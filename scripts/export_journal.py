#!/usr/bin/env python3
"""Export the append-only decision journal as a Portfolio Journal CSV and JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from desk.config import load_settings  # noqa: E402
from desk.journal.decision import export_portfolio, write_portfolio_csv, write_portfolio_json  # noqa: E402
from desk.scanner.service import open_store  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the decision journal")
    parser.add_argument("--settings", default=str(ROOT / "config" / "settings.yaml"))
    parser.add_argument("--csv", default="")
    parser.add_argument("--json", default="")
    args = parser.parse_args()
    settings = load_settings(args.settings)
    store = open_store(settings)
    records = export_portfolio(store)
    if args.csv:
        write_portfolio_csv(records, args.csv)
    if args.json:
        write_portfolio_json(records, args.json)
    if not args.csv and not args.json:
        print(json.dumps(records, indent=2, default=str))
    else:
        print(f"rows: {len(records)}", file=sys.stderr)
    store.close()


if __name__ == "__main__":
    main()
