#!/usr/bin/env python3
"""Serve the read-only desk dashboard on 127.0.0.1:8788.

This process does not place orders, attach reviews, or read webhook secrets
into a response. The paper engine is a different process. See docs/dashboard.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from desk.config import load_settings  # noqa: E402
from desk.dashboard.http import DashboardBindError, resolve_listen, serve  # noqa: E402


class Context:
    def __init__(self, settings, token, secrets_env_path):
        self.settings = settings
        self.token = token
        self.secrets_env_path = secrets_env_path
        self.now_ms = None
        self.environ = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Chart Analyst read-only monitoring dashboard")
    parser.add_argument("--settings", default=str(ROOT / "config" / "settings.yaml"))
    parser.add_argument("--listen", default="127.0.0.1:8788", help="host:port, default 127.0.0.1:8788")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Permit a non-loopback bind. Requires DASHBOARD_TOKEN in the environment.",
    )
    parser.add_argument(
        "--secrets-env",
        default="/etc/chart-analyst/secrets.env",
        help="Only KILL_SWITCH_PATH is read from this file. Other values are a denylist and are not rendered.",
    )
    args = parser.parse_args()
    token = os.environ.get("DASHBOARD_TOKEN")
    token = token.strip() if isinstance(token, str) else None
    if token == "":
        token = None
    try:
        host, port, enforced = resolve_listen(args.listen, allow_remote=args.allow_remote, token=token)
    except DashboardBindError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    settings = load_settings(args.settings)
    context = Context(settings, enforced, args.secrets_env)
    try:
        server = serve(host, port, context)
    except OSError as exc:
        print(f"dashboard could not bind {host}:{port}: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"dashboard http://{host}:{port} read-only", flush=True)
    if enforced:
        print("authorization required", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
