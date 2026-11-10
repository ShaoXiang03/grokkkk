"""Pick one variant from walk-forward in-sample windows.

The rule is fixed before the results are read. Out-of-sample numbers are not
an input. A tie stays on the baseline so a noisy sample does not move a default.
"""

from __future__ import annotations


def tally_fold_winners(fold_rows: list[dict], baseline: str, min_trades: int) -> dict:
    """Each fold votes for the eligible variant with the highest in-sample expectancy.

    A variant is eligible when its in-sample trade count is at least `min_trades`.
    Folds with no eligible variant do not vote. Equal expectancy keeps the baseline
    when the baseline is eligible, otherwise the first of the tied names.
    """
    by_fold: dict[int, list[dict]] = {}
    for row in fold_rows:
        if row.get("sample") not in (None, "in_sample"):
            continue
        by_fold.setdefault(int(row["fold"]), []).append(row)
    votes: dict[str, int] = {}
    winners: list[dict] = []
    for fold in sorted(by_fold):
        eligible = [row for row in by_fold[fold] if int(row["trade_count"]) >= min_trades and row.get("expectancy") is not None]
        if not eligible:
            continue
        best = max(float(row["expectancy"]) for row in eligible)
        tied = [row for row in eligible if float(row["expectancy"]) == best]
        chosen = next((row for row in tied if row["variant"] == baseline), tied[0])
        votes[chosen["variant"]] = votes.get(chosen["variant"], 0) + 1
        winners.append({"fold": fold, "variant": chosen["variant"], "expectancy": float(chosen["expectancy"]), "trade_count": int(chosen["trade_count"])})
    return {"voting_folds": len(winners), "votes": votes, "winners": winners}


def majority_variant(votes: dict[str, int], voting_folds: int) -> str | None:
    if voting_folds <= 0:
        return None
    needed = voting_folds / 2.0
    leaders = [name for name, count in votes.items() if count > needed]
    if len(leaders) != 1:
        return None
    return leaders[0]


def adoption_decision(
    *,
    winner: str | None,
    baseline: str,
    oos: dict[str, dict],
    min_oos_trades: int,
    dd_slack: float = 0.05,
) -> dict:
    """Adopt `winner` only when its frozen out-of-sample record clears every check.

    `oos` maps a variant name to the pooled out-of-sample metrics. Those metrics
    must already be restricted to out-of-sample entries. This function does not
    look at in-sample expectancy again.
    """
    if winner is None:
        return {"adopt": False, "variant": baseline, "reason": "no in-sample variant won a strict majority of voting folds"}
    if winner == baseline:
        return {"adopt": False, "variant": baseline, "reason": "baseline won the in-sample vote; defaults stay"}
    chosen = oos.get(winner) or {}
    base = oos.get(baseline) or {}
    trades = int(chosen.get("trade_count") or 0)
    if trades < min_oos_trades:
        return {
            "adopt": False,
            "variant": baseline,
            "reason": f"{winner} won in-sample but out-of-sample has {trades} trades, below {min_oos_trades}",
        }
    exp = chosen.get("expectancy")
    base_exp = base.get("expectancy")
    if exp is None or not (float(exp) > 0):
        return {
            "adopt": False,
            "variant": baseline,
            "reason": f"{winner} won in-sample but out-of-sample expectancy is not positive",
        }
    if base_exp is not None and not (float(exp) > float(base_exp)):
        return {
            "adopt": False,
            "variant": baseline,
            "reason": f"{winner} out-of-sample expectancy does not beat the baseline",
        }
    dd = chosen.get("max_drawdown")
    base_dd = base.get("max_drawdown")
    if dd is not None and base_dd is not None and float(dd) > float(base_dd) + dd_slack:
        return {
            "adopt": False,
            "variant": baseline,
            "reason": (
                f"{winner} out-of-sample max drawdown {float(dd):.3f} is more than "
                f"{dd_slack:.2f} above the baseline {float(base_dd):.3f}"
            ),
        }
    return {
        "adopt": True,
        "variant": winner,
        "reason": f"{winner} won a strict majority of in-sample folds and its frozen out-of-sample record beats the baseline",
    }
