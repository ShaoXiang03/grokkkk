#!/usr/bin/env python3
"""Attach market_context and master_decision to one signal.

A local operator command. The HTTP endpoint is the remote equivalent and
requires an HMAC. Neither path can override a failed hard gate, and neither
path places an order. mode=live is refused.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from desk.config import assert_no_live_orders, load_settings  # noqa: E402
from desk.delivery.eligibility import ReviewError, public_review, submit_review  # noqa: E402
from desk.paper.engine import review_account  # noqa: E402
from desk.scanner.service import open_store  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Attach a reviewer decision to a signal")
    parser.add_argument("--settings", default=str(ROOT / "config" / "settings.yaml"))
    parser.add_argument("--signal-id", required=True)
    parser.add_argument("--decision", required=True, choices=("open", "skip"))
    parser.add_argument("--reasoning", required=True)
    parser.add_argument("--summary", default=None)
    parser.add_argument("--context-json", default=None, help="Path to a market_context JSON object")
    parser.add_argument("--now-ms", type=int, default=None)
    args = parser.parse_args()
    settings = load_settings(args.settings)
    assert_no_live_orders(settings)
    store = open_store(settings)
    now_ms = int(args.now_ms if args.now_ms is not None else time.time() * 1000)
    if args.context_json:
        context = json.loads(Path(args.context_json).read_text())
    else:
        context = {
            "summary": args.summary,
            "strengtheners": [],
            "weakeners": [],
            "onchain_checks": {},
            "sources": ["cli"],
            "ts": None,
        }
    snapshot = review_account(store, settings, now_ms)
    account = None if snapshot is None else snapshot
    positions = [] if snapshot is None else list(snapshot.get("open_positions") or [])
    try:
        result = submit_review(
            store,
            settings,
            args.signal_id,
            context,
            {"decision": args.decision, "reasoning": args.reasoning, "ts": None},
            now_ms=now_ms,
            account=account,
            open_positions=positions,
        )
    except ReviewError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        store.close()
        sys.exit(1)
    print(json.dumps(public_review(result), indent=2))
    store.close()


if __name__ == "__main__":
    main()
