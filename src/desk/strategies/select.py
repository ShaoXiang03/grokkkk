"""Freeze one variant per family before the hold-out is opened.

A fold votes for the highest in-sample Sharpe among variants with at least
30 invested days. The primary wins a tie. A non-primary variant is frozen
only when it has a strict majority of the voting folds and its pooled
walk-forward out-of-sample return and Sharpe are both positive. Otherwise
the pre-declared primary stays, including when that primary is itself
negative out of sample. Out-of-sample numbers are not a reason to promote
a variant that lost the in-sample vote.
"""

from __future__ import annotations

from collections import Counter


def fold_winner(rows: list[dict], primary_id: str, min_invested_days: int = 30) -> str | None:
    eligible = [
        row
        for row in rows
        if row.get("sharpe") is not None and int(row.get("invested_days") or 0) >= min_invested_days
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda row: (float(row["sharpe"]), 1 if row["variant"] == primary_id else 0))["variant"]


def choose_frozen(votes: list[str | None], pooled: dict[str, dict], primary_id: str) -> dict:
    """Return the frozen variant id and the vote record.

    ``pooled[variant]`` has ``total_return`` and ``sharpe`` for the stitched
    walk-forward out-of-sample path.
    """
    cast = [vote for vote in votes if vote]
    counts = Counter(cast)
    record = {
        "primary": primary_id,
        "votes": dict(counts),
        "voting_folds": len(cast),
        "frozen": primary_id,
        "reason": "primary kept",
    }
    if not cast:
        record["reason"] = "no fold had enough invested days to vote; primary kept"
        return record
    winner, votes_for = counts.most_common(1)[0]
    majority = votes_for * 2 > len(cast)
    record["in_sample_winner"] = winner
    record["majority"] = majority
    if not majority or winner == primary_id:
        if majority and winner == primary_id:
            record["reason"] = "primary won a strict majority of in-sample folds"
        else:
            record["reason"] = "no strict in-sample majority; primary kept"
        return record
    stats = pooled.get(winner) or {}
    oos_return = stats.get("total_return")
    oos_sharpe = stats.get("sharpe")
    if oos_return is not None and oos_return > 0 and oos_sharpe is not None and oos_sharpe > 0:
        record["frozen"] = winner
        record["reason"] = "non-primary won a strict majority and its pooled walk-forward OOS was positive"
        return record
    record["reason"] = "in-sample majority failed the pooled OOS check; primary kept"
    return record


def holdout_is_positive(total_return: float | None) -> bool:
    """Enabled only when terminal wealth is above the start after costs."""
    return total_return is not None and total_return > 0
