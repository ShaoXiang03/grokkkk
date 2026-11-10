"""Rolling in-sample / out-of-sample windows. Parameters stay frozen."""

from __future__ import annotations

from desk.timeutils import DAY_MS


def walk_forward_folds(start_ms: int, end_ms: int, in_sample_days: int, out_sample_days: int) -> list[dict]:
    """Non-overlapping out-of-sample windows. In-sample windows may overlap.

    The fold is anchored on the out-of-sample segment that follows each
    in-sample block. Nothing here refits parameters.
    """
    is_ms = int(in_sample_days) * DAY_MS
    oos_ms = int(out_sample_days) * DAY_MS
    folds = []
    cursor = int(start_ms)
    while cursor + is_ms + oos_ms <= int(end_ms):
        is_end = cursor + is_ms
        oos_end = is_end + oos_ms
        folds.append(
            {
                "in_sample": {"start_ms": cursor, "end_ms": is_end},
                "out_of_sample": {"start_ms": is_end, "end_ms": oos_end},
            }
        )
        cursor += oos_ms
    return folds


def select_trades(trades: list, start_ms: int, end_ms: int) -> list:
    """Trades whose entry time falls in [start, end)."""
    return [trade for trade in trades if start_ms <= int(trade["entry_time"]) < end_ms]
