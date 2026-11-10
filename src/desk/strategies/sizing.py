"""Desk risk caps for a portfolio book.

Open-risk translation
---------------------
The desk risks 2% of equity per name, 6% across the book, and 3% inside a
correlated cluster, with at most 3x isolated notional on a name.

Weekly long/short books (E1, E2, E4) do not have a tight stop. Their 5× ATR
rule is a catastrophe brake, not the risk denominator. The shared protocol
says to use a 1.5× daily ATR shock when the book has no hard stop. Open risk
is therefore::

    notional * 1.5 * ATR(14) / price

Sizing to the 5× catastrophe distance instead would shrink the book by
another 5/1.5 and would leave the 6% budget unused. A gap larger than the
1.5× shock can lose more than the budgeted risk before the 5× brake fills.
That gap risk is disclosed, not hidden by sizing to the wider stop.

Books with a real stop (E3 trail, E5 trail, E6 hard stop) risk to that stop.

Allocation order for equal-risk books
--------------------------------------
1. Each new name requests its risk fraction (2%, or 1% on the E6 fade),
   already capped at 2% of equity.
2. If a cluster's summed open risk exceeds 3%, that cluster is scaled down.
   Longs and shorts in the same cluster both count. BTC, ETH, the L1 list,
   and the rest are separate clusters, so an all-alt book is limited to 3%
   open risk, not 6%.
3. If the book still exceeds 6%, every name is scaled by the same factor.
4. Notional is risk / stop fraction, then clipped at 3× equity. The clip
   reduces that name's risk. Freed risk is not given to other names.

Keeps (a trend position already open) are scaled only if they themselves
breach a cap. New entries then use whatever room is left. A name below
0.5% of equity after the clip is skipped, matching the scanner's
``min_risk_after_cap_pct``.

Inverse-vol books (E2) use one gross figure per leg so the two legs have
the same notional before the 3× clip. The gross is the largest value that
satisfies every cap at once.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

MAX_NAME_RISK = 0.02
MAX_TOTAL_RISK = 0.06
MAX_CLUSTER_RISK = 0.03
MAX_LEVERAGE = 3.0
MIN_RISK_AFTER_CAP = 0.005
SHOCK_ATR = 1.5
CATASTROPHE_ATR = 5.0


@dataclass
class Sized:
    symbol: str
    side: int
    notional: float
    stop_pct: float


def _cap_risk_map(risk: dict[str, float], clusters: dict[str, str], equity: float, room_total: float, room_cluster: dict[str, float]) -> dict[str, float]:
    """Scale `risk` (positive dollars) into the remaining total and cluster room."""
    if equity <= 0 or not risk:
        return {}
    out = dict(risk)
    by_cluster: dict[str, list[str]] = defaultdict(list)
    for symbol in out:
        by_cluster[clusters.get(symbol, "alts")].append(symbol)
    for cluster, members in by_cluster.items():
        cap = room_cluster.get(cluster, MAX_CLUSTER_RISK * equity)
        total = sum(out[symbol] for symbol in members)
        if cap <= 0:
            for symbol in members:
                out[symbol] = 0.0
            continue
        if total > cap > 0:
            scale = cap / total
            for symbol in members:
                out[symbol] *= scale
    total = sum(out.values())
    if room_total <= 0:
        return {symbol: 0.0 for symbol in out}
    if total > room_total:
        scale = room_total / total
        for symbol in out:
            out[symbol] *= scale
    return out


def notionals_from_risk(
    rows: list[dict],
    equity: float,
    *,
    used_total: float = 0.0,
    used_cluster: dict[str, float] | None = None,
) -> list[Sized]:
    """Equal-risk (or already-computed per-name risk dollars) to signed notionals.

    Each row needs symbol, side, stop_pct, cluster, and risk_fraction.
    """
    if equity <= 0:
        return []
    used_cluster = used_cluster or {}
    room_total = max(0.0, MAX_TOTAL_RISK * equity - used_total)
    room_cluster = {}
    clusters = {}
    requested: dict[str, float] = {}
    meta = {}
    for row in rows:
        symbol = row["symbol"]
        stop_pct = float(row["stop_pct"])
        if not np.isfinite(stop_pct) or stop_pct <= 0:
            continue
        fraction = min(float(row.get("risk_fraction", MAX_NAME_RISK)), MAX_NAME_RISK)
        if fraction <= 0:
            continue
        clusters[symbol] = row.get("cluster") or "alts"
        requested[symbol] = fraction * equity
        meta[symbol] = row
        if clusters[symbol] not in room_cluster:
            room_cluster[clusters[symbol]] = max(0.0, MAX_CLUSTER_RISK * equity - float(used_cluster.get(clusters[symbol], 0.0)))
    risk = _cap_risk_map(requested, clusters, equity, room_total, room_cluster)
    sized: list[Sized] = []
    max_notional = MAX_LEVERAGE * equity
    for symbol, dollars in risk.items():
        if dollars <= 0:
            continue
        row = meta[symbol]
        stop_pct = float(row["stop_pct"])
        notional = dollars / stop_pct
        if notional > max_notional:
            notional = max_notional
            dollars = notional * stop_pct
        if dollars < MIN_RISK_AFTER_CAP * equity:
            continue
        side = 1 if int(row["side"]) >= 0 else -1
        sized.append(Sized(symbol=symbol, side=side, notional=notional, stop_pct=stop_pct))
    return sized


def inverse_vol_balanced(rows: list[dict], equity: float) -> list[Sized]:
    """One gross notional per leg, weights proportional to raw_weight, caps applied together.

    Both legs get the same gross before a per-name 3× clip. A name that hits
    3× is reduced and the leftover gross is not reallocated, so the legs can
    differ slightly after that clip.
    """
    if equity <= 0 or not rows:
        return []
    legs: dict[int, list[dict]] = {1: [], -1: []}
    for row in rows:
        side = 1 if int(row["side"]) >= 0 else -1
        weight = float(row.get("raw_weight") or 0.0)
        stop_pct = float(row["stop_pct"])
        if weight <= 0 or not np.isfinite(stop_pct) or stop_pct <= 0:
            continue
        legs[side].append(row)
    if not legs[1] or not legs[-1]:
        return []

    def _weights(group: list[dict]) -> list[tuple[dict, float]]:
        total = sum(float(row["raw_weight"]) for row in group)
        if total <= 0:
            return []
        return [(row, float(row["raw_weight"]) / total) for row in group]

    long_w = _weights(legs[1])
    short_w = _weights(legs[-1])
    if not long_w or not short_w:
        return []
    weighted = [(1, row, weight) for row, weight in long_w] + [(-1, row, weight) for row, weight in short_w]

    # Largest gross S such that every cap holds. notional_i = S * weight_i.
    limit = float("inf")
    max_notional = MAX_LEVERAGE * equity
    cluster_risk_per_s: dict[str, float] = defaultdict(float)
    total_risk_per_s = 0.0
    for _side, row, weight in weighted:
        stop_pct = float(row["stop_pct"])
        risk_per = weight * stop_pct
        total_risk_per_s += risk_per
        cluster_risk_per_s[row.get("cluster") or "alts"] += risk_per
        name_cap = min(float(row.get("risk_fraction", MAX_NAME_RISK)), MAX_NAME_RISK) * equity
        if risk_per > 0:
            limit = min(limit, name_cap / risk_per)
        if weight > 0:
            limit = min(limit, max_notional / weight)
    if total_risk_per_s > 0:
        limit = min(limit, (MAX_TOTAL_RISK * equity) / total_risk_per_s)
    for risk_per in cluster_risk_per_s.values():
        if risk_per > 0:
            limit = min(limit, (MAX_CLUSTER_RISK * equity) / risk_per)
    if not np.isfinite(limit) or limit <= 0:
        return []
    sized: list[Sized] = []
    for side, row, weight in weighted:
        notional = limit * weight
        if notional > max_notional:
            notional = max_notional
        dollars = notional * float(row["stop_pct"])
        if dollars < MIN_RISK_AFTER_CAP * equity:
            continue
        sized.append(Sized(symbol=row["symbol"], side=side, notional=notional, stop_pct=float(row["stop_pct"])))
    return sized


def scale_to_caps(rows: list[dict], equity: float) -> list[Sized]:
    """Scale a set of raw positive weights down until the desk caps hold.

    ``raw_weight`` is a positive relative weight. Notional starts at
    ``equity * raw_weight`` (the caller has already applied any vol target)
    and is only scaled down.
    """
    if equity <= 0 or not rows:
        return []
    prepared = []
    for row in rows:
        weight = float(row.get("raw_weight") or 0.0)
        stop_pct = float(row["stop_pct"])
        if weight <= 0 or not np.isfinite(stop_pct) or stop_pct <= 0:
            continue
        prepared.append((row, weight))
    if not prepared:
        return []
    # risk per unit of the weight sum, with notional = equity * weight.
    limit = 1.0
    max_notional = MAX_LEVERAGE * equity
    cluster_risk: dict[str, float] = defaultdict(float)
    total_risk = 0.0
    for row, weight in prepared:
        stop_pct = float(row["stop_pct"])
        notional = equity * weight
        dollars = notional * stop_pct
        total_risk += dollars
        cluster_risk[row.get("cluster") or "alts"] += dollars
        name_cap = min(float(row.get("risk_fraction", MAX_NAME_RISK)), MAX_NAME_RISK) * equity
        if dollars > 0:
            limit = min(limit, name_cap / dollars)
        if notional > 0:
            limit = min(limit, max_notional / notional)
    if total_risk > 0:
        limit = min(limit, (MAX_TOTAL_RISK * equity) / total_risk)
    for dollars in cluster_risk.values():
        if dollars > 0:
            limit = min(limit, (MAX_CLUSTER_RISK * equity) / dollars)
    if limit < 0:
        limit = 0.0
    sized: list[Sized] = []
    for row, weight in prepared:
        notional = equity * weight * limit
        dollars = notional * float(row["stop_pct"])
        if dollars < MIN_RISK_AFTER_CAP * equity:
            continue
        side = 1 if int(row["side"]) >= 0 else -1
        sized.append(Sized(symbol=row["symbol"], side=side, notional=notional, stop_pct=float(row["stop_pct"])))
    return sized


def greedy_fill(rows: list[dict], equity: float, *, used_total: float = 0.0, used_cluster: dict[str, float] | None = None) -> list[Sized]:
    """Take names in descending score, each at its full risk fraction, until the caps fill."""
    if equity <= 0:
        return []
    used_cluster = dict(used_cluster or {})
    room_total = max(0.0, MAX_TOTAL_RISK * equity - used_total)
    room_cluster: dict[str, float] = defaultdict(lambda: MAX_CLUSTER_RISK * equity)
    for cluster, used in used_cluster.items():
        room_cluster[cluster] = max(0.0, MAX_CLUSTER_RISK * equity - used)
    ordered = sorted(rows, key=lambda row: (-float(row.get("score") or 0.0), row["symbol"]))
    sized: list[Sized] = []
    max_notional = MAX_LEVERAGE * equity
    for row in ordered:
        stop_pct = float(row["stop_pct"])
        if not np.isfinite(stop_pct) or stop_pct <= 0:
            continue
        cluster = row.get("cluster") or "alts"
        fraction = min(float(row.get("risk_fraction", MAX_NAME_RISK)), MAX_NAME_RISK)
        dollars = min(fraction * equity, room_total, room_cluster[cluster])
        if dollars < MIN_RISK_AFTER_CAP * equity:
            continue
        notional = dollars / stop_pct
        if notional > max_notional:
            notional = max_notional
            dollars = notional * stop_pct
        if dollars < MIN_RISK_AFTER_CAP * equity:
            continue
        side = 1 if int(row["side"]) >= 0 else -1
        sized.append(Sized(symbol=row["symbol"], side=side, notional=notional, stop_pct=stop_pct))
        room_total -= dollars
        room_cluster[cluster] -= dollars
    return sized
