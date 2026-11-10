"""UTC and Asia/Kuala_Lumpur timestamps. Binance bars are UTC."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
MYT = ZoneInfo("Asia/Kuala_Lumpur")

HOUR_MS = 3_600_000
H4_MS = 14_400_000
DAY_MS = 86_400_000


def ms_to_dt(ms: int, tz: timezone | ZoneInfo = UTC) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=tz)


def ms_to_utc_iso(ms: int) -> str:
    return ms_to_dt(ms, UTC).isoformat()


def ms_to_myt_iso(ms: int) -> str:
    return ms_to_dt(ms, MYT).isoformat()


def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def add_ms(ms: int, delta_ms: int) -> int:
    return ms + delta_ms


def myt_date(ms: int):
    return ms_to_dt(ms, MYT).date()


def is_myt_monday(ms: int) -> bool:
    return ms_to_dt(ms, MYT).weekday() == 0


def floor_ms(ms: int, step: int) -> int:
    return ms - (ms % step)


def interval_ms(interval: str) -> int:
    return {"1h": HOUR_MS, "4h": H4_MS, "1d": DAY_MS}[interval]


def iso_in(ms: int, hours: float) -> str:
    return ms_to_utc_iso(ms + int(hours * HOUR_MS))


def cooldown_until(ms: int, days: int) -> int:
    return ms + int(timedelta(days=days).total_seconds() * 1000)
