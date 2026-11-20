"""News Watcher feed: loopback post, TTL, and a gate that stays fail-closed until enabled."""

import json
from pathlib import Path

import yaml

from desk.data.store import Store
from desk.paper.books import book_fresh_blocks
from desk.rules.news_feed import apply_news_post, news_verdict
from desk_helpers import make_packet, make_settings

NOW = 1_700_000_000_000
TOKEN = "news-token-not-a-secret-in-logs"


def _post(store, settings, payload, *, token=TOKEN, host="127.0.0.1", now_ms=NOW):
    body = json.dumps(payload).encode()
    return apply_news_post(
        store,
        settings,
        body,
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        now_ms=now_ms,
        client_host=host,
        bind_host="127.0.0.1",
    )


def _row(symbol="BTCUSDT", status="VETO", ttl=3600, ts=NOW):
    return {
        "symbol": symbol,
        "status": status,
        "reason": "halt headline on the venue",
        "ts_ms": ts,
        "ttl_seconds": ttl,
    }


def test_feed_is_ignored_while_disabled_and_missing_still_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_FEED_TOKEN", TOKEN)
    shipped = yaml.safe_load(Path("config/settings.yaml").read_text())
    assert shipped["account"]["news_status"] is None
    assert shipped["account"]["news_feed_enabled"] is False
    settings = make_settings(tmp_path)
    assert settings["account"]["news_feed_enabled"] is False
    store = Store(tmp_path / "desk.sqlite")
    status, payload = _post(store, settings, _row(status="ALLOW"))
    assert status == 200
    assert payload == {"ok": True, "stored": 1}
    allowed, reason = news_verdict(settings, store, NOW, ["BTCUSDT"], None)
    assert allowed is False
    assert reason == "News Watcher status is missing"
    text = json.dumps(payload)
    assert TOKEN not in text
    store.close()


def test_enabled_feed_allows_only_an_unexpired_allow(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_FEED_TOKEN", TOKEN)
    settings = make_settings(tmp_path)
    settings["account"]["news_feed_enabled"] = True
    store = Store(tmp_path / "desk.sqlite")
    missing, reason = news_verdict(settings, store, NOW, ["BTCUSDT"], None)
    assert missing is False and reason == "News Watcher status is missing"
    _post(store, settings, _row(symbol="*", status="ALLOW", ttl=60))
    ok, reason = news_verdict(settings, store, NOW + 30_000, ["ETHUSDT"], None)
    assert ok is True and reason == "News Watcher ALLOW"
    expired, reason = news_verdict(settings, store, NOW + 60_000, ["ETHUSDT"], None)
    assert expired is False and reason == "News Watcher status is missing"
    _post(store, settings, _row(symbol="BTCUSDT", status="VETO", ttl=600))
    blocked, reason = news_verdict(settings, store, NOW, ["BTCUSDT"], None)
    assert blocked is False
    assert "halt headline" in reason
    other, _reason = news_verdict(settings, store, NOW, ["ETHUSDT"], None)
    assert other is True
    caution = _post(store, settings, _row(symbol="ETHUSDT", status="CAUTION", ttl=600))
    assert caution[0] == 200
    caution_ok, _reason = news_verdict(settings, store, NOW, ["ETHUSDT"], None)
    assert caution_ok is False
    store.close()


def test_book_gate_uses_the_feed_only_after_it_is_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_FEED_TOKEN", TOKEN)
    settings = make_settings(tmp_path)
    store = Store(tmp_path / "desk.sqlite")
    _post(store, settings, _row(symbol="*", status="ALLOW"))
    packet = make_packet(symbol="BTCUSDT")
    account = {"equity_usdt": 800.0, "daily_pnl_pct": 0.0, "weekly_pnl_pct": 0.0, "news_status": None}
    assert "news" in book_fresh_blocks(packet, account, [], settings, NOW, store=store)
    settings["account"]["news_feed_enabled"] = True
    assert "news" not in book_fresh_blocks(packet, account, [], settings, NOW, store=store)
    _post(store, settings, _row(symbol="BTCUSDT", status="VETO"))
    assert "news" in book_fresh_blocks(packet, account, [], settings, NOW, store=store)
    store.close()


def test_post_requires_the_token_and_loopback(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_FEED_TOKEN", TOKEN)
    settings = make_settings(tmp_path)
    store = Store(tmp_path / "desk.sqlite")
    denied = _post(store, settings, _row(), token="wrong-token")
    assert denied[0] == 401
    assert store.get_news_feed("BTCUSDT") is None
    assert TOKEN not in json.dumps(denied[1])
    remote = _post(store, settings, _row(), host="10.1.1.1")
    assert remote[0] == 403
    assert store.get_news_feed("BTCUSDT") is None
    monkeypatch.delenv("NEWS_FEED_TOKEN")
    missing = _post(store, settings, _row(), token="")
    assert missing[0] == 503
    store.close()
