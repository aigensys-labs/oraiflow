"""Deterministic recorded-leverage indicators for ORAIflow.

This is an educational snapshot of data held by ORAIflow, not a credit score,
creditworthiness decision, or a complete household balance sheet. It includes
active deposit-account balances and registered loan liabilities only.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

METHODOLOGY_VERSION = "0.6.4.2"


def _as_minor(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _bps(numerator: int, denominator: int) -> int | None:
    if denominator <= 0:
        return None
    return int(
        (Decimal(numerator) * Decimal(10_000) / Decimal(denominator)).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )


def _status_for_debt_to_assets(debt_to_assets_bps: int | None, *, assets_minor: int) -> str:
    """Product heuristic labels; thresholds are not regulatory/credit-score bands."""
    if assets_minor <= 0 or debt_to_assets_bps is None:
        return "INSUFFICIENT_DATA"
    if debt_to_assets_bps == 10_000:
        return "LIABILITIES_MATCH_ASSETS"
    if debt_to_assets_bps > 10_000:
        return "LIABILITIES_EXCEED_ASSETS"
    if debt_to_assets_bps < 2_500:
        return "LOW_LEVERAGE"
    if debt_to_assets_bps < 5_000:
        return "MODERATE_LEVERAGE"
    if debt_to_assets_bps < 7_500:
        return "ELEVATED_LEVERAGE"
    return "HIGH_LEVERAGE"


def calculate_recorded_financial_health(
    accounts: Iterable[Mapping[str, Any]],
    loans: Iterable[Mapping[str, Any]],
    *,
    include_demo_loans: bool = False,
) -> dict[str, Any]:
    """Calculate recorded leverage by currency without cross-currency netting.

    Positive active deposit balances are recorded assets. Negative active
    balances are treated as recorded overdraft liabilities. Outstanding
    principal from the registered loan catalogue is treated as loan liability.
    Only the authenticated records supplied by the caller may be passed in.
    """
    balances: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "assets": 0,
            "overdrafts": 0,
            "loans": 0,
        }
    )

    for account in accounts:
        currency = str(account.get("currency") or "").strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            continue
        if str(account.get("status") or "ACTIVE").upper() != "ACTIVE":
            continue
        amount = _as_minor(account.get("balance_minor"))
        if amount >= 0:
            balances[currency]["assets"] += amount
        else:
            balances[currency]["overdrafts"] += abs(amount)

    for loan in loans:
        currency = str(loan.get("currency") or "").strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            continue
        # Synthetic illustrative records are excluded unless an operator
        # explicitly enables demo-loan presentation. Real (non-demo) loan
        # records remain eligible regardless of that demonstration switch.
        if loan.get("demo_only") is True and not include_demo_loans:
            continue
        outstanding = max(
            0, _as_minor(loan.get("outstanding_principal_minor", loan.get("principal_minor")))
        )
        balances[currency]["loans"] += outstanding

    positions: list[dict[str, Any]] = []
    for currency in sorted(balances):
        entry = balances[currency]
        assets = entry["assets"]
        loan_liabilities = entry["loans"]
        overdrafts = entry["overdrafts"]
        total_debt = loan_liabilities + overdrafts
        equity = assets - total_debt
        debt_to_assets = _bps(total_debt, assets)
        debt_to_equity = _bps(loan_liabilities, equity) if equity > 0 else None
        status = _status_for_debt_to_assets(debt_to_assets, assets_minor=assets)
        positions.append(
            {
                "currency": currency,
                "recorded_deposit_assets_minor": assets,
                "loan_liabilities_minor": loan_liabilities,
                "overdraft_liabilities_minor": overdrafts,
                "total_recorded_liabilities_minor": total_debt,
                "recorded_equity_minor": equity,
                "loan_to_equity_bps": debt_to_equity,
                "debt_to_assets_bps": debt_to_assets,
                "status": status,
                "status_basis": "TOTAL_RECORDED_LIABILITIES_TO_RECORDED_DEPOSIT_ASSETS",
            }
        )

    return {
        "methodology_version": METHODOLOGY_VERSION,
        "positions": positions,
        "coverage": {
            "included": [
                "active ORAIflow deposit accounts",
                "registered non-demo loan outstanding principal",
            ]
            + (
                ["opt-in illustrative demo loan outstanding principal"]
                if include_demo_loans
                else []
            ),
            "excluded": [
                "property",
                "external bank accounts",
                "investments",
                "pensions",
                "unrecorded debts",
                "income and monthly debt service",
            ],
            "is_complete_household_balance_sheet": False,
            "is_credit_score": False,
            "status_thresholds": {
                "type": "ORAIflow educational heuristics based on total recorded liabilities / recorded deposit assets",
                "low_leverage_below_bps": 2500,
                "moderate_leverage_below_bps": 5000,
                "elevated_leverage_below_bps": 7500,
                "high_leverage_below_bps": 10000,
                "liabilities_match_assets_at_bps": 10000,
                "liabilities_exceed_assets_above_bps": 10000,
            },
        },
    }
