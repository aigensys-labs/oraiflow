
import pytest
from fastapi import HTTPException

from shared.money import amount_minor as _amount_minor


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("25", 2500),
        ("25.00", 2500),
        ("1,234.56", 123456),
        ("0.01", 1),
    ],
)
def test_amount_minor_accepts_valid_money(value: str, expected: int) -> None:
    assert _amount_minor(value) == expected


@pytest.mark.parametrize("value", ["0", "-1", "1.001", "NaN", "Infinity", "not-money"])
def test_amount_minor_rejects_invalid_money(value: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _amount_minor(value)
    assert exc.value.status_code == 422
