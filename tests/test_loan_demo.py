from datetime import date

from shared.loan_demo import build_demo_loan_record, build_amortization_schedule


def test_demo_loan_is_a_separate_illustrative_liability_with_balanced_schedule():
    loan = build_demo_loan_record(disbursement_date=date(2026, 11, 23))
    assert loan["principal_minor"] == 500_000
    assert loan["outstanding_principal_minor"] == 500_000
    assert loan["status"] == "ILLUSTRATIVE"
    assert loan["demo_only"] is True
    assert loan["payment_tracking"] == "NOT_TRACKED"
    assert len(loan["installments"]) == 24
    assert sum(row["principal_minor"] for row in loan["installments"]) == 500_000
    assert sum(row["interest_minor"] for row in loan["installments"]) == loan["total_interest_minor"]
    assert loan["total_repayment_minor"] == loan["principal_minor"] + loan["total_interest_minor"]


def test_loan_schedule_has_illustrative_due_dates_and_no_overdue_states():
    rows = build_amortization_schedule(500_000, 690, 24, date(2026, 12, 23))
    assert rows[0]["due_date"] == "2026-12-23"
    assert all(row["status"] == "ILLUSTRATIVE" for row in rows)
    assert rows[-1]["principal_minor"] > 0


def test_loan_schedule_rejects_invalid_or_non_amortizing_parameters():
    try:
        build_amortization_schedule(500_000, 690, 0, date(2026, 12, 23))
    except ValueError:
        pass
    else:
        raise AssertionError("term of zero must be rejected")


def test_zero_apr_loan_is_supported_and_has_no_interest():
    loan = build_demo_loan_record(
        disbursement_date=date(2026, 11, 23), apr_bps=0, term_months=10,
    )
    assert loan["total_interest_minor"] == 0
    assert loan["total_repayment_minor"] == 500_000
    assert sum(row["principal_minor"] for row in loan["installments"]) == 500_000


def test_demo_loan_record_matches_registered_read_only_capability_output_contract():
    from shared.contracts.registry import validate_capability_output

    loan = build_demo_loan_record(disbursement_date=date(2026, 11, 23))
    validated = validate_capability_output("banking.loans.overview", loan)
    assert validated["demo_only"] is True
    assert validated["payment_tracking"] == "NOT_TRACKED"
