"""Binance USDT-M public REST. No API key, no order routes, no withdrawals.

Request weights match the USD-M futures market-data routes this client is
allowed to call. `get` does not retry. HTTP 418 and 429 raise
`BinanceRateLimitError` so the caller can pause instead of hammering the IP.
"""

from __future__ import annotations

from typing import Any

ALLOWED_PATHS = frozenset(
    {
        "/fapi/v1/exchangeInfo",
        "/fapi/v1/klines",
        "/fapi/v1/ticker/24hr",
        "/fapi/v1/ticker/bookTicker",
        "/fapi/v1/premiumIndex",
        "/fapi/v1/fundingRate",
    }
)

_USED_WEIGHT_HEADER = "X-MBX-USED-WEIGHT-1M"


class BinancePublicError(RuntimeError):
    pass


class BinanceRateLimitError(BinancePublicError):
    """HTTP 418 or 429. The request was not retried."""

    def __init__(
        self,
        path: str,
        status_code: int,
        retry_after_seconds: float | None,
        used_weight: int | None,
        body: str,
    ):
        self.path = path
        self.status_code = int(status_code)
        self.retry_after_seconds = retry_after_seconds
        self.used_weight = used_weight
        super().__init__(f"{path} returned HTTP {status_code}: {body}")


def kline_request_weight(limit: int) -> int:
    """USD-M `/fapi/v1/klines` weight for a LIMIT.

    Below 100 is 1, 100–499 is 2, 500–1000 is 5, above 1000 is 10.
    """
    limit = int(limit)
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


def request_weight(path: str, params: dict | None = None) -> int:
    """Weight this process will charge for one allowed public GET.

    Omitting `symbol` on the ticker routes is the heavy call. This client
    does not use those. Unknown paths are refused rather than guessed.
    """
    if path not in ALLOWED_PATHS:
        raise BinancePublicError(f"refusing non-public or trading endpoint: {path}")
    params = params or {}
    if path == "/fapi/v1/klines":
        return kline_request_weight(int(params.get("limit") or 500))
    if path == "/fapi/v1/ticker/24hr":
        return 1 if params.get("symbol") else 40
    if path == "/fapi/v1/ticker/bookTicker":
        return 2 if params.get("symbol") else 5
    if path == "/fapi/v1/premiumIndex":
        return 1 if params.get("symbol") else 10
    if path in ("/fapi/v1/exchangeInfo", "/fapi/v1/fundingRate"):
        return 1
    raise BinancePublicError(f"no weight for endpoint: {path}")


def used_weight_1m(headers) -> int | None:
    """Parse `X-MBX-USED-WEIGHT-1M`. Missing or junk headers are None."""
    if not headers:
        return None
    wanted = _USED_WEIGHT_HEADER.upper()
    for key, value in headers.items():
        if str(key).upper().replace("_", "-") != wanted:
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    return None


def parse_retry_after(value) -> float | None:
    """Retry-After as seconds. HTTP-dates are accepted. Blank is None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, parsed.timestamp() - datetime.now(timezone.utc).timestamp())


class BinancePublicClient:
    """Read-only client. `get` refuses any path outside ALLOWED_PATHS.

    There is no retry loop. A 418 or 429 is raised once, with Retry-After
    and the used-weight header when Binance sent them.
    """

    def __init__(self, base_url: str = "https://fapi.binance.com", timeout: float = 20, session=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = session
        self.last_used_weight_1m: int | None = None
        self.last_status: int | None = None

    def _session_get(self):
        if self._session is not None:
            return self._session
        import requests

        self._session = requests.Session()
        return self._session

    def get(self, path: str, params: dict | None = None) -> Any:
        if path not in ALLOWED_PATHS:
            raise BinancePublicError(f"refusing non-public or trading endpoint: {path}")
        response = self._session_get().get(self.base_url + path, params=params or {}, timeout=self.timeout)
        self.last_status = getattr(response, "status_code", None)
        self.last_used_weight_1m = used_weight_1m(getattr(response, "headers", None))
        status = int(response.status_code)
        if status in (418, 429):
            raise BinanceRateLimitError(
                path,
                status,
                parse_retry_after(_header(response, "Retry-After")),
                self.last_used_weight_1m,
                getattr(response, "text", "")[:300],
            )
        if status != 200:
            body = getattr(response, "text", "")[:300]
            raise BinancePublicError(f"{path} returned HTTP {status}: {body}")
        return response.json()

    def exchange_info(self) -> dict:
        return self.get("/fapi/v1/exchangeInfo")

    def klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 1500,
    ) -> list:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        if end_ms is not None:
            params["endTime"] = int(end_ms)
        return self.get("/fapi/v1/klines", params)

    def ticker_24h(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol} if symbol else {}
        return self.get("/fapi/v1/ticker/24hr", params)

    def book_ticker(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol} if symbol else {}
        return self.get("/fapi/v1/ticker/bookTicker", params)

    def premium_index(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol} if symbol else {}
        return self.get("/fapi/v1/premiumIndex", params)

    def funding_history(self, symbol: str, start_ms: int | None = None, end_ms: int | None = None, limit: int = 1000) -> list:
        params: dict[str, Any] = {"symbol": symbol, "limit": limit}
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        if end_ms is not None:
            params["endTime"] = int(end_ms)
        return self.get("/fapi/v1/fundingRate", params)


def _header(response, name: str):
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    return getter(name)


def parse_klines(raw: list) -> list[dict]:
    rows = []
    for item in raw:
        rows.append(
            {
                "open_time": int(item[0]),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
                "close_time": int(item[6]),
                "quote_volume": float(item[7]),
            }
        )
    return rows


def usdt_m_perpetuals(exchange_info: dict, *, trading_only: bool = False) -> list[dict]:
    found = []
    for row in exchange_info.get("symbols", []):
        if str(row.get("contractType", "")).upper() != "PERPETUAL":
            continue
        if str(row.get("quoteAsset", "")).upper() != "USDT":
            continue
        if trading_only and str(row.get("status", "")).upper() != "TRADING":
            continue
        found.append(
            {
                "symbol": row.get("symbol"),
                "status": row.get("status"),
                "contract_type": row.get("contractType"),
                "base_asset": row.get("baseAsset"),
                "quote_asset": row.get("quoteAsset"),
                "margin_asset": row.get("marginAsset"),
                "onboard_date_ms": row.get("onboardDate"),
            }
        )
    return found
