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


def premium_url(symbol: str, interval: str, month: str) -> str:
    return f"{VISION}/premiumIndexKlines/{symbol}/{interval}/{symbol}-{interval}-{month}.zip"


def metrics_url(symbol: str, day: str) -> str:
    """Daily 5-minute metrics file. `day` is YYYY-MM-DD."""
    return f"https://data.binance.vision/data/futures/um/daily/metrics/{symbol}/{symbol}-metrics-{day}.zip"


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
        # Older two-column files are fundingTime, fundingRate. Missing interval is 8h.
        if len(parts) > 2:
            interval = float(parts[1])
            rate = float(parts[2])
        else:
            interval = 8.0
            rate = float(parts[1])
        rows.append(
            {
                "funding_time": int(float(parts[0])),
                "funding_rate": rate,
                "interval_hours": interval,
            }
        )
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


def is_um_usdt_perpetual_symbol(name: str) -> bool:
    """USD-M perpetual names have no delivery suffix. USDC and dated contracts are out."""
    if not name or "_" in name:
        return False
    return name.endswith("USDT") and name.isalnum()


def list_common_prefixes(session, prefix: str, timeout: float) -> list[str]:
    """List S3 common prefixes under data.binance.vision. Public read, no credentials."""
    import xml.etree.ElementTree as ET

    ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
    base = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
    marker = None
    found: list[str] = []
    while True:
        params = {"delimiter": "/", "prefix": prefix, "max-keys": "1000"}
        if marker:
            params["marker"] = marker
        response = session.get(base, params=params, timeout=timeout)
        if response.status_code != 200:
            raise RuntimeError(f"vision listing returned HTTP {response.status_code}")
        root = ET.fromstring(response.content)
        for node in root.findall("s:CommonPrefixes/s:Prefix", ns):
            text = node.text or ""
            name = text[len(prefix) :].strip("/")
            if name:
                found.append(name)
        truncated = root.findtext("s:IsTruncated", default="false", namespaces=ns)
        if truncated != "true":
            break
        marker = root.findtext("s:NextMarker", default=None, namespaces=ns)
        if not marker:
            break
    return found


def list_um_usdt_perpetuals(session, timeout: float) -> list[str]:
    prefix = "data/futures/um/monthly/klines/"
    names = list_common_prefixes(session, prefix, timeout)
    return sorted(name for name in names if is_um_usdt_perpetual_symbol(name))


def cache_file(cache_dir, kind: str, symbol: str, month: str, interval: str | None = None):
    from pathlib import Path

    root = Path(cache_dir)
    if kind == "funding":
        return root / "funding" / symbol / f"{month}.zip"
    if kind == "premium":
        if not interval:
            raise ValueError("interval is required for premium cache files")
        return root / "premium" / symbol / interval / f"{month}.zip"
    if not interval:
        raise ValueError("interval is required for kline cache files")
    return root / "klines" / symbol / interval / f"{month}.zip"


def load_cached_zip(session, url: str, path, timeout: float) -> bytes | None:
    """Return zip bytes, using a file cache. A `.missing` marker remembers HTTP 404."""
    from pathlib import Path

    path = Path(path)
    missing = path.with_suffix(path.suffix + ".missing")
    if missing.exists():
        return None
    if path.exists() and path.stat().st_size > 0:
        return path.read_bytes()
    payload = fetch_bytes(url, session, timeout)
    path.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        missing.write_bytes(b"")
        return None
    path.write_bytes(payload)
    return payload


def load_klines_month_cached(session, cache_dir, symbol: str, interval: str, month: str, timeout: float) -> list[dict] | None:
    path = cache_file(cache_dir, "klines", symbol, month, interval)
    payload = load_cached_zip(session, kline_url(symbol, interval, month), path, timeout)
    if payload is None:
        return None
    try:
        return parse_kline_csv(_zip_text(payload))
    except (zipfile.BadZipFile, IndexError, UnicodeDecodeError, ValueError):
        path.unlink(missing_ok=True)
        raise


def load_funding_month_cached(session, cache_dir, symbol: str, month: str, timeout: float) -> list[dict] | None:
    path = cache_file(cache_dir, "funding", symbol, month)
    payload = load_cached_zip(session, funding_url(symbol, month), path, timeout)
    if payload is None:
        return None
    try:
        return parse_funding_csv(_zip_text(payload))
    except (zipfile.BadZipFile, IndexError, UnicodeDecodeError, ValueError):
        path.unlink(missing_ok=True)
        raise


def load_premium_month_cached(session, cache_dir, symbol: str, interval: str, month: str, timeout: float) -> list[dict] | None:
    path = cache_file(cache_dir, "premium", symbol, month, interval)
    payload = load_cached_zip(session, premium_url(symbol, interval, month), path, timeout)
    if payload is None:
        return None
    try:
        return parse_kline_csv(_zip_text(payload))
    except (zipfile.BadZipFile, IndexError, UnicodeDecodeError, ValueError):
        path.unlink(missing_ok=True)
        raise


def parse_metric_time(text: str) -> int:
    """Vision metrics use 'YYYY-MM-DD HH:MM:SS' or a millisecond timestamp."""
    raw = text.strip()
    numeric = raw.replace(".", "", 1)
    if numeric.isdigit():
        value = int(float(raw))
        if value < 10_000_000_000:
            value *= 1000
        return value
    parsed = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _metric_float(text: str) -> float | None:
    text = text.strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if value != value:  # NaN
        return None
    return value


def parse_metrics_csv(text: str) -> list[dict]:
    """Deduplicate by create_time and keep the last complete observation.

    A row is complete when both open-interest columns parse. An incomplete
    duplicate does not replace a complete one at the same timestamp.
    """
    order: list[int] = []
    by_time: dict[int, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("create_time"):
            continue
        parts = line.split(",")
        if len(parts) < 4:
            continue
        try:
            created = parse_metric_time(parts[0])
        except (ValueError, IndexError):
            continue
        oi = _metric_float(parts[2])
        oi_value = _metric_float(parts[3])
        if oi is None or oi_value is None:
            continue
        if created not in by_time:
            order.append(created)
        by_time[created] = {
            "create_time": created,
            "sum_open_interest": oi,
            "sum_open_interest_value": oi_value,
            "count_long_short_ratio": _metric_float(parts[6]) if len(parts) > 6 else None,
            "sum_taker_long_short_vol_ratio": _metric_float(parts[7]) if len(parts) > 7 else None,
        }
    return [by_time[stamp] for stamp in order]


def metrics_last_snapshot(rows: list[dict]) -> dict | None:
    """Last complete snapshot in a daily file, with the UTC day floored."""
    if not rows:
        return None
    last = rows[-1]
    for row in rows:
        if row["create_time"] >= last["create_time"]:
            last = row
    day_ms = last["create_time"] - (last["create_time"] % 86_400_000)
    return {**last, "day_ms": day_ms}
