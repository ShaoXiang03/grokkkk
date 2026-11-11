"""Budget, incremental daily refresh, and the daily-close staleness gate."""

from datetime import datetime, timezone

import pytest

from desk.data.binance import (
    BinancePublicClient,
    BinancePublicError,
    BinanceRateLimitError,
    request_weight,
)
from desk.data.budget import (
    ABSOLUTE_MAX_WEIGHT_PER_MINUTE,
    DEFAULT_418_PAUSE_MS,
    DEFAULT_429_PAUSE_MS,
    WeightBudget,
)
from desk.data.paper_refresh import PaperRefresh
from desk.data.store import Store
from desk.paper.freshness import data_is_fresh
from desk.timeutils import DAY_MS, HOUR_MS
from desk_helpers import make_settings

NOW = int(datetime(2026, 11, 11, 12, tzinfo=timezone.utc).timestamp() * 1000)
OCT31 = int(datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp() * 1000)
NOV1 = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp() * 1000)
NOV10 = int(datetime(2026, 11, 10, tzinfo=timezone.utc).timestamp() * 1000)


def _ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _bar(open_ms: int, price: float = 100.0) -> dict:
    return {
        "open_time": int(open_ms),
        "open": price,
        "high": price + 1,
        "low": price - 1,
        "close": price,
        "volume": 10.0,
        "close_time": int(open_ms) + DAY_MS - 1,
        "quote_volume": 30_000_000.0,
    }


def _seed(store, symbol: str, open_ms: int) -> None:
    store.upsert_klines(symbol, "1d", [_bar(open_ms)])


class _Client:
    def __init__(self, rows=None, error=None, used=1):
        self.rows = [] if rows is None else rows
        self.error = error
        self.used = used
        self.calls = []
        self.last_used_weight_1m = None

    def funding_history(self, symbol, start_ms=None, end_ms=None, limit=1000):
        self.calls.append({"symbol": symbol, "kind": "funding", "start_ms": start_ms, "limit": limit})
        self.last_used_weight_1m = self.used
        if self.error is not None:
            raise self.error
        return [{"fundingTime": NOW - HOUR_MS, "fundingRate": "0.0001"}]

    def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=1500):
        self.calls.append(
            {"symbol": symbol, "interval": interval, "start_ms": start_ms, "end_ms": end_ms, "limit": limit}
        )
        self.last_used_weight_1m = self.used
        if self.error is not None:
            raise self.error
        return list(self.rows)


def _refresh(tmp_path, client, kline_fetch, **budget_kw):
    settings = make_settings(tmp_path)
    budget = WeightBudget(**budget_kw) if budget_kw else WeightBudget(max_weight_per_minute=300)
    refresher = PaperRefresh(
        settings,
        budget=budget,
        client=client,
        kline_fetch=kline_fetch,
        sleep=lambda _seconds: None,
    )
    return settings, refresher


def _vision_through(last_day: str):
    def fetch(symbol, day):
        if day > last_day:
            return None
        return [_bar(_ms(day), price=101.0)]

    return fetch


def test_budget_caps_a_rolling_minute_and_then_releases():
    budget = WeightBudget(max_weight_per_minute=300)
    assert budget.allow(300, 0) == (True, "ok")
    budget.charge(300, 0)
    assert budget.allow(1, 0) == (False, "budget")
    assert budget.allow(1, 59_999) == (False, "budget")
    assert budget.allow(1, 60_000) == (True, "ok")
    assert budget.spent(60_000) == 0


def test_budget_backs_off_above_1200_used_weight_and_not_at_1200():
    budget = WeightBudget(pause_above=1200)
    budget.observe_used_weight(1200, 1_000)
    assert budget.allow(1, 1_000) == (True, "ok")
    budget.observe_used_weight(1201, 1_000)
    assert budget.pause_reason == "used_weight"
    assert budget.allow(1, 1_000) == (False, "used_weight")
    assert budget.allow(1, 1_000 + 60_000) == (True, "ok")


def test_budget_honors_retry_after_and_does_not_clear_early():
    budget = WeightBudget()
    budget.observe_rejection(429, 30, 5_000)
    assert budget.allow(1, 5_000) == (False, "http_429")
    assert budget.allow(1, 5_000 + 29_999) == (False, "http_429")
    assert budget.allow(1, 5_000 + 30_000) == (True, "ok")


def test_budget_pauses_when_retry_after_is_missing():
    denied = WeightBudget()
    denied.observe_rejection(429, None, 0)
    assert denied.pause_until_ms == DEFAULT_429_PAUSE_MS
    assert denied.allow(1, 1_000)[0] is False
    banned = WeightBudget()
    banned.observe_rejection(418, None, 0)
    assert banned.pause_until_ms == DEFAULT_418_PAUSE_MS
    assert banned.allow(1, 60_000)[0] is False


def test_settings_weight_is_clamped_below_the_exchange_limit():
    budget = WeightBudget.from_settings({"data": {"request_weight_per_minute": 50_000, "used_weight_pause": 1200}})
    assert budget.max_weight == ABSOLUTE_MAX_WEIGHT_PER_MINUTE
    assert ABSOLUTE_MAX_WEIGHT_PER_MINUTE <= 600
    shipped = WeightBudget.from_settings({})
    assert shipped.max_weight == 300


def test_request_weights_match_the_public_routes():
    assert request_weight("/fapi/v1/klines", {"limit": 99}) == 1
    assert request_weight("/fapi/v1/klines", {"limit": 100}) == 2
    assert request_weight("/fapi/v1/klines", {"limit": 500}) == 5
    assert request_weight("/fapi/v1/klines", {"limit": 1001}) == 10
    assert request_weight("/fapi/v1/ticker/24hr", {}) == 40
    assert request_weight("/fapi/v1/ticker/24hr", {"symbol": "BTCUSDT"}) == 1
    assert request_weight("/fapi/v1/ticker/bookTicker", {}) == 5
    assert request_weight("/fapi/v1/ticker/bookTicker", {"symbol": "BTCUSDT"}) == 2
    assert request_weight("/fapi/v1/premiumIndex", {}) == 10
    assert request_weight("/fapi/v1/exchangeInfo", {}) == 1
    with pytest.raises(BinancePublicError):
        request_weight("/fapi/v1/order", {})


def test_client_does_not_retry_a_429_and_reads_the_weight_header():
    class Session:
        def __init__(self):
            self.calls = 0

        def get(self, url, params=None, timeout=None):
            self.calls += 1
            return _Response(
                429,
                {"X-MBX-USED-WEIGHT-1M": "1800", "Retry-After": "15"},
                "Too many requests; current limit of IP is 2400 requests per minute",
            )

    session = Session()
    client = BinancePublicClient(session=session)
    with pytest.raises(BinanceRateLimitError) as caught:
        client.get("/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1d", "limit": 10})
    assert session.calls == 1
    assert caught.value.status_code == 429
    assert caught.value.retry_after_seconds == 15
    assert caught.value.used_weight == 1800
    assert client.last_used_weight_1m == 1800

    class Ok:
        def get(self, url, params=None, timeout=None):
            return _Response(200, {"X-MBX-USED-WEIGHT-1M": "4"}, "")

    ok = BinancePublicClient(session=Ok())
    assert ok.get("/fapi/v1/exchangeInfo") == {"symbols": []}
    assert ok.last_used_weight_1m == 4


def test_client_does_not_retry_a_418():
    class Session:
        def __init__(self):
            self.calls = 0

        def get(self, url, params=None, timeout=None):
            self.calls += 1
            return _Response(418, {"Retry-After": "120"}, "banned")

    session = Session()
    client = BinancePublicClient(session=session)
    with pytest.raises(BinanceRateLimitError) as caught:
        client.klines("BTCUSDT", "1d", limit=10)
    assert session.calls == 1
    assert caught.value.status_code == 418
    assert caught.value.retry_after_seconds == 120


class _Response:
    def __init__(self, status, headers, text):
        self.status_code = status
        self.headers = headers
        self.text = text

    def json(self):
        return {"symbols": []}


def test_incremental_refresh_uses_vision_and_does_not_call_fapi(tmp_path):
    store = Store(tmp_path / "desk.sqlite")
    _seed(store, "BTCUSDT", OCT31)
    client = _Client()
    _settings, refresher = _refresh(tmp_path, client, _vision_through("2026-11-10"))
    first = refresher.run_slice(store, ["BTCUSDT"], NOW, max_wall_ms=10**9, max_vision_files=1000)
    assert client.calls == []
    assert first["fapi_requests"] == 0
    assert first["vision_rows"] == 10
    assert store.latest_open_time("BTCUSDT", "1d") == NOV10
    second = refresher.run_slice(store, ["BTCUSDT"], NOW, max_wall_ms=10**9, max_vision_files=1000)
    assert second["http"] == 0
    assert client.calls == []
    store.close()


def test_fapi_is_only_the_missing_tail_and_drops_a_forming_bar(tmp_path):
    store = Store(tmp_path / "desk.sqlite")
    _seed(store, "BTCUSDT", OCT31)
    forming = _bar(NOV10 + DAY_MS, price=999.0)
    forming["close_time"] = NOW + DAY_MS
    client = _Client(rows=[_bar(NOV10, price=50.0), forming])
    _settings, refresher = _refresh(tmp_path, client, _vision_through("2026-11-09"))
    summary = refresher.run_slice(store, ["BTCUSDT"], NOW, max_wall_ms=10**9, max_vision_files=1000)
    assert summary["fapi_requests"] == 1
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["interval"] == "1d"
    assert call["limit"] < 100
    assert call["start_ms"] == NOV10
    assert store.latest_open_time("BTCUSDT", "1d") == NOV10
    frame = store.klines("BTCUSDT", "1d")
    assert 999.0 not in set(frame["close"])
    again = refresher.run_slice(store, ["BTCUSDT"], NOW, max_wall_ms=10**9, max_vision_files=1000)
    assert again["fapi_requests"] == 0
    assert len(client.calls) == 1
    store.close()


def test_429_pauses_the_slice_and_does_not_call_again(tmp_path):
    store = Store(tmp_path / "desk.sqlite")
    _seed(store, "BTCUSDT", OCT31)
    error = BinanceRateLimitError("/fapi/v1/klines", 429, 30, 100, "Too many requests")
    client = _Client(error=error, used=100)
    _settings, refresher = _refresh(tmp_path, client, _vision_through("2026-11-09"))
    summary = refresher.run_slice(store, ["BTCUSDT"], NOW, max_wall_ms=10**9, max_vision_files=1000)
    assert summary["paused"] is True
    assert summary["pause_reason"] == "http_429"
    assert len(client.calls) == 1
    second = refresher.run_slice(store, ["BTCUSDT"], NOW, max_wall_ms=10**9, max_vision_files=1000)
    assert second["fapi_requests"] == 0
    assert len(client.calls) == 1
    assert refresher.budget.allow(1, NOW)[0] is False
    assert refresher.budget.allow(1, NOW + 30_000)[0] is True
    store.close()


def test_418_pauses_without_raising(tmp_path):
    store = Store(tmp_path / "desk.sqlite")
    _seed(store, "ETHUSDT", OCT31)
    error = BinanceRateLimitError("/fapi/v1/klines", 418, 600, 2400, "banned")
    client = _Client(error=error, used=2400)
    _settings, refresher = _refresh(tmp_path, client, _vision_through("2026-11-09"))
    summary = refresher.run_slice(store, ["ETHUSDT"], NOW, max_wall_ms=10**9, max_vision_files=1000)
    assert summary["paused"] is True
    assert summary["pause_reason"] == "http_418"
    assert len(client.calls) == 1
    store.close()


def test_high_used_weight_stops_the_next_fapi_call(tmp_path):
    store = Store(tmp_path / "desk.sqlite")
    _seed(store, "BTCUSDT", OCT31)
    _seed(store, "ETHUSDT", OCT31)
    client = _Client(rows=[_bar(NOV10)], used=1500)
    _settings, refresher = _refresh(tmp_path, client, _vision_through("2026-11-09"))
    summary = refresher.run_slice(
        store, ["BTCUSDT", "ETHUSDT"], NOW, max_wall_ms=10**9, max_vision_files=1000
    )
    assert len(client.calls) == 1
    assert summary["paused"] is True
    assert summary["pause_reason"] == "used_weight"
    store.close()


def test_weight_budget_stops_a_second_fapi_call_inside_the_minute(tmp_path):
    store = Store(tmp_path / "desk.sqlite")
    _seed(store, "BTCUSDT", OCT31)
    _seed(store, "ETHUSDT", OCT31)
    client = _Client(rows=[_bar(NOV10)], used=1)
    _settings, refresher = _refresh(
        tmp_path, client, _vision_through("2026-11-09"), max_weight_per_minute=1
    )
    summary = refresher.run_slice(
        store, ["BTCUSDT", "ETHUSDT"], NOW, max_wall_ms=10**9, max_vision_files=1000
    )
    assert len(client.calls) == 1
    assert summary["paused"] is True
    assert summary["pause_reason"] == "budget"
    assert summary["vision_rows"] == 18
    store.close()


def test_funding_fapi_runs_only_for_an_open_name(tmp_path):
    store = Store(tmp_path / "desk.sqlite")
    _seed(store, "BTCUSDT", NOV10)
    _seed(store, "ETHUSDT", NOV10)
    store.save_book_position("e3", "BTCUSDT", "{}", NOW)
    client = _Client()
    _settings, refresher = _refresh(tmp_path, client, _vision_through("2026-11-10"))
    first = refresher.run_slice(store, ["BTCUSDT", "ETHUSDT"], NOW, max_wall_ms=10**9, max_vision_files=50)
    funding = [call for call in client.calls if call.get("kind") == "funding"]
    assert [call["symbol"] for call in funding] == ["BTCUSDT"]
    assert funding[0]["limit"] == 1000
    assert first["fapi_requests"] == 1
    frame = store.funding("BTCUSDT")
    assert len(frame) == 1
    second = refresher.run_slice(store, ["BTCUSDT", "ETHUSDT"], NOW, max_wall_ms=10**9, max_vision_files=50)
    assert second["fapi_requests"] == 0
    assert len(client.calls) == 1
    store.close()


def test_vision_failure_does_not_raise_and_a_dead_name_does_not_keep_calling_fapi(tmp_path):
    store = Store(tmp_path / "desk.sqlite")
    _seed(store, "DEADUSDT", OCT31)

    def boom(symbol, day):
        raise RuntimeError("vision down")

    client = _Client(rows=[])
    _settings, refresher = _refresh(tmp_path, client, boom)
    summary = refresher.run_slice(store, ["DEADUSDT"], NOW, max_wall_ms=10**9, max_vision_files=20)
    assert summary["errors"] >= 1
    assert client.calls == []

    def missing(symbol, day):
        return None

    client = _Client(rows=[])
    _settings, refresher = _refresh(tmp_path, client, missing)
    summary = refresher.run_slice(store, ["DEADUSDT"], NOW, max_wall_ms=10**9, max_vision_files=1000)
    assert summary["inactive"] == 1
    assert len(client.calls) == 1
    assert store.symbol_row("DEADUSDT")["status"] == "DELISTED"
    again = refresher.run_slice(store, ["DEADUSDT"], NOW, max_wall_ms=10**9, max_vision_files=1000)
    assert again["fapi_requests"] == 0
    assert len(client.calls) == 1
    store.close()


def _daily_settings(tmp_path):
    settings = make_settings(tmp_path)
    for cfg in settings["setups"].values():
        cfg["enabled"] = False
    return settings


def test_daily_close_is_fresh_for_hours_and_stale_when_the_close_is_missing(tmp_path):
    settings = _daily_settings(tmp_path)
    store = Store(tmp_path / "desk.sqlite")
    _seed(store, "BTCUSDT", NOV10)
    fresh, reason = data_is_fresh(store, ["BTCUSDT"], NOW, settings)
    assert fresh, reason
    # Two hours after the next midnight the Nov 10 bar is no longer the latest close.
    later = NOV10 + 2 * DAY_MS + 2 * HOUR_MS
    stale, reason = data_is_fresh(store, ["BTCUSDT"], later, settings)
    assert stale is False
    assert "1d" in reason
    # A 1h horizon must not be what fails a daily book that has today's close.
    assert settings["paper"]["max_data_age_seconds"] == 5400
    store.close()


def test_delisted_name_does_not_fail_the_daily_gate(tmp_path):
    settings = _daily_settings(tmp_path)
    store = Store(tmp_path / "desk.sqlite")
    _seed(store, "BTCUSDT", NOV10)
    _seed(store, "OLDUSDT", OCT31)
    store.upsert_symbols(
        [
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "contract_type": "PERPETUAL",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "margin_asset": "USDT",
                "onboard_date_ms": OCT31,
            },
            {
                "symbol": "OLDUSDT",
                "status": "DELISTED",
                "contract_type": "PERPETUAL",
                "base_asset": "OLD",
                "quote_asset": "USDT",
                "margin_asset": "USDT",
                "onboard_date_ms": OCT31,
            },
        ],
        NOW,
    )
    fresh, reason = data_is_fresh(store, ["BTCUSDT", "OLDUSDT"], NOW, settings)
    assert fresh, reason
    _seed(store, "ETHUSDT", OCT31)
    store.upsert_symbols(
        [
            {
                "symbol": "ETHUSDT",
                "status": "TRADING",
                "contract_type": "PERPETUAL",
                "base_asset": "ETH",
                "quote_asset": "USDT",
                "margin_asset": "USDT",
                "onboard_date_ms": OCT31,
            }
        ],
        NOW,
    )
    stale, reason = data_is_fresh(store, ["BTCUSDT", "ETHUSDT"], NOW, settings)
    assert stale is False
    assert "ETHUSDT" in reason
    store.close()


def test_intraday_setups_still_use_the_1h_horizon(tmp_path):
    settings = make_settings(tmp_path)
    assert settings["setups"]["squeeze_breakout"]["enabled"] is True
    store = Store(tmp_path / "desk.sqlite")
    store.upsert_klines(
        "BTCUSDT",
        "1h",
        [
            {
                "open_time": NOW - 2 * HOUR_MS,
                "close_time": NOW - HOUR_MS,
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
                "quote_volume": 1,
            }
        ],
    )
    fresh, _reason = data_is_fresh(store, ["BTCUSDT"], NOW, settings)
    assert fresh is True
    old = NOW + 10 * HOUR_MS
    stale, reason = data_is_fresh(store, ["BTCUSDT"], old, settings)
    assert stale is False
    assert "1h" in reason
    store.close()
