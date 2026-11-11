"""Read-only monitoring dashboard for the paper desk.

This package does not place orders, attach reviews, or change config.
The HTTP process binds to loopback unless an operator turns on the
authenticated remote option. SQLite is opened with mode=ro.
"""

__all__ = ["build_snapshot"]


def __getattr__(name: str):
    if name == "build_snapshot":
        from desk.dashboard.read import build_snapshot

        return build_snapshot
    raise AttributeError(name)
