import pytest

from shared.demo_seed import (
    DEFAULT_DEMO_SALARY_AMOUNT_MINOR,
    demo_salary_balance_delta,
    parse_demo_salary_amount_minor,
    build_demo_transaction_history,
)


def test_default_synthetic_salary_is_eleven_thousand_eur():
    assert DEFAULT_DEMO_SALARY_AMOUNT_MINOR == 1_100_000
    assert parse_demo_salary_amount_minor(None) == 1_100_000
    assert parse_demo_salary_amount_minor("1100000") == 1_100_000


def test_salary_migration_delta_is_idempotent_after_first_adjustment():
    first_delta = demo_salary_balance_delta(325_000, 1_100_000)
    assert first_delta == 775_000
    assert demo_salary_balance_delta(1_100_000, 1_100_000) == 0


def test_invalid_salary_seed_amount_is_rejected():
    for value in ("", "abc", "11000.00", "0", "-1"):
        with pytest.raises(ValueError):
            parse_demo_salary_amount_minor(value)


def test_account_delta_can_be_negative_when_demo_salary_is_reduced():
    assert demo_salary_balance_delta(1_100_000, 800_000) == -300_000


def test_extended_demo_history_has_35_rows_across_three_calendar_months():
    from datetime import datetime, timezone

    anchor = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
    rows = build_demo_transaction_history(anchor=anchor)
    assert len(rows) == 28  # 7 original schema rows + 28 additive rows = 35 total.
    assert min(row["days_ago"] for row in rows) == 8
    assert max(row["days_ago"] for row in rows) == 63
    assert all(row["status"] == "BOOKED" and row["currency"] == "EUR" for row in rows)
    salary_rows = [row for row in rows if row["category"] == "INCOME"]
    assert len(salary_rows) == 2
    assert {row["amount_minor"] for row in salary_rows} == {DEFAULT_DEMO_SALARY_AMOUNT_MINOR}
    coffee_rows = [row for row in rows if "coffee" in str(row["description"]).casefold()]
    assert len(coffee_rows) == 3
    assert {row["occurred_at"].strftime("%Y-%m") for row in coffee_rows} == {
        "2026-09",
        "2026-10",
        "2026-11",
    }
    transport_rows = [row for row in rows if row["category"] == "TRANSPORT"]
    assert len(transport_rows) == 5


def test_demo_history_salaries_follow_the_configured_demo_amount_and_employer():
    rows = build_demo_transaction_history(
        employer="Example Employer",
        salary_amount_minor=975_000,
    )
    salary_rows = [row for row in rows if row["category"] == "INCOME"]
    assert all(row["amount_minor"] == 975_000 for row in salary_rows)
    assert all(row["description"] == "Salary · Example Employer" for row in salary_rows)


def test_extended_history_seed_is_opt_in_outside_local_compose(monkeypatch):
    from shared.demo_configuration import demo_transaction_history_enabled

    monkeypatch.delenv("DEMO_TRANSACTION_HISTORY_ENABLED", raising=False)
    assert demo_transaction_history_enabled() is False
    for value in ("1", "true", "yes", "on"):
        monkeypatch.setenv("DEMO_TRANSACTION_HISTORY_ENABLED", value)
        assert demo_transaction_history_enabled() is True
    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("DEMO_TRANSACTION_HISTORY_ENABLED", value)
        assert demo_transaction_history_enabled() is False
