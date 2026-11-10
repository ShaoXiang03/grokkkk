"""Append-only journal entries in the same SQLite file as the market cache."""

from __future__ import annotations

from desk.data.store import Store


def record(store: Store, ts_ms: int, kind: str, payload: dict) -> None:
    store.journal(ts_ms, kind, payload)
