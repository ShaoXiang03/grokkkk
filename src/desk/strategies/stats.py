"""Return statistics for a daily equity curve. Sharpe uses sample stdev and √365."""

from __future__ import annotations

import math

import numpy as np

EULER = 0.5772156649015329


def max_drawdown(equity: np.ndarray) -> float | None:
    if equity.size == 0:
        return None
    peak = float(equity[0])
    worst = 0.0
    for value in equity:
        price = float(value)
        if price > peak:
            peak = price
        if peak > 0:
            worst = max(worst, (peak - price) / peak)
    return worst


def daily_returns(equity: np.ndarray) -> np.ndarray:
    if equity.size < 2:
        return np.array([], dtype=float)
    prev = equity[:-1]
    out = np.full(equity.size - 1, np.nan)
    ok = prev > 0
    out[ok] = equity[1:][ok] / prev[ok] - 1.0
    return out


def sharpe(returns: np.ndarray) -> float | None:
    clean = returns[np.isfinite(returns)]
    if clean.size < 2:
        return None
    stdev = float(np.std(clean, ddof=1))
    if stdev == 0:
        return None
    return float(np.mean(clean) / stdev * math.sqrt(365.0))


def cagr(start_equity: float, end_equity: float, n_days: int) -> float | None:
    if start_equity <= 0 or end_equity <= 0 or n_days <= 1:
        return None
    return float((end_equity / start_equity) ** (365.0 / n_days) - 1.0)


def correlation(left: np.ndarray, right: np.ndarray) -> float | None:
    n = min(left.size, right.size)
    if n < 3:
        return None
    a = left[:n]
    b = right[:n]
    mask = np.isfinite(a) & np.isfinite(b)
    if int(mask.sum()) < 3:
        return None
    a = a[mask]
    b = b[mask]
    if float(np.std(a)) == 0 or float(np.std(b)) == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Acklam's rational approximation of the inverse standard normal CDF."""
    if p <= 0.0 or p >= 1.0:
        raise ValueError("probability must be in (0, 1)")
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    ]
    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )


def _moment_skew_kurt(returns: np.ndarray) -> tuple[float, float]:
    clean = returns[np.isfinite(returns)]
    if clean.size < 4:
        return 0.0, 3.0
    mean = float(np.mean(clean))
    centered = clean - mean
    m2 = float(np.mean(centered**2))
    if m2 <= 0:
        return 0.0, 3.0
    m3 = float(np.mean(centered**3))
    m4 = float(np.mean(centered**4))
    skew = m3 / (m2**1.5)
    kurt = m4 / (m2**2)
    return skew, kurt


def deflated_sharpe(returns: np.ndarray, trial_sharpes_annual: list[float]) -> float | None:
    """Probability the true daily Sharpe clears the expected maximum of the trial set.

    Bailey and López de Prado. ``trial_sharpes_annual`` includes the strategy
    being tested. One trial sets the benchmark Sharpe to zero. The Sharpe
    inside the formula is the non-annualized daily Sharpe.
    """
    clean = returns[np.isfinite(returns)]
    n_obs = int(clean.size)
    if n_obs < 10:
        return None
    stdev = float(np.std(clean, ddof=1))
    if stdev == 0:
        return None
    sr = float(np.mean(clean) / stdev)
    trials = [float(value) for value in trial_sharpes_annual if value is not None and np.isfinite(value)]
    n_trials = len(trials)
    if n_trials <= 1:
        sr0 = 0.0
    else:
        daily = [value / math.sqrt(365.0) for value in trials]
        variance = float(np.var(daily, ddof=1))
        if variance <= 0:
            sr0 = 0.0
        else:
            z1 = _norm_ppf(1.0 - 1.0 / n_trials)
            z2 = _norm_ppf(1.0 - (1.0 / n_trials) * math.exp(-1.0))
            sr0 = math.sqrt(variance) * ((1.0 - EULER) * z1 + EULER * z2)
    skew, kurt = _moment_skew_kurt(clean)
    inside = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr
    if inside <= 0 or not np.isfinite(inside):
        return None
    z = (sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(inside)
    if not np.isfinite(z):
        return None
    return _norm_cdf(z)
