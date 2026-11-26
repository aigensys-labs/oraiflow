"""Currency-aware integer minor-unit arithmetic and formatting helpers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import HTTPException


def amount_minor(value: str) -> int:
    """Parse a positive decimal amount into minor units without float rounding."""
    try:
        amount = Decimal(value.replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        raise HTTPException(status_code=422, detail="Enter a valid transfer amount")
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        raise HTTPException(status_code=422, detail="Amount must be positive with at most two decimal places")
    return int(amount * 100)
