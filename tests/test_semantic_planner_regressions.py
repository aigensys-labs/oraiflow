from types import SimpleNamespace

import pytest

from services.agent_runtime.intent_contracts import (
    classify_canonical_read_intent,
    discover_intent_candidates,
)
from services.agent_runtime.navigation import (
    navigation_confirmation_metadata,
    resolve_pending_navigation_confirmation,
    resolve_workspace_request,
)
from services.agent_runtime.task_interpretation import InterpretedTask, TaskEvidence
from services.agent_runtime.task_planner import plan_interpreted_task


def _human(content: str, message_id: str = "human-1"):
    return SimpleNamespace(type="human", content=content, id=message_id)


def _ai(content: str, pending=None):
    from services.agent_runtime.navigation import PENDING_NAVIGATION_KEY

    return SimpleNamespace(
        type="ai",
        content=content,
        additional_kwargs={PENDING_NAVIGATION_KEY: pending} if pending else {},
    )


def test_account_aggregate_language_resolves_to_all_accounts_total_not_account_list_or_single_balance():
    for utterance in (
        "How much do I have across my accounts?",
        "what is total sum of my accounts?",
        "How much money do I have in all my accounts?",
    ):
        assert classify_canonical_read_intent(utterance) == "ACCOUNT_TOTAL_BALANCE", utterance


def test_received_money_question_discovers_transaction_analysis_candidate():
    candidates = discover_intent_candidates("Did I receive any money in the last 30 days?")
    transaction = next(item for item in candidates if item["intent_id"] == "TRANSACTION_ANALYSIS")
    assert transaction["match_kind"] == "semantic_phrase"
    assert transaction["auto_resolve"] is False


@pytest.mark.parametrize(
    "utterance",
    [
        "how much do I spend on coffee each month?",
        "do I have any spendings on transport?",
    ],
)
def test_ordinary_spending_paraphrases_surface_transaction_analysis_candidate(utterance):
    candidates = discover_intent_candidates(utterance, limit=8)
    transaction = next(item for item in candidates if item["intent_id"] == "TRANSACTION_ANALYSIS")
    assert transaction["score"] > 0
    assert transaction["auto_resolve"] is False


def test_latest_transaction_explanation_is_a_semantic_hint_not_a_transaction_list_or_detail_fast_path():
    utterance = "Explain my latest transaction"
    candidates = discover_intent_candidates(utterance, limit=5)
    transaction = next(
        item for item in candidates if item["intent_id"] == "TRANSACTION_EXPLANATION"
    )
    assert transaction["match_kind"] == "semantic_phrase"
    assert transaction["auto_resolve"] is False
    assert classify_canonical_read_intent(utterance) is None


def test_latest_transaction_selector_plans_one_verified_transaction_without_extra_identifiers():
    utterance = "Explain my latest transaction"
    task = InterpretedTask(
        intent_id="TRANSACTION_EXPLANATION",
        requested_outcome="EXPLANATION",
        intent_confidence=0.97,
        evidence=[TaskEvidence(field="intent", source="current_turn", quote=utterance)],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance},
        messages=[_human(utterance)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["capability_id"] == "banking.transactions.recent"
    assert planned["arguments"] == {"limit": 1}
    assert planned["response_mode"] == "grounded_explanation"


def test_planner_reconciles_model_clarification_when_registered_latest_default_completes_contract():
    utterance = "Explain my latest transaction"
    task = InterpretedTask(
        intent_id="TRANSACTION_EXPLANATION",
        requested_outcome="EXPLANATION",
        resolution="NEEDS_CLARIFICATION",
        clarification_question="Which transaction would you like me to explain?",
        intent_confidence=0.97,
        evidence=[TaskEvidence(field="intent", source="current_turn", quote=utterance)],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance},
        messages=[_human(utterance)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["semantic_values"] == {"transaction_selector": "latest"}
    assert planned["arguments"] == {"limit": 1}
    assert planned["model_resolution"] == "NEEDS_CLARIFICATION"
    assert planned["model_clarification_reconciled"] is True
    # The accepted assessment is normalized only after the OCCF plan has
    # supplied and validated its declared default.
    assert planned["assessment"]["resolution"] == "READY"


def test_transaction_semantic_selector_accepts_unlisted_paraphrase_with_exact_customer_evidence():
    utterance = "Please explain the payment I made most recently"
    task = InterpretedTask(
        intent_id="TRANSACTION_EXPLANATION",
        requested_outcome="EXPLANATION",
        resolution="NEEDS_CLARIFICATION",
        intent_confidence=0.97,
        slot_confidence=0.94,
        slots={"transaction_selector": "latest"},
        evidence=[
            TaskEvidence(field="intent", source="current_turn", quote=utterance),
            TaskEvidence(
                field="transaction_selector",
                source="current_turn",
                quote="the payment I made most recently",
            ),
        ],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance},
        messages=[_human(utterance)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["semantic_values"] == {"transaction_selector": "latest"}
    assert planned["arguments"] == {"limit": 1}


def test_account_total_contract_defaults_to_all_accessible_accounts_when_model_requests_clarification():
    utterance = "What is the combined amount across all the accounts I keep here?"
    task = InterpretedTask(
        intent_id="ACCOUNT_TOTAL_BALANCE",
        requested_outcome="INFORMATION",
        resolution="NEEDS_CLARIFICATION",
        clarification_question="Which account do you mean?",
        intent_confidence=0.96,
        evidence=[TaskEvidence(field="intent", source="current_turn", quote=utterance)],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance},
        messages=[_human(utterance)],
        context={"selected_account_id": "acct-selected"},
    )
    assert planned["status"] == "PLANNED"
    assert planned["task_class"] == "presentation"
    assert planned["semantic_values"] == {"account_scope": "all_accounts"}
    assert planned["execution_plan"]["ignore_selected_account"] is True
    assert planned["model_clarification_reconciled"] is True


def test_semantic_selector_requires_field_specific_customer_evidence():
    utterance = "Explain my transaction"
    task = InterpretedTask(
        intent_id="TRANSACTION_EXPLANATION",
        requested_outcome="EXPLANATION",
        resolution="NEEDS_CLARIFICATION",
        intent_confidence=0.97,
        slot_confidence=0.96,
        slots={"transaction_selector": "latest"},
        # An intent quote is not evidence for a semantic selector. The runtime
        # must not manufacture field-specific evidence for an LLM interpretation.
        evidence=[TaskEvidence(field="intent", source="current_turn", quote=utterance)],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance},
        messages=[_human(utterance)],
        context={},
    )
    assert planned["status"] == "CLARIFICATION_REQUIRED"
    assert planned["reason_code"] == "VALUE_EVIDENCE_MISSING"
    assert planned["missing_fields"] == ["transaction_selector"]
    assert planned.get("arguments") is None


def test_model_clarification_does_not_bypass_genuinely_required_capability_inputs():
    utterance = "Open a workspace"
    task = InterpretedTask(
        intent_id="WORKSPACE_NAVIGATION",
        requested_outcome="NAVIGATION",
        resolution="NEEDS_CLARIFICATION",
        clarification_question="Which workspace would you like to open?",
        intent_confidence=0.99,
        evidence=[TaskEvidence(field="intent", source="current_turn", quote=utterance)],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance},
        messages=[_human(utterance)],
        context={},
    )
    assert planned["status"] == "CLARIFICATION_REQUIRED"
    assert planned["reason_code"] == "REQUIRED_INPUT_MISSING"
    assert planned["missing_fields"] == ["workspace"]
    assert planned.get("arguments") is None


def test_explicit_unsupported_transaction_selector_is_not_replaced_by_latest_default():
    utterance = "Explain the first transaction"
    task = InterpretedTask(
        intent_id="TRANSACTION_EXPLANATION",
        requested_outcome="EXPLANATION",
        intent_confidence=0.96,
        slot_confidence=0.95,
        slots={"transaction_selector": "first"},
        evidence=[
            TaskEvidence(field="intent", source="current_turn", quote=utterance),
            TaskEvidence(
                field="transaction_selector", source="current_turn", quote="first transaction"
            ),
        ],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance},
        messages=[_human(utterance)],
        context={},
    )
    assert planned["status"] == "CLARIFICATION_REQUIRED"
    assert planned["reason_code"] == "INTENT_SEMANTIC_VALUE_UNSUPPORTED"
    assert planned.get("arguments") is None


def test_principal_interest_and_apr_concepts_have_direct_registered_academy_guidance():
    from services.agent_runtime.academy import academy_direct_guidance_for_request

    answer = academy_direct_guidance_for_request("Explain principal, interest and APR.")
    assert answer
    assert "**Principal**" in answer
    assert "**Interest**" in answer
    assert "**APR (Annual Percentage Rate)**" in answer


def test_financial_health_uses_customer_labels_without_claiming_a_trend_or_changing_ratios():
    from services.agent_runtime.academy import render_financial_health_explanation

    labels = {
        "LOW_LEVERAGE": "Excellent",
        "MODERATE_LEVERAGE": "Good",
        "ELEVATED_LEVERAGE": "Needs attention",
        "HIGH_LEVERAGE": "Warning",
        "LIABILITIES_MATCH_ASSETS": "Warning",
        "LIABILITIES_EXCEED_ASSETS": "Warning",
        "INSUFFICIENT_DATA": "Not enough recorded data",
    }
    for status, expected in labels.items():
        text = render_financial_health_explanation(
            {
                "positions": [
                    {
                        "currency": "EUR",
                        "status": status,
                        "recorded_equity_minor": 100_000,
                        "recorded_deposit_assets_minor": 200_000,
                        "total_recorded_liabilities_minor": 100_000,
                        "loan_liabilities_minor": 100_000,
                        "loan_to_equity_bps": 10_000,
                        "debt_to_assets_bps": 5_000,
                    }
                ]
            },
            "explain my financial health",
        )
        assert expected in text
        assert "cannot establish whether your position is improving or worsening" in text
        assert "Total liabilities / recorded deposit assets" in text


def test_academy_pronoun_navigation_resolves_only_a_unique_preceding_workspace_reference():
    messages = [
        _human("What is Academy?"),
        _ai("Academy is the learning workspace where you can explore financial concepts."),
        _human("show me its page"),
    ]
    assert resolve_workspace_request("show me its page", messages=messages) == "Academy"

    after_navigation_clarification = [
        _human("What is Academy?", "human-1"),
        _ai("Academy is the learning workspace where you can explore financial concepts."),
        _human("go to", "human-2"),
        _ai(
            "Which ORAIflow workspace would you like me to open? Available workspaces: Overview, Accounts, Academy."
        ),
        _human("show me its page", "human-3"),
    ]
    assert (
        resolve_workspace_request("show me its page", messages=after_navigation_clarification)
        == "Academy"
    )

    controls = [
        _human("What is Controls & audit?", "human-4"),
        _ai("Controls & audit contains the administrative review views."),
        _human("show me its page", "human-5"),
    ]
    assert resolve_workspace_request("show me its page", messages=controls) == "Controls & audit"

    ambiguous = [
        _human("What is the app?"),
        _ai("Academy and Accounts are two useful workspaces."),
        _human("show me its page"),
    ]
    assert resolve_workspace_request("show me its page", messages=ambiguous) is None


def test_completed_navigation_turn_does_not_revive_older_legacy_confirmation():
    pending = navigation_confirmation_metadata("Academy")
    messages = [
        _ai("Would you like me to open Academy?", pending),
        _human("yes"),
        _ai("Done — I opened the Academy workspace."),
    ]
    assert resolve_pending_navigation_confirmation(messages, "yes", {"navigate_workspace"}) is None


def test_demo_loan_feature_flag_is_passed_into_the_mcp_container():
    from pathlib import Path

    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    mcp_block = compose.split("  mcp:\n", 1)[1].split("  agent-runtime:\n", 1)[0]
    assert "DEMO_LOAN_ENABLED: ${DEMO_LOAN_ENABLED:-false}" in mcp_block


@pytest.mark.asyncio
async def test_long_academy_credit_score_explanation_is_chunked_by_existing_agui_streamer():
    from services.agent_runtime.academy import academy_direct_guidance_for_request
    from services.agent_runtime.streaming import stream_text_events

    explanation = academy_direct_guidance_for_request("What is my current scoring?")
    assert explanation and len(explanation) > 100
    start = SimpleNamespace(
        type=SimpleNamespace(value="TEXT_MESSAGE_START"), message_id="academy-1", role="assistant"
    )
    content = SimpleNamespace(
        type=SimpleNamespace(value="TEXT_MESSAGE_CONTENT"),
        message_id="academy-1",
        role="assistant",
        delta=explanation,
    )
    end = SimpleNamespace(
        type=SimpleNamespace(value="TEXT_MESSAGE_END"), message_id="academy-1", role="assistant"
    )

    async def source():
        yield start
        yield content
        yield end

    streamed = [
        item async for item in stream_text_events(source(), target_chars=24, delay_seconds=0)
    ]
    chunks = [item for item in streamed if "TEXT_MESSAGE_CONTENT" in item.type.value]
    assert len(chunks) > 1
    assert "".join(item.delta for item in chunks) == explanation


def test_monthly_coffee_question_compiles_month_grouping_and_ninety_day_window():
    utterance = "how much do I spend on coffee each month?"
    task = InterpretedTask(
        intent_id="TRANSACTION_ANALYSIS",
        requested_outcome="INFORMATION",
        intent_confidence=0.98,
        evidence=[TaskEvidence(field="intent", source="current_turn", quote=utterance)],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance},
        messages=[_human(utterance)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["semantic_values"] == {
        "period": "last_90_days",
        "flow": "outflows",
        "measure": "sum",
        "group_by": "month",
        "analysis_mode": "activity",
        "transfer_scope": "exclude_category_transfers",
    }
    assert planned["arguments"]["merchant_query"] == "coffee"
    assert planned["arguments"]["group_by"] == "month"
    assert planned["arguments"]["period"] == "last_90_days"
    assert planned["arguments"]["flow"] == "outflows"


@pytest.mark.parametrize(
    "grouping_phrase",
    [
        "by month",
        "by months",
        "each month",
        "every month",
        "month by month",
        "month-by-month",
        "monthly",
    ],
)
def test_coffee_month_grouping_phrases_never_leak_into_merchant_filter(grouping_phrase):
    utterance = f"How much do I spend on coffee {grouping_phrase}?"
    task = InterpretedTask(
        intent_id="TRANSACTION_ANALYSIS",
        requested_outcome="INFORMATION",
        intent_confidence=0.98,
        evidence=[TaskEvidence(field="intent", source="current_turn", quote=utterance)],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance},
        messages=[_human(utterance)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["semantic_values"]["group_by"] == "month"
    assert planned["semantic_values"]["period"] == "last_90_days"
    assert planned["arguments"]["merchant_query"] == "coffee"
    assert planned["arguments"]["group_by"] == "month"
    assert planned["arguments"]["period"] == "last_90_days"


def test_model_supplied_merchant_filter_is_cleaned_when_it_contains_grouping_suffix():
    utterance = "How much do I spend on coffee by months?"
    task = InterpretedTask(
        intent_id="TRANSACTION_ANALYSIS",
        requested_outcome="INFORMATION",
        intent_confidence=0.98,
        entities={"merchant_query": "coffee by months"},
        evidence=[
            TaskEvidence(field="intent", source="current_turn", quote=utterance),
            TaskEvidence(field="merchant_query", source="current_turn", quote="coffee by months"),
        ],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance},
        messages=[_human(utterance)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["semantic_values"]["group_by"] == "month"
    assert planned["semantic_values"]["period"] == "last_90_days"
    assert planned["arguments"]["merchant_query"] == "coffee"


def test_transport_spending_question_compiles_recorded_category_filter():
    utterance = "do I have any spendings on transport?"
    task = InterpretedTask(
        intent_id="TRANSACTION_ANALYSIS",
        requested_outcome="INFORMATION",
        intent_confidence=0.98,
        evidence=[TaskEvidence(field="intent", source="current_turn", quote=utterance)],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance},
        messages=[_human(utterance)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["semantic_values"]["flow"] == "outflows"
    assert planned["arguments"]["category_query"] == "transport"
    assert "merchant_query" not in planned["arguments"]
