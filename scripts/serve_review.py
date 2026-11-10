#!/usr/bin/env python3
"""Serve POST /signals/<id>/review on the configured loopback address.

The request body must be HMAC-signed. A failed gate cannot be opened.
This process does not place orders. mode=live is refused.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from desk.config import assert_no_live_orders, load_settings  # noqa: E402
from desk.delivery.http import serve_review  # noqa: E402
from desk.paper.engine import review_account  # noqa: E402
from desk.scanner.service import open_store  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the signal review endpoint")
    parser.add_argument("--settings", default=str(ROOT / "config" / "settings.yaml"))
    parser.add_argument("--listen", default=None, help="Override review.listen, for example 127.0.0.1:8787")
    args = parser.parse_args()
    settings = load_settings(args.settings)
    assert_no_live_orders(settings)
    if args.listen:
        settings["review"]["listen"] = args.listen
    if not str(settings["review"].get("listen") or "").strip():
        print("review.listen is empty. Pass --listen 127.0.0.1:8787 or set it in settings.", file=sys.stderr)
        sys.exit(1)
    store = open_store(settings)

    def provider():
        snapshot = review_account(store, settings, int(time.time() * 1000))
        if snapshot is None:
            return {"account": None, "open_positions": []}
        return {"account": snapshot, "open_positions": snapshot.get("open_positions") or []}

    server = serve_review(store, settings, account_provider=provider)
    print(f"review endpoint on http://{settings['review']['listen']}/signals/<id>/review", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    store.close()


if __name__ == "__main__":
    main()
