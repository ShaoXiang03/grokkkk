"""14-day paper clock in Asia/Kuala_Lumpur."""

from __future__ import annotations

from desk.timeutils import DAY_MS, ms_to_myt_iso, myt_date


def myt_week_key(ms: int) -> str:
    day = myt_date(ms)
    return str(day.toordinal() - day.weekday())


def paper_clock(started_at_ms: int | None, now_ms: int, days: int = 14) -> dict:
    duration_ms = int(days) * DAY_MS
    if started_at_ms is None:
        return {
            "timezone": "Asia/Kuala_Lumpur",
            "started_at_ms": None,
            "started_at_myt": None,
            "as_of_myt": ms_to_myt_iso(now_ms),
            "calendar_day": 0,
            "duration_days": int(days),
            "elapsed_ms": 0,
            "remaining_ms": duration_ms,
            "complete": False,
        }
    elapsed = max(0, int(now_ms) - int(started_at_ms))
    calendar_day = (myt_date(now_ms) - myt_date(started_at_ms)).days + 1
    return {
        "timezone": "Asia/Kuala_Lumpur",
        "started_at_ms": int(started_at_ms),
        "started_at_myt": ms_to_myt_iso(started_at_ms),
        "as_of_myt": ms_to_myt_iso(now_ms),
        "calendar_day": calendar_day,
        "duration_days": int(days),
        "elapsed_ms": elapsed,
        "remaining_ms": max(0, duration_ms - elapsed),
        "complete": elapsed >= duration_ms,
    }
