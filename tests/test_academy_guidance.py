from services.agent_runtime.academy import (
    academy_personal_snapshot_intent,
    render_loan_schedule_explanation,
)


def test_personal_loan_schedule_academy_topic_requires_registered_loan_read():
    question = (
        "Walk me through my illustrative loan repayment schedule. "
        "Explain the difference between principal, interest and APR."
    )
    assert academy_personal_snapshot_intent(question) == "LOAN_INFORMATION"


def test_loan_schedule_explanation_uses_verified_schedule_and_distinguishes_rate_from_apr():
    result = {
        "loan_id": "loan-demo-001",
        "product_name": "Illustrative personal loan",
        "principal_minor": 500000,
        "outstanding_principal_minor": 500000,
        "currency": "EUR",
        "annual_interest_rate_bps": 690,
        "term_months": 24,
        "monthly_payment_minor": 22373,
        "total_interest_minor": 36952,
        "total_repayment_minor": 536952,
        "first_due_date": "2026-12-23",
        "status": "ILLUSTRATIVE",
        "demo_only": True,
        "payment_tracking": "NOT_TRACKED",
        "installments": [
            {"installment_number": 1, "due_date": "2026-12-23", "payment_minor": 22373, "principal_minor": 19500, "interest_minor": 2873, "status": "ILLUSTRATIVE"},
            {"installment_number": 2, "due_date": "2027-01-23", "payment_minor": 22373, "principal_minor": 19612, "interest_minor": 2761, "status": "ILLUSTRATIVE"},
            {"installment_number": 3, "due_date": "2027-02-23", "payment_minor": 22373, "principal_minor": 19725, "interest_minor": 2648, "status": "ILLUSTRATIVE"},
            {"installment_number": 4, "due_date": "2027-03-23", "payment_minor": 22373, "principal_minor": 19839, "interest_minor": 2534, "status": "ILLUSTRATIVE"},
        ],
    }
    answer = render_loan_schedule_explanation(result)
    assert "Original principal: **EUR 5,000.00**" in answer
    assert "Recorded outstanding principal: **EUR 5,000.00**" in answer
    assert "First illustrative instalments" in answer
    assert "EUR 195.00" in answer
    assert "EUR 28.73" in answer
    assert "**Principal**" in answer
    assert "**Interest**" in answer
    assert "**APR (Annual Percentage Rate)**" in answer
    assert "annual interest rate of 6.90%" in answer
    assert "won't present that interest rate as the loan's APR" in answer
    assert "do not establish that an instalment is currently due" in answer


def test_loan_schedule_explanation_does_not_invent_missing_loan_record():
    answer = render_loan_schedule_explanation({})
    assert "couldn't find an illustrative loan record" in answer
    assert "I won't invent loan terms" in answer


def test_general_monthly_cash_flow_request_uses_fast_registry_topic_guidance():
    from services.agent_runtime.academy import academy_direct_guidance_for_request

    question = "Help me understand my monthly cash flow and how to leave enough room for bills, savings and loan instalments."
    answer = academy_direct_guidance_for_request(question)
    assert answer
    assert "Monthly cash flow" in answer
    assert "essential bills" in answer
    assert "buffer" in answer
    assert "does not maintain a complete household budget" in answer


def test_scoring_profile_request_uses_credit_score_education_without_claiming_bureau_access():
    from services.agent_runtime.academy import academy_direct_guidance_for_request

    answer = academy_direct_guidance_for_request("Is my current scoring profile good enough?")

    assert answer
    assert "does not read a credit-bureau report" in answer
    assert "Pay credit instalments and other bills on time" in answer


def test_current_scoring_wording_routes_to_credit_score_education_without_bureau_claims():
    from services.agent_runtime.academy import academy_direct_guidance_for_request

    answer = academy_direct_guidance_for_request("What is my current scoring?")

    assert answer
    assert "does not read a credit-bureau report" in answer
    assert "credit score" in answer.casefold()


def test_savings_purpose_starter_prompt_uses_catalogue_direct_education():
    from services.agent_runtime.academy import academy_direct_guidance_for_request

    question = (
        "Help me understand what my savings account is good for and how to balance "
        "savings with money I need for everyday expenses."
    )
    answer = academy_direct_guidance_for_request(question)
    assert answer
    assert "Saving with a purpose" in answer
    assert "goals and unexpected costs" in answer
    assert "essential bills and short-term needs" in answer
    assert "withdrawal limits" in answer


def test_spending_category_question_is_not_preempted_by_general_cash_flow_education():
    from services.agent_runtime.academy import academy_direct_guidance_for_request

    assert academy_direct_guidance_for_request("do I have any spendings on transport?") is None


def test_general_cash_flow_education_still_uses_catalogue_direct_guidance():
    from services.agent_runtime.academy import academy_direct_guidance_for_request

    answer = academy_direct_guidance_for_request(
        "Please explain budgeting and cash flow, including how I can plan bills and savings."
    )
    assert answer
    assert "Budgeting and cash flow" in answer
