"""Synthetic loan terms and deterministic illustrative amortization schedule.

This is demo data only. It is not a credit offer, origination decision,
repayment instruction, or evidence of a real loan disbursement.
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


DEFAULT_LOAN_PRINCIPAL_MINOR = 500_000
DEFAULT_LOAN_APR_BPS = 690
DEFAULT_LOAN_TERM_MONTHS = 24


def _bounded_int(
    value: str | int | None, default: int, name: str, maximum: int, *, minimum: int = 1,
) -> int:
    try:
        parsed = int(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def loan_demo_terms(
    principal_minor: str | int | None = None,
    apr_bps: str | int | None = None,
    term_months: str | int | None = None,
) -> dict[str, int]:
    """Calculate an illustrative amortizing loan schedule using integer minor units."""
    principal = _bounded_int(principal_minor, DEFAULT_LOAN_PRINCIPAL_MINOR, "DEMO_LOAN_PRINCIPAL_MINOR", 100_000_000)
    apr = _bounded_int(apr_bps, DEFAULT_LOAN_APR_BPS, "DEMO_LOAN_APR_BPS", 100_000, minimum=0)
    term = _bounded_int(term_months, DEFAULT_LOAN_TERM_MONTHS, "DEMO_LOAN_TERM_MONTHS", 600)
    return {"principal_minor": principal, "annual_interest_rate_bps": apr, "term_months": term}


def add_months(value: date, months: int) -> date:
    """Add calendar months while keeping a valid day of month."""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def build_amortization_schedule(
    principal_minor: int,
    annual_interest_rate_bps: int,
    term_months: int,
    first_due_date: date,
) -> list[dict[str, Any]]:
    """Return an equal-payment illustrative schedule, rounded to minor units."""
    principal = _bounded_int(principal_minor, 0, "principal_minor", 100_000_000)
    apr_bps = _bounded_int(annual_interest_rate_bps, 0, "annual_interest_rate_bps", 100_000, minimum=0)
    months = _bounded_int(term_months, 0, "term_months", 600)
    monthly_rate = Decimal(apr_bps) / Decimal(10_000) / Decimal(12)
    p = Decimal(principal)
    if monthly_rate == 0:
        regular_payment = (p / months).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    else:
        factor = (Decimal(1) + monthly_rate) ** (-months)
        regular_payment = (p * monthly_rate / (Decimal(1) - factor)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    outstanding = p
    rows: list[dict[str, Any]] = []
    for number in range(1, months + 1):
        interest = (outstanding * monthly_rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        payment = regular_payment
        principal_component = payment - interest
        if number == months or principal_component >= outstanding:
            principal_component = outstanding
            payment = principal_component + interest
        if principal_component < 0:
            raise ValueError("The illustrative loan payment does not amortize principal")
        outstanding -= principal_component
        rows.append({
            "installment_number": number,
            "due_date": add_months(first_due_date, number - 1).isoformat(),
            "payment_minor": int(payment),
            "principal_minor": int(principal_component),
            "interest_minor": int(interest),
            "status": "ILLUSTRATIVE",
        })
    if outstanding != 0:
        raise ValueError("The illustrative loan schedule did not amortize to zero")
    return rows


def build_demo_loan_record(
    *, loan_id: str = "loan-demo-001", principal_minor: int = DEFAULT_LOAN_PRINCIPAL_MINOR,
    apr_bps: int = DEFAULT_LOAN_APR_BPS, term_months: int = DEFAULT_LOAN_TERM_MONTHS,
    disbursement_date: date,
) -> dict[str, Any]:
    """Build a synthetic loan record and its illustrative repayment schedule."""
    terms = loan_demo_terms(principal_minor, apr_bps, term_months)
    first_due_date = add_months(disbursement_date, 1)
    installments = build_amortization_schedule(
        terms["principal_minor"], terms["annual_interest_rate_bps"], terms["term_months"], first_due_date,
    )
    total_repayment = sum(item["payment_minor"] for item in installments)
    total_interest = sum(item["interest_minor"] for item in installments)
    return {
        "loan_id": loan_id,
        "product_name": "Illustrative personal loan",
        "principal_minor": terms["principal_minor"],
        "outstanding_principal_minor": terms["principal_minor"],
        "currency": "EUR",
        "annual_interest_rate_bps": terms["annual_interest_rate_bps"],
        "term_months": terms["term_months"],
        "monthly_payment_minor": installments[0]["payment_minor"],
        "total_interest_minor": total_interest,
        "total_repayment_minor": total_repayment,
        "disbursement_date": disbursement_date.isoformat(),
        "first_due_date": first_due_date.isoformat(),
        "status": "ILLUSTRATIVE",
        "demo_only": True,
        "payment_tracking": "NOT_TRACKED",
        "installments": installments,
    }
