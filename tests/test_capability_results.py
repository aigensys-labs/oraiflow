import json
from types import SimpleNamespace

from services.agent_runtime.capability_results import (
    capability_result_diagnostic,
    render_saved_contact_lookup_summary,
    render_transaction_analysis_summary,
    verified_saved_contact_lookup_summary,
    verified_transaction_analysis_summary,
)
from shared.contracts.registry import create_result_envelope


def _result(**overrides):
    data = {
        "period": "this_month",
        "period_start": "2026-11-01",
        "period_end": "2026-11-25",
        "business_timezone": "Europe/Prague",
        "flow": "outflows",
        "analysis_mode": "activity",
        "measure": "sum",
        "group_by": "none",
        "include_transfers": False,
        "transaction_count": 2,
        "truncated": False,
        "summary_by_currency": [
            {"currency": "EUR", "transaction_count": 2, "sum_minor": 970, "average_minor": 485},
        ],
        "groups": [],
        "transactions": [],
    }
    data.update(overrides)
    return data


def test_generic_transaction_response_fallback_displays_verified_sum_and_date_range():
    answer = render_transaction_analysis_summary(_result())
    assert "spending" in answer
    assert "from 1 Nov 2026 to 25 Nov 2026" in answer
    assert "EUR 9.70" in answer
    assert "2 booked transactions" in answer


def test_empty_transaction_analysis_fallback_explicitly_says_no_matches():
    answer = render_transaction_analysis_summary(
        _result(
            transaction_count=0,
            summary_by_currency=[],
        )
    )
    assert "no matching booked transactions" in answer
    assert "from 1 Nov 2026 to 25 Nov 2026" in answer


def test_multiple_currencies_are_shown_separately_not_combined():
    answer = render_transaction_analysis_summary(
        _result(
            flow="all",
            summary_by_currency=[
                {
                    "currency": "EUR",
                    "transaction_count": 2,
                    "sum_minor": 15000,
                    "average_minor": 7500,
                },
                {
                    "currency": "USD",
                    "transaction_count": 1,
                    "sum_minor": 4200,
                    "average_minor": 4200,
                },
            ],
            transaction_count=3,
        )
    )
    assert "EUR 150.00" in answer
    assert "USD 42.00" in answer
    assert "EUR 192.00" not in answer


def test_recurring_inflow_fallback_is_explicitly_only_a_screening_signal():
    answer = render_transaction_analysis_summary(
        _result(
            flow="inflows",
            analysis_mode="recurring_inflows",
            group_by="merchant",
            summary_by_currency=[
                {
                    "currency": "EUR",
                    "transaction_count": 3,
                    "sum_minor": 3300000,
                    "average_minor": 1100000,
                },
            ],
            groups=[
                {
                    "group_value": "Recorded salary description",
                    "currency": "EUR",
                    "transaction_count": 3,
                    "sum_minor": 3300000,
                    "average_minor": 1100000,
                    "active_months": 3,
                    "potentially_recurring": True,
                }
            ],
            transaction_count=3,
        )
    )
    assert "screening signal" in answer
    assert "not proof of salary" in answer
    assert "Recorded salary description" in answer


def test_truncated_analysis_fallback_discloses_partial_result():
    answer = render_transaction_analysis_summary(_result(truncated=True))
    assert "partial result" in answer


def test_transaction_summary_source_is_a_validated_current_turn_result_envelope():
    data = _result()
    envelope = create_result_envelope(
        "banking.transactions.analyze",
        "SUCCESS",
        data=data,
        trace_id="test-analysis",
    )
    messages = [
        SimpleNamespace(type="human", content="How much did I spend on coffee this month?"),
        SimpleNamespace(
            type="tool",
            name="analyze_customer_transactions",
            content=json.dumps(envelope),
        ),
    ]
    seen, verified, reason, outcome, error_code = capability_result_diagnostic(
        messages,
        "banking.transactions.analyze",
        trace_id="test-analysis",
    )
    assert seen is True
    assert reason is None
    assert outcome == "SUCCESS"
    assert error_code is None
    assert verified["data"] == data
    assert "EUR 9.70" in render_transaction_analysis_summary(verified["data"])


def test_verified_summary_helper_reads_only_the_registered_current_turn_envelope():
    data = _result()
    envelope = create_result_envelope(
        "banking.transactions.analyze",
        "SUCCESS",
        data=data,
        trace_id="verified-current-turn",
    )
    messages = [
        SimpleNamespace(type="human", content="How much did I spend on coffee last month?"),
        SimpleNamespace(
            type="tool",
            name="analyze_customer_transactions",
            content=json.dumps(envelope),
        ),
    ]
    answer = verified_transaction_analysis_summary(messages, trace_id="verified-current-turn")
    assert answer is not None
    assert "EUR 9.70" in answer
    assert "from 1 Nov 2026 to 25 Nov 2026" in answer


def test_verified_summary_helper_rejects_unregistered_or_invalid_tool_output():
    messages = [
        SimpleNamespace(type="human", content="How much did I spend on coffee?"),
        SimpleNamespace(type="tool", name="some_other_tool", content='{"data":{}}'),
    ]
    assert verified_transaction_analysis_summary(messages) is None


def test_transaction_summary_names_the_verified_merchant_filter():
    answer = render_transaction_analysis_summary(_result(merchant_query="coffee"))
    assert "coffee spending" in answer
    assert "EUR 9.70" in answer


def test_saved_contact_summary_shows_verified_contact_details_and_only_masked_reference():
    answer = render_saved_contact_lookup_summary(
        [
            {
                "contact_id": "ct-12345678",
                "full_name": "John Smith",
                "bank_name": "Northstar Bank",
                "account_reference_masked": "DEMO ···· 1042",
                "currency": "EUR",
                "status": "ACTIVE",
            }
        ]
    )
    assert "John Smith" in answer
    assert "Northstar Bank" in answer
    assert "DEMO ···· 1042" in answer
    assert "EUR" in answer
    assert "ct-12345678" not in answer


def test_saved_contact_summary_omits_unmasked_account_reference():
    answer = render_saved_contact_lookup_summary(
        [
            {
                "contact_id": "ct-12345678",
                "full_name": "John Smith",
                "bank_name": "Northstar Bank",
                "account_reference_masked": "1234567890123456",
                "currency": "EUR",
                "status": "ACTIVE",
            }
        ]
    )
    assert "1234567890123456" not in answer


def test_verified_saved_contact_summary_requires_valid_current_turn_result():
    from types import SimpleNamespace
    from shared.contracts.registry import create_result_envelope

    data = [
        {
            "contact_id": "ct-12345678",
            "full_name": "John Smith",
            "bank_name": "Northstar Bank",
            "account_reference_masked": "DEMO ···· 1042",
            "currency": "EUR",
            "status": "ACTIVE",
        }
    ]
    result = create_result_envelope(
        "banking.contacts.search", "SUCCESS", data=data, trace_id="test-contact"
    )
    messages = [
        SimpleNamespace(type="human", content="Find John in my saved contacts."),
        SimpleNamespace(type="tool", name="search_contacts", content=json.dumps(result)),
    ]
    assert "John Smith" in verified_saved_contact_lookup_summary(messages, trace_id="test-contact")
    assert (
        verified_saved_contact_lookup_summary(
            [
                SimpleNamespace(type="human", content="Find John in my saved contacts."),
            ]
        )
        is None
    )


def test_category_filtered_analysis_names_the_requested_category_in_customer_summary():
    answer = render_transaction_analysis_summary(
        _result(
            category_query="transport",
            merchant_query=None,
            transaction_count=2,
            summary_by_currency=[
                {
                    "currency": "EUR",
                    "transaction_count": 2,
                    "sum_minor": 3200,
                    "average_minor": 1600,
                }
            ],
        )
    )
    assert "Your transport spending" in answer
    assert "EUR 32.00" in answer


def test_empty_category_filtered_analysis_names_the_requested_category():
    answer = render_transaction_analysis_summary(
        _result(
            category_query="transport",
            merchant_query=None,
            transaction_count=0,
            summary_by_currency=[],
        )
    )
    assert "no booked transactions matching transport" in answer


def test_monthly_analysis_summary_displays_total_and_readable_chronological_breakdown():
    answer = render_transaction_analysis_summary(
        _result(
            period="last_90_days",
            period_start="2026-08-29",
            period_end="2026-11-26",
            group_by="month",
            merchant_query="coffee",
            category_query=None,
            transaction_count=4,
            summary_by_currency=[
                {
                    "currency": "EUR",
                    "transaction_count": 4,
                    "sum_minor": 1955,
                    "average_minor": 489,
                }
            ],
            groups=[
                {
                    "group_value": "2026-09",
                    "currency": "EUR",
                    "transaction_count": 1,
                    "sum_minor": 465,
                },
                {
                    "group_value": "2026-10",
                    "currency": "EUR",
                    "transaction_count": 1,
                    "sum_minor": 485,
                },
                {
                    "group_value": "2026-11",
                    "currency": "EUR",
                    "transaction_count": 2,
                    "sum_minor": 1005,
                },
            ],
        )
    )
    assert "Your coffee spending" in answer
    assert "EUR 19.55" in answer
    assert "Breakdown:" in answer
    assert "Sep 2026: EUR 4.65" in answer
    assert "Oct 2026: EUR 4.85" in answer
    assert "Nov 2026: EUR 10.05" in answer
