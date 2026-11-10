"""Setup D on-chain checks. The scanner has no on-chain source, so these stay MISSING.

Any FAIL or MISSING fails the gate closed. PASS is only possible when a caller
supplies the six fields. Market Bot's later market_context.onchain_checks object
is a separate placeholder and is not filled here.
"""

from __future__ import annotations

ONCHAIN_CHECKS: tuple[str, ...] = (
    "top10_holders_le_25",
    "no_single_wallet_ge_10",
    "lp_locked_or_burned",
    "not_honeypot",
    "mint_disabled_or_renounced",
    "tax_le_10",
)

PASS = "PASS"
FAIL = "FAIL"
MISSING = "MISSING"


def normalize_onchain(raw: dict | None) -> dict[str, str]:
    source = raw or {}
    checks: dict[str, str] = {}
    for name in ONCHAIN_CHECKS:
        value = source.get(name, MISSING)
        if value in (True, PASS, "pass"):
            checks[name] = PASS
        elif value in (False, FAIL, "fail"):
            checks[name] = FAIL
        else:
            checks[name] = MISSING
    return checks


def onchain_gate(raw: dict | None) -> dict:
    checks = normalize_onchain(raw)
    blocking = [name for name, value in checks.items() if value != PASS]
    return {
        "checks": checks,
        "pass": not blocking,
        "blocking": blocking,
    }
