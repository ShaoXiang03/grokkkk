#!/usr/bin/env python3
"""Full-universe exit and Setup B study.

Scans each stored symbol once, then resimulates the portfolio for a small
pre-registered grid. Defaults in config/settings.yaml are changed only when
the adoption rule in desk.backtest.selection says the out-of-sample record
supports it. This script does not place orders.
"""

from __future__ import annotations

import argparse
import csv
import pickle
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from desk.backtest.metrics import fill_flat_days, summarize_trades  # noqa: E402
from desk.backtest.research import (  # noqa: E402
    EXIT_GRID,
    EXIT_MIN_IS_TRADES,
    OOS_MIN_TRADES,
    PULLBACK_MIN_IS_TRADES,
    SETUP_B_GRID,
    scan_stored_symbol,
    settings_for_exit,
    settings_for_pullback,
    simulate,
)
from desk.backtest.selection import adoption_decision, majority_variant, tally_fold_winners  # noqa: E402
from desk.backtest.walkforward import select_trades, walk_forward_folds  # noqa: E402
from desk.config import assert_no_live_orders, load_settings  # noqa: E402
from desk.data.store import Store  # noqa: E402
from desk.timeutils import ms_to_utc_iso  # noqa: E402

SETUPS = ("squeeze_breakout", "trend_pullback", "range_reclaim", "meme_momentum")
FIELDS = ("trade_count", "win_rate", "expectancy", "max_drawdown", "sharpe", "net_pnl")


def _sleeve(trades: list[dict], equity: float, start_ms: int, end_ms: int) -> dict:
    flows = [flow for trade in trades for flow in trade.get("flows") or []]
    from desk.backtest.metrics import equity_from_flows

    curve = equity_from_flows(equity, flows)
    if not curve:
        curve = [(start_ms, equity), (end_ms, equity)]
    curve = fill_flat_days(curve, start_ms, end_ms)
    metrics = summarize_trades(trades, curve)
    return {key: metrics[key] for key in FIELDS}


def _rows_for(variant: str, family: str, trades: list[dict], folds: list[dict], equity: float, start_ms: int, end_ms: int) -> list[dict]:
    rows = []

    def add(setup: str, direction: str, sample: str, fold: str, subset: list[dict], window_start: int, window_end: int) -> None:
        metrics = _sleeve(subset, equity, window_start, window_end)
        rows.append(
            {
                "variant": variant,
                "family": family,
                "setup": setup,
                "direction": direction,
                "sample": sample,
                "fold": fold,
                **metrics,
            }
        )

    groups = [("all", trades)] + [(setup, [t for t in trades if t["setup"] == setup]) for setup in SETUPS]
    for direction in ("all", "long", "short"):
        for setup, subset in groups:
            chosen = subset if direction == "all" else [t for t in subset if t["direction"] == direction]
            add(setup, direction, "full", "", chosen, start_ms, end_ms)
    oos_trades = []
    is_ids = set()
    is_trades = []
    for index, fold in enumerate(folds):
        oos = select_trades(trades, fold["out_of_sample"]["start_ms"], fold["out_of_sample"]["end_ms"])
        ins = select_trades(trades, fold["in_sample"]["start_ms"], fold["in_sample"]["end_ms"])
        oos_trades.extend(oos)
        for trade in ins:
            if trade["signal_id"] not in is_ids:
                is_ids.add(trade["signal_id"])
                is_trades.append(trade)
        add("all", "all", "in_sample", str(index), ins, fold["in_sample"]["start_ms"], fold["in_sample"]["end_ms"])
        add("all", "all", "out_of_sample", str(index), oos, fold["out_of_sample"]["start_ms"], fold["out_of_sample"]["end_ms"])
        pull_is = [t for t in ins if t["setup"] == "trend_pullback"]
        pull_oos = [t for t in oos if t["setup"] == "trend_pullback"]
        add("trend_pullback", "all", "in_sample", str(index), pull_is, fold["in_sample"]["start_ms"], fold["in_sample"]["end_ms"])
        add("trend_pullback", "all", "out_of_sample", str(index), pull_oos, fold["out_of_sample"]["start_ms"], fold["out_of_sample"]["end_ms"])
    if folds:
        oos_start = folds[0]["out_of_sample"]["start_ms"]
        oos_end = folds[-1]["out_of_sample"]["end_ms"]
        is_start = folds[0]["in_sample"]["start_ms"]
        is_end = folds[-1]["in_sample"]["end_ms"]
        add("all", "all", "out_of_sample", "pooled", oos_trades, oos_start, oos_end)
        add("all", "all", "in_sample", "pooled_unique", is_trades, is_start, is_end)
        for setup in ("all", *SETUPS):
            for direction in ("all", "long", "short"):
                if setup == "all" and direction == "all":
                    continue
                subset = oos_trades
                if setup != "all":
                    subset = [t for t in subset if t["setup"] == setup]
                if direction != "all":
                    subset = [t for t in subset if t["direction"] == direction]
                add(setup, direction, "out_of_sample", "pooled", subset, oos_start, oos_end)
        add(
            "trend_pullback",
            "all",
            "in_sample",
            "pooled_unique",
            [t for t in is_trades if t["setup"] == "trend_pullback"],
            is_start,
            is_end,
        )
    return rows


def _fold_score_rows(rows: list[dict], variant: str, setup: str) -> list[dict]:
    found = []
    for row in rows:
        if row["variant"] != variant or row["setup"] != setup or row["direction"] != "all":
            continue
        if row["sample"] not in ("in_sample", "out_of_sample") or row["fold"] in ("", "pooled", "pooled_unique"):
            continue
        found.append(row)
    return found


def _diagnose(trades: list[dict]) -> dict:
    rows = [trade for trade in trades if trade["setup"] == "trend_pullback"]
    reasons = Counter(trade["exit_reason"] for trade in rows)
    def mean(values):
        vals = [float(v) for v in values if v is not None]
        return sum(vals) / len(vals) if vals else None

    def bucket(predicate):
        chosen = [trade for trade in rows if predicate(trade)]
        rs = [trade["r_multiple"] for trade in chosen if trade.get("r_multiple") is not None]
        return {"trades": len(chosen), "expectancy": mean(rs), "win_rate": (sum(1 for r in rs if r > 0) / len(rs)) if rs else None}

    return {
        "trades": len(rows),
        "exit_reasons": dict(reasons),
        "partial_taken": sum(1 for trade in rows if trade.get("partial_taken")),
        "trail_before_partial": sum(1 for trade in rows if trade.get("exit_reason") == "trail_stop" and not trade.get("partial_taken")),
        "mean_mfe_r": mean(trade.get("mfe_r") for trade in rows),
        "mean_mae_r": mean(trade.get("mae_r") for trade in rows),
        "mean_stop_atr": mean(trade.get("stop_distance_atr") for trade in rows),
        "reached_1r_mfe_without_partial": sum(1 for trade in rows if (trade.get("mfe_r") or 0) >= 1 and not trade.get("partial_taken")),
        "by_side": {"long": bucket(lambda t: t["direction"] == "long"), "short": bucket(lambda t: t["direction"] == "short")},
        "by_regime": {
            name: bucket(lambda t, name=name: t.get("regime") == name)
            for name in ("long_favored", "caution", "short_favored")
        },
        "by_tag": {
            "fast_ema": bucket(lambda t: bool((t.get("tagged_fast")))),
            "mid_only": bucket(lambda t: t.get("tagged_fast") is False and t.get("tagged_mid") is True),
        },
        "gap_winners": mean(trade.get("ema_gap_fast") for trade in rows if (trade.get("r_multiple") or 0) > 0),
        "gap_losers": mean(trade.get("ema_gap_fast") for trade in rows if (trade.get("r_multiple") or 0) <= 0),
    }


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _pct(value) -> str:
    if value is None:
        return "n/a"
    return f"{100 * float(value):.1f}%"


def _num(value, digits=3) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}"


def _write_csv(path: Path, rows: list[dict]) -> None:
    fields = ["variant", "family", "setup", "direction", "sample", "fold", *FIELDS]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _fmt(row.get(key)) for key in fields})


def _lookup(rows: list[dict], **wanted) -> dict | None:
    for row in rows:
        if all(row.get(key) == value for key, value in wanted.items()):
            return row
    return None


def _md_table(rows: list[dict], columns: list[tuple[str, str]]) -> str:
    header = "| " + " | ".join(label for _, label in columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = []
    for row in rows:
        cells = []
        for key, _label in columns:
            value = row.get(key)
            if key in ("win_rate", "max_drawdown") and isinstance(value, float):
                cells.append(_pct(value))
            elif key in ("expectancy", "sharpe", "net_pnl") and isinstance(value, float):
                cells.append(_num(value))
            else:
                cells.append("" if value is None else str(value))
        body.append("| " + " | ".join(cells) + " |")
    return "\n".join([header, sep, *body])


def _build_report(context: dict) -> str:
    lines = []
    lines.append(f"# Backtest {context['date']}")
    lines.append("")
    lines.append("## What was asked")
    lines.append("")
    lines.append(
        "The first three-symbol run (BTC, ETH, SOL, about 110 days, $800) lost money. "
        "Most exits were the 4H trail firing before the 2R partial, and Setup B (trend pullback) was the worst sleeve. "
        "This study replays the same rules on the Vision USDT-M archive, then compares a small set of looser exits and four pullback filters. "
        "Nothing here places an order."
    )
    lines.append("")
    lines.append("## Data")
    lines.append("")
    lines.append(
        f"Source is `data.binance.vision` monthly USD-M klines and funding, {context['months']}. "
        f"The archive listing contains {context['symbols_listed']} USDT-M perpetual names, including contracts that later stopped printing bars. "
        f"Daily files were stored for the names Vision still hosts. "
        f"A name was downloaded on 1h, 4h, and funding only when its best UTC day had at least "
        f"${context['min_daily_quote_volume']:,.0f} of quote volume, which is half the $1M Setup D gate: "
        f"a 24h window sits inside two UTC days, so a name below that ceiling can never pass the gate. "
        f"{context['symbols_liquid']} names cleared it. {context['symbols_scanned']} of those had 1h, 4h, and 1d bars and were scanned. "
        f"{context['delisted_scanned']} of the scanned names are inferred delisted "
        f"(last daily bar more than three days before the archive's latest daily bar). "
        f"`exchangeInfo` was not used: this environment receives HTTP 451 from `fapi.binance.com`, and the archive does not include it."
    )
    lines.append("")
    lines.append(
        f"Sample span {context['span_start']} to {context['span_end']} "
        f"({context['span_days']} days, {context['fold_count']} walk-forward folds of 182 in-sample and 91 out-of-sample days). "
        f"Signals emitted after the static gates: {context['signal_count']}. "
        f"Setup D chart hits: {context['meme_hits']}. Every one of those stays rejected because on-chain checks are missing."
    )
    lines.append("")
    lines.append("## Costs and path rules")
    lines.append("")
    lines.append(
        "Every fill pays the 0.05% taker fee per side, half of the 0.05% assumed spread, and 0.01% slippage. "
        "Funding uses the settled rate both as the last settled rate and as the predicted rate "
        "(`funding_predicted_model: persist_last_settled`), because the predicted rate is not in the archive. "
        "A stop and a target in the same bar fill the stop. A stop that gaps through fills at the open. "
        "The trail updates on a closed 4H bar and can only fill on a later bar. "
        "The hard stop never moves to a worse price. A partial, once filled, moves the stop to breakeven when that tightens it. "
        "News vetoes are not modeled (`backtest.news_status: ALLOW` is an assumption). "
        "Open trades at the last bar are marked `end_of_data`."
    )
    lines.append("")
    lines.append("## Selection rule")
    lines.append("")
    lines.append(
        "This rule was written into `desk.backtest.selection` before the grid was scored. "
        "A fold votes only when a variant has enough in-sample trades "
        f"({EXIT_MIN_IS_TRADES} for an exit, {PULLBACK_MIN_IS_TRADES} for a Setup B variant). "
        "The vote goes to the highest in-sample expectancy. A tie stays on the baseline. "
        "A variant is adopted only if it wins a strict majority of voting folds and its frozen pooled out-of-sample record "
        f"has at least {OOS_MIN_TRADES} trades, positive expectancy, expectancy above the baseline, "
        "and max drawdown no more than 5 points worse than the baseline. "
        "Out-of-sample numbers are not used to pick the winner. "
        "Pooled out-of-sample trades are those entered in the non-overlapping 91-day windows. "
        "In-sample windows overlap, so the per-fold vote can see a trade in more than one in-sample window; "
        "the pooled in-sample row in the CSV counts each trade once and is descriptive only."
    )
    lines.append("")
    lines.append("Slice metrics restart at $800 and include only that slice's cashflows, with flat days filled in. ")
    lines.append("That drawdown is not the live account's drawdown while other sleeves are also open. The full-sample row is the actual portfolio path.")
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append(context["headline"])
    lines.append("")
    lines.append("## Exit grid")
    lines.append("")
    lines.append(
        "The baseline is `ema_immediate_2r`: the v0.3 4H trail (20 EMA buffered by 0.25 ATR, and close buffered by 1.5 ATR) from the first 4H close, partial 50% at 2R. "
        "`after_partial` and `after_1r` leave the hard stop alone until that event. "
        "Chandelier trails ratchet off the best 4H high or low since entry, at 2x or 3x ATR, and are armed only after 1R so they are not a second hard stop. "
        "The structure trail uses the lowest low (highest high) of the last 3 closed 4H bars, minus 0.25 ATR, and is armed after the partial. "
        "1R rows turn off `enforce_min_reward_risk` so the cost check does not reject every 1R target. That also lets a few extra entries through. It is the same switch as `--allow-below-min-rr`."
    )
    lines.append("")
    lines.append(_md_table(context["exit_table"], context["table_columns"]))
    lines.append("")
    lines.append(f"Exit decision: {context['exit_decision']}")
    lines.append("")
    lines.append("Per-fold in-sample winners: " + ", ".join(context["exit_votes"]) + ".")
    lines.append("")
    lines.append("## Setup B")
    lines.append("")
    lines.append(
        "The baseline pullback is unchanged: 4H EMA20 > EMA50 > EMA200 (mirrored for shorts), RSI inside the configured band, "
        "the bar tags EMA20 or EMA50, and the stop is the signal-bar extreme minus 0.25 ATR. "
        "The four variants each change one thing. B1 requires both EMA gaps to be at least 0.5 ATR. "
        "B2 drops the caution BTC regime. B3 places the initial stop beyond the 3-bar swing instead of the signal bar, still capped by `max_stop_atr`, and that initial stop is not a widening of a stop already in the trade. "
        "B4 requires the bar to tag EMA20, not only EMA50. "
        "These were scored on pullback trades only, with the baseline exit, so the exit grid and the pullback grid are not a joint search."
    )
    lines.append("")
    lines.append(_md_table(context["pullback_table"], context["table_columns"]))
    lines.append("")
    lines.append(f"Setup B decision: {context['pullback_decision']}")
    lines.append("")
    lines.append("Per-fold in-sample winners: " + ", ".join(context["pullback_votes"]) + ".")
    lines.append("")
    lines.append("### Why the baseline pullbacks look the way they do")
    lines.append("")
    lines.append(context["diagnosis_text"])
    lines.append("")
    lines.append("## By side, baseline exit, pooled out of sample")
    lines.append("")
    lines.append(_md_table(context["side_table"], [
        ("setup", "Setup"),
        ("direction", "Side"),
        ("trade_count", "Trades"),
        ("win_rate", "Win rate"),
        ("expectancy", "Expectancy R"),
        ("max_drawdown", "Max DD"),
        ("sharpe", "Sharpe"),
        ("net_pnl", "Net PnL"),
    ]))
    lines.append("")
    lines.append("## What is evidence, and what is not")
    lines.append("")
    lines.append(context["evidence"])
    lines.append("")
    lines.append("## Recommended settings")
    lines.append("")
    lines.append(context["recommendation"])
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    for caveat in context["caveats"]:
        lines.append(f"- {caveat}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _decision_text(decision: dict, votes: dict) -> str:
    parts = [decision["reason"] + "."]
    if votes:
        rendered = ", ".join(f"{name} {count}" for name, count in sorted(votes.items(), key=lambda item: (-item[1], item[0])))
        parts.append(f"Votes: {rendered}.")
    return " ".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full-universe exit and Setup B study")
    parser.add_argument("--settings", default=str(ROOT / "config" / "settings.yaml"))
    parser.add_argument("--cache", default=str(ROOT / "data" / "research-cache.pkl"))
    parser.add_argument("--reuse-cache", action="store_true")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--report-dir", default=str(ROOT / "reports"))
    parser.add_argument("--date", default="20261110")
    args = parser.parse_args()
    settings = load_settings(args.settings)
    assert_no_live_orders(settings)
    db_path = Path(settings["data"]["sqlite_path"])
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    cache_path = Path(args.cache)
    part_dir = cache_path.parent / "research-parts"
    if args.reuse_cache and cache_path.exists():
        payload = pickle.loads(cache_path.read_bytes())
    else:
        store = Store(db_path)
        have_1h = set(store.symbols_with_interval("1h"))
        have_4h = set(store.symbols_with_interval("4h"))
        symbols = sorted(have_1h & have_4h)
        meta = {row["symbol"]: row for row in store.list_symbols(trading_only=False)}
        store.close()
        if "BTCUSDT" not in symbols:
            print("BTCUSDT 1h/4h bars are missing. Finish scripts/backfill.py first.", file=sys.stderr)
            sys.exit(1)
        part_dir.mkdir(parents=True, exist_ok=True)
        done_map = {}
        for path in part_dir.glob("*.pkl"):
            item = pickle.loads(path.read_bytes())
            if item.get("symbol") and not item.get("error"):
                done_map[item["symbol"]] = item
        if done_map:
            print(f"resuming scan, {len(done_map)} symbols already cached", flush=True)
        pending = [symbol for symbol in symbols if symbol not in done_map]
        print(f"scanning {len(pending)} of {len(symbols)} symbols", flush=True)
        if pending:
            tasks = [(symbol, str(db_path), settings) for symbol in pending]
            finished = 0
            with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
                futures = [pool.submit(scan_stored_symbol, task) for task in tasks]
                for future in as_completed(futures):
                    item = future.result()
                    done_map[item["symbol"]] = item
                    (part_dir / f"{item['symbol']}.pkl").write_bytes(pickle.dumps(item, protocol=pickle.HIGHEST_PROTOCOL))
                    finished += 1
                    if finished % 25 == 0 or finished == len(tasks):
                        print(f"scanned {finished}/{len(pending)}", flush=True)
        payload = {"symbols": symbols, "scanned": [done_map[symbol] for symbol in symbols if symbol in done_map], "meta": meta}
        print(f"scan complete, {len(payload['scanned'])} symbols in memory", flush=True)

    signals = []
    books = {}
    spans = []
    meme_hits = 0
    errors = []
    catalog = payload.get("meta") or {}
    for item in payload["scanned"]:
        if item.get("error"):
            errors.append(f"{item['symbol']}: {item['error']}")
        meme_hits += int(item.get("meme_chart_hits") or 0)
        if item.get("span"):
            spans.append(item["span"])
        signals.extend(item.get("signals") or [])
        if item.get("book") is not None:
            books[item["symbol"]] = item["book"]
            catalog.setdefault(item["symbol"], item["book"]["meta"])
    if not spans:
        print("no klines were scanned", file=sys.stderr)
        sys.exit(1)
    start_ms = min(span[0] for span in spans)
    end_ms = max(span[1] for span in spans)
    equity = float(settings["backtest"]["initial_equity"])
    in_days = int(settings["backtest"]["walk_forward_in_sample_days"])
    out_days = int(settings["backtest"]["walk_forward_out_sample_days"])
    folds = walk_forward_folds(start_ms, end_ms, in_days, out_days)
    print(f"signals={len(signals)} books={len(books)} folds={len(folds)}", flush=True)

    all_rows = []
    results = {}
    for spec in EXIT_GRID:
        print(f"simulate {spec['name']}", flush=True)
        variant_settings = settings_for_exit(settings, spec)
        result = simulate(signals, books, variant_settings)
        results[spec["name"]] = result
        all_rows.extend(_rows_for(spec["name"], "exit", result["trades"], folds, equity, start_ms, end_ms))
    for spec in SETUP_B_GRID:
        print(f"simulate {spec['name']}", flush=True)
        variant_settings = settings_for_pullback(settings, spec)
        result = simulate(signals, books, variant_settings)
        results[spec["name"]] = result
        all_rows.extend(_rows_for(spec["name"], "setup_b", result["trades"], folds, equity, start_ms, end_ms))

    exit_fold_rows = []
    for spec in EXIT_GRID:
        exit_fold_rows.extend(_fold_score_rows(all_rows, spec["name"], "all"))
    pull_fold_rows = []
    for spec in SETUP_B_GRID:
        pull_fold_rows.extend(_fold_score_rows(all_rows, spec["name"], "trend_pullback"))
    exit_tally = tally_fold_winners([row for row in exit_fold_rows if row["sample"] == "in_sample"], "ema_immediate_2r", EXIT_MIN_IS_TRADES)
    pull_tally = tally_fold_winners([row for row in pull_fold_rows if row["sample"] == "in_sample"], "B0_baseline", PULLBACK_MIN_IS_TRADES)
    exit_winner = majority_variant(exit_tally["votes"], exit_tally["voting_folds"])
    pull_winner = majority_variant(pull_tally["votes"], pull_tally["voting_folds"])

    def pooled(variant: str, setup: str) -> dict:
        row = _lookup(all_rows, variant=variant, setup=setup, direction="all", sample="out_of_sample", fold="pooled")
        return row or {"trade_count": 0, "expectancy": None, "max_drawdown": None, "win_rate": None, "sharpe": None, "net_pnl": 0.0}

    exit_oos = {spec["name"]: pooled(spec["name"], "all") for spec in EXIT_GRID}
    pull_oos = {spec["name"]: pooled(spec["name"], "trend_pullback") for spec in SETUP_B_GRID}
    exit_decision = adoption_decision(winner=exit_winner, baseline="ema_immediate_2r", oos=exit_oos, min_oos_trades=OOS_MIN_TRADES)
    pull_decision = adoption_decision(winner=pull_winner, baseline="B0_baseline", oos=pull_oos, min_oos_trades=OOS_MIN_TRADES)

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"backtest-{args.date}"
    _write_csv(report_dir / f"{stem}.csv", all_rows)
    diagnosis = _diagnose(results["ema_immediate_2r"]["trades"])
    diag_rows = []
    for key, value in diagnosis["exit_reasons"].items():
        diag_rows.append({"variant": "ema_immediate_2r", "family": "diagnosis", "setup": "trend_pullback", "direction": key, "sample": "full", "fold": "exit_reason", "trade_count": value, "win_rate": None, "expectancy": None, "max_drawdown": None, "sharpe": None, "net_pnl": None})
    _write_csv(report_dir / f"{stem}-diagnosis.csv", diag_rows)

    def table_for(names, setup, sample, fold):
        out = []
        for name in names:
            row = _lookup(all_rows, variant=name, setup=setup, direction="all", sample=sample, fold=fold) or {}
            out.append({"variant": name, **{key: row.get(key) for key in FIELDS}})
        return out

    exit_names = [spec["name"] for spec in EXIT_GRID]
    pull_names = [spec["name"] for spec in SETUP_B_GRID]
    columns = [
        ("variant", "Variant"),
        ("trade_count", "Trades"),
        ("win_rate", "Win rate"),
        ("expectancy", "Expectancy R"),
        ("max_drawdown", "Max DD"),
        ("sharpe", "Sharpe"),
        ("net_pnl", "Net PnL"),
    ]
    base_full = _lookup(all_rows, variant="ema_immediate_2r", setup="all", direction="all", sample="full", fold="") or {}
    base_oos = exit_oos["ema_immediate_2r"]
    headline = (
        f"Baseline `ema_immediate_2r` on the full sample: {base_full.get('trade_count')} trades, "
        f"win rate {_pct(base_full.get('win_rate'))}, expectancy {_num(base_full.get('expectancy'))}R, "
        f"max drawdown {_pct(base_full.get('max_drawdown'))}, Sharpe {_num(base_full.get('sharpe'))}, "
        f"net PnL {_num(base_full.get('net_pnl'), 2)} on $800. "
        f"Pooled out of sample: {base_oos.get('trade_count')} trades, expectancy {_num(base_oos.get('expectancy'))}R, "
        f"max drawdown {_pct(base_oos.get('max_drawdown'))}, net PnL {_num(base_oos.get('net_pnl'), 2)}."
    )
    diag = diagnosis
    diagnosis_text = (
        f"Baseline pullbacks: {diag['trades']} trades. "
        f"Exit reasons: {diag['exit_reasons']}. "
        f"Partials filled: {diag['partial_taken']}. "
        f"Trail exits before the partial: {diag['trail_before_partial']}. "
        f"Trades whose favorable excursion reached 1R without a partial fill: {diag['reached_1r_mfe_without_partial']}. "
        f"Mean MFE {_num(diag['mean_mfe_r'])}R, mean MAE {_num(diag['mean_mae_r'])}R, mean stop distance {_num(diag['mean_stop_atr'])} ATR. "
        f"Long {diag['by_side']['long']}. Short {diag['by_side']['short']}. "
        f"By BTC regime: {diag['by_regime']}. "
        f"Fast-EMA tag {diag['by_tag']['fast_ema']}. Mid-EMA only {diag['by_tag']['mid_only']}. "
        f"Mean fast-EMA gap on winners {_num(diag['gap_winners'])} ATR and on losers {_num(diag['gap_losers'])} ATR. "
        "These splits describe the baseline. They were not used to add a fifth variant."
    )
    side_table = []
    for setup in ("all", *SETUPS):
        for direction in ("long", "short"):
            row = _lookup(all_rows, variant="ema_immediate_2r", setup=setup, direction=direction, sample="out_of_sample", fold="pooled") or {}
            side_table.append({"setup": setup, "direction": direction, **{key: row.get(key) for key in FIELDS}})
    scanned_names = [item["symbol"] for item in payload["scanned"] if item.get("span")]
    listing_path = ROOT / "data" / "vision-cache" / "um-usdt-perpetuals.txt"
    archive_count = 0
    if listing_path.exists():
        archive_count = len([line for line in listing_path.read_text().splitlines() if line.strip()])
    store = Store(db_path)
    meta_rows = {row["symbol"]: row for row in store.list_symbols()}
    liquid = len(store.symbols_with_interval("1h"))
    store.close()
    delisted = sum(
        1
        for symbol in scanned_names
        if str((meta_rows.get(symbol) or {}).get("status") or "") not in ("", "TRADING")
    )
    evidence_bits = []
    if exit_decision["adopt"] or pull_decision["adopt"]:
        evidence_bits.append(
            "The adoption rule fired for "
            + " and ".join(
                name
                for name, decision in (("the exit", exit_decision), ("Setup B", pull_decision))
                if decision["adopt"]
            )
            + ". That is evidence about those frozen variants on this archive and this cost model, not a promise about the next year."
        )
    else:
        evidence_bits.append(
            "No variant cleared the out-of-sample rule. "
            "A higher in-sample expectancy, a single fold, or the best out-of-sample row in the table is not evidence of an edge. "
            "The baseline's own out-of-sample expectancy is the number to quote for the rules as they stand."
        )
    evidence_bits.append(
        "Twelve exit variants and five pullback variants were scored. "
        "That is small next to a continuous parameter search, and it is still large enough that one of them can look good by chance. "
        "The majority-vote plus frozen out-of-sample check is what keeps that from becoming the default."
    )
    if not folds:
        evidence_bits.append("The sample is shorter than one 182-day window plus one 91-day window, so there is no out-of-sample figure.")
    recommendation = [
        "Leave `config/settings.yaml` on the v0.3 exit and the v0.3 pullback."
        if not exit_decision["adopt"] and not pull_decision["adopt"]
        else "Update the defaults that the rule adopted, and leave every other switch where it is."
    ]
    if exit_decision["adopt"]:
        spec = next(item for item in EXIT_GRID if item["name"] == exit_decision["variant"])
        recommendation.append(
            f"Exit default becomes trail_mode `{spec['trail_mode']}`, trail_arm `{spec['trail_arm']}`, "
            f"partial_at_r {spec['partial_at_r']}, chandelier_atr_mult {spec['chandelier_atr_mult']}, "
            f"enforce_min_reward_risk {spec['enforce_min_reward_risk']} (`{spec['name']}`)."
        )
    else:
        recommendation.append("Exit stays `trail_mode: ema`, `trail_arm: immediate`, `partial_at_r: 2.0`.")
    if pull_decision["adopt"]:
        spec = next(item for item in SETUP_B_GRID if item["name"] == pull_decision["variant"])
        recommendation.append(
            f"Setup B default becomes min_ema_gap_atr {spec['min_ema_gap_atr']}, allow_caution {spec['allow_caution']}, "
            f"stop_swing_bars {spec['stop_swing_bars']}, require_fast_ema_tag {spec['require_fast_ema_tag']} (`{spec['name']}`)."
        )
    else:
        recommendation.append(
            "Setup B stays min_ema_gap_atr 0, allow_caution true, stop_swing_bars 1, require_fast_ema_tag false."
        )
    recommendation.append(
        "The exit winner and the Setup B winner were not resimulated as one combined configuration on a fresh out-of-sample slice. "
        "Do not treat the pair as a jointly tested system."
    )
    caveats = [
        "Delisted versus still listed is inferred from the last Vision daily bar, not from exchangeInfo. A contract that died in the last three days of the archive can be labeled TRADING.",
        "Names whose best day was under the download floor are absent. The per-bar volume gate still rejects thin days of the names that were downloaded.",
        "The spread is the configured 0.05% assumption on every fill. Historical books are not in the archive. Quiet names are charged too little; violent names are charged too little as well.",
        "Predicted funding is the last settled rate, held until the next settlement. That is disclosed in the cost block and it is not the exchange's predicted rate.",
        "News vetoes and Setup D on-chain checks are not historical series. Setup D contributes no trades.",
        "Same-bar stop-before-target fills the stop when both prices print. The path inside the bar is not known.",
        "A trail computed from a 4H close is not active during that same bar. A wick through the new stop on the bar that creates it is not a fill.",
        "The research clock walks the union of hourly opens and skips hours where the account is flat and no signal is due. Flat days are filled back in before Sharpe and drawdown.",
        "Symbols are considered in alphabetical order when several signal on the same hour. That tie-break can change which name gets the last slot under the risk cap.",
        "In-sample windows overlap by design of the 182/91 split. The vote uses each fold's own window. Do not add the fold expectancies into one pooled in-sample trade count.",
        "This process refuses mode live and has no order path, no API key, and no withdrawal.",
        "A passing test and a positive in-sample expectancy are not an edge.",
    ]
    if errors:
        caveats.append("Scan errors (these symbols contributed no signals): " + "; ".join(errors[:20]))
    months = "2019-09 through 2026-10"
    context = {
        "date": f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:8]}",
        "months": months,
        "symbols_listed": archive_count,
        "min_daily_quote_volume": 500_000,
        "symbols_liquid": liquid,
        "symbols_scanned": len(scanned_names),
        "delisted_scanned": delisted,
        "span_start": ms_to_utc_iso(start_ms),
        "span_end": ms_to_utc_iso(end_ms),
        "span_days": round((end_ms - start_ms) / 86_400_000, 1),
        "fold_count": len(folds),
        "signal_count": len(signals),
        "meme_hits": meme_hits,
        "headline": headline,
        "exit_table": table_for(exit_names, "all", "out_of_sample", "pooled") + table_for(exit_names, "all", "full", ""),
        "pullback_table": table_for(pull_names, "trend_pullback", "out_of_sample", "pooled") + table_for(pull_names, "trend_pullback", "full", ""),
        "table_columns": columns,
        "exit_decision": _decision_text(exit_decision, exit_tally["votes"]),
        "pullback_decision": _decision_text(pull_decision, pull_tally["votes"]),
        "exit_votes": [f"fold {item['fold']} {item['variant']}" for item in exit_tally["winners"]] or ["none"],
        "pullback_votes": [f"fold {item['fold']} {item['variant']}" for item in pull_tally["winners"]] or ["none"],
        "diagnosis_text": diagnosis_text,
        "side_table": side_table,
        "evidence": " ".join(evidence_bits),
        "recommendation": " ".join(recommendation),
        "caveats": caveats,
    }
    # The exit table currently concatenates OOS and full without a column that says which. Split them in prose by repeating the header note.
    context["exit_table"] = []
    for sample, fold, label in (("out_of_sample", "pooled", "OOS"), ("full", "", "full")):
        for row in table_for(exit_names, "all", sample, fold):
            row = dict(row)
            row["variant"] = f"{row['variant']} ({label})"
            context["exit_table"].append(row)
    context["pullback_table"] = []
    for sample, fold, label in (("out_of_sample", "pooled", "OOS"), ("full", "", "full")):
        for row in table_for(pull_names, "trend_pullback", sample, fold):
            row = dict(row)
            row["variant"] = f"{row['variant']} ({label})"
            context["pullback_table"].append(row)
    text = _build_report(context)
    md_path = report_dir / f"{stem}.md"
    md_path.write_text(text)
    print(f"wrote {md_path}")
    print(headline)
    print(context["exit_decision"])
    print(context["pullback_decision"])
    # Machine-readable decisions for the settings update.
    (report_dir / f"{stem}-decisions.txt").write_text(
        f"exit_adopt={exit_decision['adopt']}\nexit_variant={exit_decision['variant']}\n"
        f"pullback_adopt={pull_decision['adopt']}\npullback_variant={pull_decision['variant']}\n"
    )


if __name__ == "__main__":
    main()
