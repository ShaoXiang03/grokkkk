"""One-shot scan over the local store. No order placement."""

from __future__ import annotations

import time
from pathlib import Path

from desk.config import assert_no_live_orders, load_settings
from desk.data.binance import BinancePublicClient
from desk.data.store import Store
from desk.data.sync import backfill_symbol, sync_exchange_info, sync_live_quotes
from desk.journal.log import record
from desk.scanner.engine import evaluate_frame_at
from desk.scanner.webhook import send_packet
from desk.timeutils import DAY_MS


def open_store(settings: dict) -> Store:
    path = Path(settings["data"]["sqlite_path"])
    if not path.is_absolute():
        path = Path.cwd() / path
    return Store(path)


def account_from_settings(settings: dict) -> dict:
    account = settings["account"]
    if not account.get("use_config_snapshot"):
        return {"equity_usdt": None, "daily_pnl_pct": None, "weekly_pnl_pct": None, "news_status": None}
    return {
        "equity_usdt": account.get("equity_usdt"),
        "daily_pnl_pct": account.get("daily_pnl_pct"),
        "weekly_pnl_pct": account.get("weekly_pnl_pct"),
        "news_status": account.get("news_status"),
    }


def scan_once(settings: dict, store: Store, *, symbols: list[str] | None = None, now_ms: int | None = None) -> list[dict]:
    assert_no_live_orders(settings)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    chosen = symbols or settings["data"].get("symbols") or []
    if not chosen:
        chosen = [row["symbol"] for row in store.list_symbols(trading_only=True)]
    btc = store.klines("BTCUSDT", "1d")
    account = account_from_settings(settings)
    blocked = set(settings["blocklist"].get("symbols") or []) | store.blocklist_symbols()
    available = bool(settings["blocklist"].get("available", True))
    packets: list[dict] = []
    for symbol in chosen:
        df1h = store.klines(symbol, "1h", end_ms=now)
        df4h = store.klines(symbol, "4h", end_ms=now)
        df1d = store.klines(symbol, "1d", end_ms=now)
        if df1h.empty or df4h.empty:
            continue
        meta = store.symbol_row(symbol) or {}
        book = store.book(symbol) or {}
        snap = store.funding_snapshot(symbol) or {}
        settled = store.funding(symbol)
        last_settled = None if settled.empty else float(settled.iloc[-1]["funding_rate"])
        market = {
            "symbol": symbol,
            "status": meta.get("status", "TRADING"),
            "contract_type": meta.get("contract_type", "PERPETUAL"),
            "quote_asset": meta.get("quote_asset", "USDT"),
            "base_asset": meta.get("base_asset"),
            "onboard_date_ms": meta.get("onboard_date_ms"),
            "bid": book.get("bid"),
            "ask": book.get("ask"),
            "funding_rate": last_settled,
            "predicted_funding_rate": snap.get("predicted_rate"),
            "funding_predicted_source": "premium_index" if snap.get("predicted_rate") is not None else "missing",
            "quote_volume_24h": None,
        }
        ticker = store.ticker(symbol)
        if ticker and ticker.get("quote_volume") is not None:
            market["quote_volume_24h"] = float(ticker["quote_volume"])
        found = evaluate_frame_at(
            symbol=symbol,
            df1h=df1h,
            df4h=df4h,
            df1d=df1d,
            btc_1d=btc,
            i_1h=len(df1h) - 1,
            settings=settings,
            market=market,
            account=account,
            open_positions=[],
            cooldown_until_ms=store.cooldown_until(symbol),
            blocklist_symbols=blocked,
            blocklist_available=available,
            onchain_raw=None,
            historical=False,
        )
        for packet in found:
            if store.has_signal(packet["signal_id"]) or store.has_dedupe(packet["dedupe_key"]):
                continue
            store.save_signal(packet)
            record(store, now, "signal", {"signal_id": packet["signal_id"], "status": packet["status"]})
            if settings["webhook"].get("enabled"):
                result = send_packet(packet, settings)
                record(store, now, "webhook", {"signal_id": packet["signal_id"], **result})
            packets.append(packet)
    return packets


def refresh_symbols(settings: dict, store: Store, symbols: list[str], *, days: int, daily_days: int) -> None:
    client = BinancePublicClient(
        settings["data"]["rest_base"],
        timeout=float(settings["data"]["request_timeout_seconds"]),
    )
    now_ms = int(time.time() * 1000)
    sync_exchange_info(client, store, now_ms)
    for symbol in symbols:
        backfill_symbol(
            client,
            store,
            symbol,
            now_ms=now_ms,
            days=days,
            daily_days=daily_days,
            limit=int(settings["data"]["kline_limit"]),
            pause=float(settings["data"]["request_pause_seconds"]),
        )
        sync_live_quotes(client, store, symbol, now_ms)


def default_backfill_start(now_ms: int, days: int) -> int:
    return now_ms - days * DAY_MS
