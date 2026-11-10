"""Serialize paper positions. The fill math stays in the backtester."""

from __future__ import annotations

import json
from dataclasses import asdict, fields

import numpy as np

from desk.backtest.replay import Position


def _default(value):
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    raise TypeError(type(value))


def dump_position(pos: Position) -> str:
    payload = asdict(pos)
    payload["flows"] = [[int(ts), float(amount)] for ts, amount in pos.flows]
    return json.dumps(payload, sort_keys=True, default=_default)


def load_position(payload: str) -> Position:
    data = json.loads(payload)
    data["flows"] = [(int(item[0]), float(item[1])) for item in data.get("flows") or []]
    allowed = {field.name for field in fields(Position)}
    cleaned = {key: data[key] for key in allowed if key in data}
    return Position(**cleaned)
