#!/usr/bin/env python3
"""Walk-forward E1–E6 on 2020–2024, then one hold-out pass on 2025–2026.

Public data only. This process does not place orders. mode=live is refused.
The hold-out window is evaluated once, on the variant frozen by the
development vote. It is not a search.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from desk.config import load_settings  # noqa: E402
from desk.data.store import Store  # noqa: E402
from desk.strategies import require_research_mode  # noqa: E402
from desk.strategies.catalog import FAMILY_CONFIG, NOT_SEARCHED, VARIANTS  # noqa: E402
from desk.strategies.evaluate import (  # noqa: E402
    combine_returns,
    evaluate_benchmark,
    evaluate_family,
    summarize_combined,
)
from desk.strategies.panel import Features, load_panel  # noqa: E402
from desk.strategies.select import holdout_is_positive  # noqa: E402

DEV_START = "2020-01-01"
DEV_END = "2025-01-01"
HOLD_START = "2025-01-01"
HOLD_END = "2026-11-01"
V03_EXPECTANCY = -0.076
V03_TRADES = 10512


def _ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _pct(value, digits=1) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.{digits}f}%"


def _num(value, digits=2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _row(family: str, window: str, packed: dict) -> dict:
    keys = (
        "cagr",
        "sharpe",
        "max_dd",
        "total_return",
        "turnover",
        "fees",
        "slippage",
        "funding_pnl",
        "days_in_market",
        "days",
        "avg_names",
        "avg_gross",
        "avg_net",
        "trades",
        "expectancy_r",
        "corr_btc",
        "deflated_sharpe",
        "n_trials",
        "rebalances",
        "weeks_flat",
    )
    out = {"family": family, "variant": packed.get("variant"), "window": window}
    for key in keys:
        value = packed.get(key)
        if hasattr(value, "tolist"):
            continue
        out[key] = value
    return out


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    fields = list(rows[0].keys())
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    line = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([line, sep, *body])


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest research books E1–E6")
    parser.add_argument("--settings", default=str(ROOT / "config" / "settings.yaml"))
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--families", nargs="*", default=["E1", "E2", "E3", "E4", "E5", "E6"])
    parser.add_argument("--report-date", default="20261110")
    args = parser.parse_args()
    settings = load_settings(args.settings)
    require_research_mode(settings)
    path = Path(settings["data"]["sqlite_path"])
    if not path.is_absolute():
        path = ROOT / path
    store = Store(path)
    print("loading panel", flush=True)
    panel = load_panel(store)
    store.close()
    features = Features(panel)
    print(f"symbols={panel.n} days={panel.t} first={panel.day_ms[0]} last={panel.day_ms[-1]}", flush=True)
    dev_start, dev_end = _ms(DEV_START), _ms(DEV_END)
    hold_start, hold_end = _ms(HOLD_START), _ms(HOLD_END)
    in_days = int(settings["backtest"]["walk_forward_in_sample_days"])
    out_days = int(settings["backtest"]["walk_forward_out_sample_days"])
    reports = []
    for family in args.families:
        reports.append(
            evaluate_family(
                features,
                family,
                dev_start=dev_start,
                dev_end=dev_end,
                hold_start=hold_start,
                hold_end=hold_end,
                equity=args.equity,
                in_sample_days=in_days,
                out_sample_days=out_days,
            )
        )
    print("benchmarks", flush=True)
    btc_dev = evaluate_benchmark(features, dev_start, dev_end, args.equity, "btc")
    btc_hold = evaluate_benchmark(features, hold_start, hold_end, args.equity, "btc")
    hedge_dev = evaluate_benchmark(features, dev_start, dev_end, args.equity, "hedge")
    hedge_hold = evaluate_benchmark(features, hold_start, hold_end, args.equity, "hedge")

    survivors = []
    for report in reports:
        hold = report["holdout"]
        if holdout_is_positive(hold.get("total_return")):
            survivors.append(report)

    out_dir = ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"strategies-{args.report_date}"
    summary_rows = []
    fold_rows = []
    for report in reports:
        family = report["family"]
        for row in report["development"]:
            summary_rows.append(_row(family, "development", row))
        for variant, row in report["walk_forward"].items():
            summary_rows.append(_row(family, "walk_forward_oos", row))
        summary_rows.append(_row(family, "holdout", report["holdout"]))
        fold_rows.extend(report["fold_rows"])
    summary_rows.append(_row("BTC", "development", btc_dev))
    summary_rows.append(_row("BTC", "holdout", btc_hold))
    summary_rows.append(_row("BTC_SHORT_ALTS", "development", hedge_dev))
    summary_rows.append(_row("BTC_SHORT_ALTS", "holdout", hedge_hold))

    combo_note = "No strategy had a positive hold-out after costs, so there is no survivor book."
    combo_hold = None
    combo_beats = []
    if len(survivors) >= 2:
        paths = [report["holdout"]["returns"] for report in survivors]
        combined = summarize_combined(combine_returns(paths), args.equity, btc_hold["returns"])
        combo_hold = combined
        summary_rows.append(_row("COMBO", "holdout_survivors", combined))
        beats = []
        for report in survivors:
            own = report["holdout"]["sharpe"]
            beats.append((report["family"], own, combined["sharpe"]))
        combo_beats = beats
        combo_note = (
            "The survivor book is an equal weight of the hold-out daily returns. "
            "It was assembled after seeing which sleeves were positive, so it is not itself an out-of-sample test."
        )
    elif len(survivors) == 1:
        combo_note = f"Only {survivors[0]['family']} was positive on the hold-out, so a combined book is that sleeve alone."

    _write_csv(out_dir / f"{stem}-summary.csv", summary_rows)
    _write_csv(out_dir / f"{stem}-folds.csv", fold_rows)
    daily_rows = []
    for report in reports:
        hold = report["holdout"]
        for index, (stamp, value) in enumerate(zip(hold["day_ms"], hold["equity"], strict=False)):
            daily_rows.append(
                {
                    "family": report["family"],
                    "variant": hold["variant"],
                    "window": "holdout",
                    "day_ms": int(stamp),
                    "equity": float(value),
                }
            )
        dev = next(row for row in report["development"] if row["variant"] == report["frozen"]["variant"])
        for stamp, value in zip(dev["day_ms"], dev["equity"], strict=False):
            daily_rows.append(
                {
                    "family": report["family"],
                    "variant": dev["variant"],
                    "window": "development",
                    "day_ms": int(stamp),
                    "equity": float(value),
                }
            )
    _write_csv(out_dir / f"{stem}-equity.csv", daily_rows)

    enabled = {}
    lines = [
        f"# Strategy books {args.report_date[:4]}-{args.report_date[4:6]}-{args.report_date[6:]}",
        "",
        "## What was tested",
        "",
        "E1–E6 from the research note, as portfolio and trend books on Binance USDT-M perpetuals. "
        "Development is 2020-01-01 through 2024-12-31. Each family has a pre-declared primary and at most two alternatives. "
        f"Walk-forward uses the existing {in_days}-day in-sample and {out_days}-day out-of-sample windows, restarted flat at each window so a lucky in-sample equity does not size the next window. "
        "A non-primary variant is frozen only if it wins a strict majority of in-sample fold votes and its pooled walk-forward out-of-sample return and Sharpe are both positive. "
        "The 2025-01-01 through 2026-10-31 hold-out is run once on that frozen variant. "
        f"Starting equity is ${args.equity:,.0f}. Costs are a 0.05% taker fee per fill, 4 bps slippage on the top 10 names by trailing 30-day quote volume and 8 bps otherwise, and the archived funding prints. "
        f"v0.3 baseline, from the merged full-universe study, is {V03_EXPECTANCY:.3f}R over {V03_TRADES:,} trades. It was not re-simulated here.",
        "",
        "## Open-risk translation",
        "",
        "Weekly books size to a 1.5× ATR(14) shock, not to the 5× ATR catastrophe stop. "
        "The 5× rule is a close-only disaster brake. Using it as the risk denominator would shrink the book by another 5/1.5 and would leave the 6% budget idle. "
        "A gap through the 1.5× shock can lose more than the budgeted risk before the brake fills. "
        "Each new name requests 2% of equity (1% on the E6 fade). A cluster above 3% is scaled down first (BTC, ETH, a fixed L1 list, and the rest are separate; longs and shorts in one cluster both count). "
        "The book is then scaled so total open risk is at most 6%. Notional is clipped at 3× equity and the freed risk is not reallocated. "
        "An all-alt book therefore runs at 3% open risk, not 6%. "
        "E2 uses one gross figure per leg so the legs match before the 3× clip. "
        "E3 and E6 risk to the stop they actually exit on. E5 targets 20% annualised volatility and is then scaled down until the same caps hold. It is not scaled up to the caps.",
        "",
        "## Universe",
        "",
        "At each decision close, a name must already have a daily bar, 60 closes (200 for E5 and E6-B), trailing 30-day quote volume of at least $20m per day, and, when an open-interest print exists within 3 days, at least $5m of OI. "
        "Missing OI does not exclude a name. Stables and a fixed list of tokenized equities and index perps are removed before the top-N rank. "
        "Delisted contracts stay eligible on days they still print and drop out the day they do not. "
        "The rank and the signal use only that close and earlier bars. Orders fill at the next day's open.",
        "",
        "## Headline",
        "",
    ]
    table = []
    for report in reports:
        hold = report["holdout"]
        dev = next(row for row in report["development"] if row["variant"] == report["frozen"]["variant"])
        oos = report["walk_forward"][report["frozen"]["variant"]]
        positive = holdout_is_positive(hold.get("total_return"))
        enabled[FAMILY_CONFIG[report["family"]]] = positive
        table.append(
            [
                report["family"],
                report["frozen"]["variant"],
                _pct(dev.get("cagr")),
                _num(dev.get("sharpe")),
                _pct(dev.get("max_dd")),
                _num(oos.get("sharpe")),
                _pct(hold.get("cagr")),
                _num(hold.get("sharpe")),
                _pct(hold.get("max_dd")),
                _pct(hold.get("total_return")),
                "yes" if positive else "no",
            ]
        )
    lines.append(
        _md_table(
            ["Book", "Frozen", "Dev CAGR", "Dev Sharpe", "Dev max DD", "WF OOS Sharpe", "Hold CAGR", "Hold Sharpe", "Hold max DD", "Hold return", "Enabled"],
            table,
        )
    )
    lines.append("")
    lines.append(
        f"BTC 1x buy-and-hold, same fee and funding model: development CAGR {_pct(btc_dev.get('cagr'))}, Sharpe {_num(btc_dev.get('sharpe'))}, max DD {_pct(btc_dev.get('max_dd'))}. "
        f"Hold-out CAGR {_pct(btc_hold.get('cagr'))}, Sharpe {_num(btc_hold.get('sharpe'))}, max DD {_pct(btc_hold.get('max_dd'))}."
    )
    lines.append("")
    lines.append(
        f"Long BTC / short equal-weight liquid alts, 50/50 notional, weekly, costs on, risk cap off: "
        f"development CAGR {_pct(hedge_dev.get('cagr'))}, Sharpe {_num(hedge_dev.get('sharpe'))}, max DD {_pct(hedge_dev.get('max_dd'))}. "
        f"Hold-out CAGR {_pct(hedge_hold.get('cagr'))}, Sharpe {_num(hedge_hold.get('sharpe'))}, max DD {_pct(hedge_hold.get('max_dd'))}."
    )
    lines.append("")
    lines.append(combo_note)
    if combo_beats:
        bits = [f"{family} Sharpe {_num(own)} vs combo {_num(combo)}" for family, own, combo in combo_beats]
        lines.append("Hold-out Sharpe against the equal-weight survivor book: " + "; ".join(bits) + ".")
        if combo_hold is not None:
            lines.append(
                f"Survivor book CAGR {_pct(combo_hold.get('cagr'))}, Sharpe {_num(combo_hold.get('sharpe'))}, max DD {_pct(combo_hold.get('max_dd'))}."
            )
    lines.append("")
    lines.append("## Per book")
    lines.append("")
    for report in reports:
        family = report["family"]
        frozen = report["frozen"]["variant"]
        dev = next(row for row in report["development"] if row["variant"] == frozen)
        oos = report["walk_forward"][frozen]
        hold = report["holdout"]
        lines.append(f"### {family}")
        lines.append("")
        lines.append(f"Frozen `{frozen}`. {report['decision']['reason']}. Votes: {report['decision']['votes'] or 'none'}.")
        lines.append("")
        lines.append(
            f"Development: CAGR {_pct(dev.get('cagr'))}, Sharpe {_num(dev.get('sharpe'))}, max DD {_pct(dev.get('max_dd'))}, "
            f"turnover {_num(dev.get('turnover'))}×/year, fees ${dev.get('fees') or 0:,.0f}, slippage ${dev.get('slippage') or 0:,.0f}, "
            f"funding PnL ${dev.get('funding_pnl') or 0:,.0f}, days in market {_pct(dev.get('invested_frac'))}, "
            f"average names {_num(dev.get('avg_names'), 1)}, average gross leverage {_num(dev.get('avg_gross'), 2)}, "
            f"correlation to BTC {_num(dev.get('corr_btc'))}, trades {dev.get('trades')}, expectancy R {_num(dev.get('expectancy_r'))}."
        )
        lines.append("")
        lines.append(
            f"Walk-forward OOS (stitched, each window restarted): Sharpe {_num(oos.get('sharpe'))}, CAGR {_pct(oos.get('cagr'))}, "
            f"max DD {_pct(oos.get('max_dd'))}, total return {_pct(oos.get('total_return'))}."
        )
        lines.append("")
        lines.append(
            f"Hold-out: CAGR {_pct(hold.get('cagr'))}, Sharpe {_num(hold.get('sharpe'))}, max DD {_pct(hold.get('max_dd'))}, "
            f"total return {_pct(hold.get('total_return'))}, turnover {_num(hold.get('turnover'))}×/year, "
            f"fees ${hold.get('fees') or 0:,.0f}, slippage ${hold.get('slippage') or 0:,.0f}, funding PnL ${hold.get('funding_pnl') or 0:,.0f}, "
            f"correlation to BTC {_num(hold.get('corr_btc'))}, trades {hold.get('trades')}, expectancy R {_num(hold.get('expectancy_r'))}, "
            f"days in market {_pct(hold.get('invested_frac'))}."
        )
        lines.append("")
        lines.append(
            f"Trials in this family: {len(family_variants_safe(family))}. "
            f"Deflated Sharpe on the development path of the frozen variant: {_num(dev.get('deflated_sharpe'), 3)}. "
            f"Deflated Sharpe on the hold-out path, using the same trial count: {_num(hold.get('deflated_sharpe'), 3)}. "
            f"{NOT_SEARCHED[family]}"
        )
        lines.append("")
        variant_rows = []
        for row in report["development"]:
            variant_rows.append(
                [
                    row["variant"] + (" (frozen)" if row["variant"] == frozen else ""),
                    _pct(row.get("cagr")),
                    _num(row.get("sharpe")),
                    _pct(row.get("max_dd")),
                    _num(report["walk_forward"][row["variant"]].get("sharpe")),
                    _pct(report["walk_forward"][row["variant"]].get("total_return")),
                ]
            )
        lines.append(_md_table(["Variant", "Dev CAGR", "Dev Sharpe", "Dev max DD", "WF OOS Sharpe", "WF OOS return"], variant_rows))
        lines.append("")
    # Correlations of frozen development daily returns.
    lines.append("## Correlation")
    lines.append("")
    lines.append("Pearson correlation of daily simple returns. Development window, frozen variant.")
    lines.append("")
    names = [report["family"] for report in reports]
    matrix = []
    series = []
    for report in reports:
        dev = next(row for row in report["development"] if row["variant"] == report["frozen"]["variant"])
        series.append(dev["returns"])
    for i, name in enumerate(names):
        row = [name]
        for j in range(len(names)):
            from desk.strategies.stats import correlation

            row.append(_num(correlation(series[i], series[j])))
        btc_corr = next(row for row in reports[i]["development"] if row["variant"] == reports[i]["frozen"]["variant"]).get("corr_btc")
        row.append(_num(btc_corr))
        matrix.append(row)
    lines.append(_md_table(["", *names, "BTC"], matrix))
    lines.append("")
    lines.append("## What is enabled")
    lines.append("")
    if not any(enabled.values()):
        lines.append("Nothing. Every frozen hold-out return was at or below zero after fees, slippage, and funding. `config/settings.yaml` leaves E1–E6 disabled.")
    else:
        turned = [name for name, flag in enabled.items() if flag]
        lines.append(
            "Enabled only where the hold-out terminal return is positive after costs: "
            + ", ".join(f"`{name}`" for name in turned)
            + ". The others stay disabled. A positive hold-out on one window is not a licence to trade size."
        )
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- The 6% cap is open risk to a 1.5× ATR shock for the weekly books, and to the real stop for E3, E5, and E6. It is not a 6% stop on equity. A gap can lose more."
    )
    lines.append(
        "- Delisted versus still listed is inferred from the last Vision daily bar. A name that died in the last three days of the archive can be labelled TRADING. The backtest itself uses whether a bar exists on the decision day."
    )
    lines.append(
        "- Metrics start in September 2020. Before that, the OI floor is not applied. E5 and E6 need OI or a year of funding z-scores, so they sit flat until the inputs exist."
    )
    lines.append(
        "- Funding notional uses that day's daily close as the mark for the 08:00 and 16:00 prints. The rate is the archived settlement. The mark is not a signal."
    )
    lines.append(
        "- Slippage is a fixed 4/8 bps, not a historical book. The 2/4 bp sensitivity was not run, so the cost number is the harsher of the two schedules in the note."
    )
    lines.append(
        "- The v0.3 −0.076R figure is the merged study. This run did not reload the 1h and 4h archive."
    )
    lines.append(
        f"- {len(VARIANTS)} variants were estimated across six families. Deflated Sharpe is per family, using that family's trial count, not a joint haircut across every book."
    )
    lines.append(
        "- Restarted walk-forward windows begin flat. A trend position that would have been open is re-entered only when the rule says so again."
    )
    lines.append("")
    text = "\n".join(lines) + "\n"
    report_path = out_dir / f"{stem}.md"
    report_path.write_text(text)
    # Small machine-readable enable list for the config update. Not a second backtest.
    enable_path = out_dir / f"{stem}-enabled.csv"
    _write_csv(
        enable_path,
        [{"strategy": key, "enabled": "true" if flag else "false"} for key, flag in enabled.items()],
    )
    print(text)
    print(f"wrote {report_path}", flush=True)


def family_variants_safe(family: str) -> list:
    from desk.strategies.catalog import family_variants

    return family_variants(family)


if __name__ == "__main__":
    main()
