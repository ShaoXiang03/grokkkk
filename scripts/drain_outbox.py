#!/usr/bin/env python3
"""Deliver due webhook outbox rows once. The body stays on disk until it is delivered or dead-lettered."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from desk.config import assert_no_live_orders, load_settings  # noqa: E402
from desk.delivery.outbox import drain_outbox  # noqa: E402
from desk.scanner.service import open_store  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Drain the signal webhook outbox")
    parser.add_argument("--settings", default=str(ROOT / "config" / "settings.yaml"))
    args = parser.parse_args()
    settings = load_settings(args.settings)
    assert_no_live_orders(settings)
    store = open_store(settings)
    outcomes = drain_outbox(store, settings, int(time.time() * 1000))
    print(json.dumps(outcomes, indent=2, default=str))
    store.close()


if __name__ == "__main__":
    main()
