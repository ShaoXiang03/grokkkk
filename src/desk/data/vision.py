"""Binance public data archive (data.binance.vision), not a trading API.

Monthly USD-M futures kline and funding files. Used when fapi.binance.com
refuses the caller (HTTP 451) or when a historical file is the available source.
Missing months are skipped. This module does not send orders.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone

VISION = "https://data.binance.vision/data/futures/um/monthly"


def iter_months(start_ms: int, end_ms: int) -> list[str]:
    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    year, month = start.year, start.month
    months = []
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            month = 1
            year += 1
    return months


def kline_url(symbol: str, interval: str, month: str) -> str:
    return f"{VISION}/klines/{symbol}/{interval}/{symbol}-{interval}-{month}.zip"


def funding_url(symbol: str, month: str) -> str:
    return f"{VISION}/fundingRate/{symbol}/{symbol}-fundingRate-{month}.zip"


def parse_kline_csv(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("open_time"):
            continue
        parts = line.split(",")
        if not parts[0].isdigit():
            continue
        rows.append(
            {
                "open_time": int(parts[0]),
                "open": float(parts[1]),
                "high": float(parts[2]),
                "low": float(parts[3]),
                "close": float(parts[4]),
                "volume": float(parts[5]),
                "close_time": int(parts[6]),
                "quote_volume": float(parts[7]),
            }
        )
    return rows


def parse_funding_csv(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("calc_time") or line.startswith("fundingTime"):
            continue
        parts = line.split(",")
        if not parts[0].lstrip("-").isdigit():
            continue
        # Vision files are calc_time, funding_interval_hours, last_funding_rate.
        rate = float(parts[2] if len(parts) > 2 else parts[1])
        rows.append({"funding_time": int(float(parts[0])), "funding_rate": rate})
    return rows


def _zip_text(payload: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = archive.namelist()[0]
        return archive.read(name).decode("utf-8")


def fetch_bytes(url: str, session, timeout: float) -> bytes | None:
    response = session.get(url, timeout=timeout)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise RuntimeError(f"{url} returned HTTP {response.status_code}")
    return response.content


def load_klines_month(session, symbol: str, interval: str, month: str, timeout: float) -> list[dict] | None:
    payload = fetch_bytes(kline_url(symbol, interval, month), session, timeout)
    if payload is None:
        return None
    return parse_kline_csv(_zip_text(payload))


def load_funding_month(session, symbol: str, month: str, timeout: float) -> list[dict] | None:
    payload = fetch_bytes(funding_url(symbol, month), session, timeout)
    if payload is None:
        return None
    return parse_funding_csv(_zip_text(payload))
