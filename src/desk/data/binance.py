"""Binance USDT-M public REST. No API key, no order routes, no withdrawals."""

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


class BinancePublicError(RuntimeError):
    pass


class BinancePublicClient:
    """Read-only client. `get` refuses any path outside ALLOWED_PATHS."""

    def __init__(self, base_url: str = "https://fapi.binance.com", timeout: float = 20, session=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = session

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
        if response.status_code != 200:
            body = getattr(response, "text", "")[:300]
            raise BinancePublicError(f"{path} returned HTTP {response.status_code}: {body}")
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
