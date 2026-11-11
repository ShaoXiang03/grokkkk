"""Persist the paper refresh counters the dashboard can read.

The refresh thread keeps these numbers in memory. A JSON file next to the
sqlite database is the only copy that survives the process. Writing it does
not change a fill, a gate, or a request. Failures are swallowed so a full
disk cannot stop the paper loop.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

STATUS_NAME = "refresh_status.json"

_SLICE_INTS = (
    "vision_files",
    "vision_rows",
    "fapi_requests",
    "fapi_rows",
    "funding_files",
    "errors",
    "pending",
    "inactive",
    "http",
)

_TOTAL_KEYS = (
    "vision_files",
    "vision_rows",
    "fapi_requests",
    "fapi_rows",
    "funding_requests",
    "errors",
    "pauses",
    "http_429",
    "http_418",
    "slices",
)


def sqlite_path_for(settings: dict) -> Path:
    """Same relative-path rule as ``desk.scanner.service.open_store``."""
    raw = Path((settings.get("data") or {}).get("sqlite_path") or "data/desk.sqlite")
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    return raw


def status_path_for(settings: dict) -> Path:
    return sqlite_path_for(settings).parent / STATUS_NAME


def empty_totals() -> dict:
    return {key: 0 for key in _TOTAL_KEYS}


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def slice_view(summary: dict) -> dict:
    out = {key: _as_int(summary.get(key)) for key in _SLICE_INTS}
    out["paused"] = bool(summary.get("paused"))
    reason = summary.get("pause_reason")
    out["pause_reason"] = None if reason in (None, "") else str(reason)[:80]
    return out


class RefreshObserver:
    """Accumulate slice summaries and replace the status file atomically."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.totals = empty_totals()
        self._pause_reason: str | None = None
        self._load()

    @classmethod
    def from_settings(cls, settings: dict) -> RefreshObserver:
        return cls(status_path_for(settings))

    def observe(self, summary: dict) -> None:
        try:
            with self._lock:
                self._apply(summary if isinstance(summary, dict) else {})
                self._write()
        except Exception:
            logging.exception("paper refresh status file was not updated")

    def _apply(self, summary: dict) -> None:
        view = slice_view(summary)
        self.totals["slices"] += 1
        self.totals["vision_files"] += view["vision_files"]
        self.totals["vision_rows"] += view["vision_rows"]
        self.totals["fapi_requests"] += view["fapi_requests"]
        self.totals["fapi_rows"] += view["fapi_rows"]
        self.totals["funding_requests"] += view["funding_files"]
        self.totals["errors"] += view["errors"]
        reason = view["pause_reason"] if view["paused"] else None
        if view["paused"] and reason != self._pause_reason:
            self.totals["pauses"] += 1
            if reason == "http_429":
                self.totals["http_429"] += 1
            elif reason == "http_418":
                self.totals["http_418"] += 1
        self._pause_reason = reason
        self._last = view

    def _load(self) -> None:
        self._last = slice_view({})
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return
        if not isinstance(payload, dict):
            return
        stored = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
        for key in _TOTAL_KEYS:
            self.totals[key] = max(0, _as_int(stored.get(key)))
        if payload.get("paused"):
            reason = payload.get("pause_reason")
            self._pause_reason = None if reason in (None, "") else str(reason)[:80]
        last = payload.get("last_slice")
        if isinstance(last, dict):
            self._last = slice_view(last)

    def _write(self) -> None:
        import time

        payload = {
            "v": 1,
            "updated_ms": int(time.time() * 1000),
            "paused": bool(self._last.get("paused")),
            "pause_reason": self._pause_reason,
            "last_slice": self._last,
            "totals": dict(self.totals),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)
