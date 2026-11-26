from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from services.agent_runtime.task_interpretation import InterpretedTask, TaskEvidence
from services.agent_runtime.task_planner import plan_interpreted_task
from shared.task_contract import TurnTaskOutcome, TurnTaskPlan
from shared.contracts.registry import get_registry


def _message(message_id: str, content: str):
    return SimpleNamespace(type="human", id=message_id, content=content)


def test_internal_task_plan_is_a_strict_versioned_contract():
    valid = {
        "schema_version": "1.0.0",
        "tasks": [
            {
                "task_id": "task-1",
                "intent_id": "LOAN_INFORMATION",
                "status_hint": "PLANNED",
                "capability_ids": ["banking.loans.overview"],
                "capability_id": "banking.loans.overview",
                "kind": "mcp",
                "tool_name": "get_customer_loan_overview",
                "arguments": {},
            }
        ],
    }
    parsed = TurnTaskPlan.model_validate(valid)
    assert parsed.schema_version == "1.0.0"
    assert parsed.tasks[0].intent_id == "LOAN_INFORMATION"

    with_deadline = TurnTaskPlan.model_validate(
        {
            **valid,
            "model_deadline_epoch": 1_800_000_000.0,
        }
    )
    assert with_deadline.model_deadline_epoch == 1_800_000_000.0

    invalid = {**valid, "tasks": [{**valid["tasks"][0], "model_selected_capability": "evil"}]}
    with pytest.raises(ValidationError):
        TurnTaskPlan.model_validate(invalid)


def test_task_outcome_is_a_typed_independent_terminal_record():
    outcome = TurnTaskOutcome(
        task_id="task-1",
        intent_id="RECENT_TRANSACTIONS",
        status="UNAVAILABLE",
        capability_ids=("banking.transactions.recent",),
        reason_code="MCP_TIMEOUT",
    )
    assert outcome.status == "UNAVAILABLE"
    assert outcome.reason_code == "MCP_TIMEOUT"


def test_planner_derives_required_fields_from_registered_frontend_schema():
    text = "Please open a workspace for me"
    task = InterpretedTask(
        intent_id="WORKSPACE_NAVIGATION",
        requested_outcome="NAVIGATION",
        intent_confidence=0.95,
        evidence=[TaskEvidence(field="intent", source="current_turn", quote="open a workspace")],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": text},
        messages=[_message("turn-1", text)],
        context={"navigation_epoch": 0},
    )
    assert planned["status"] == "CLARIFICATION_REQUIRED"
    assert planned["missing_fields"] == ["workspace"]
    assert "workspace" in planned["clarification_question"].lower()


def test_planner_maps_grounded_workspace_entity_from_occf_not_model_tool_name():
    text = "Open Academy"
    task = InterpretedTask(
        intent_id="WORKSPACE_NAVIGATION",
        requested_outcome="NAVIGATION",
        intent_confidence=0.99,
        entities={"workspace": "Academy"},
        evidence=[
            TaskEvidence(field="intent", source="current_turn", quote="Open Academy"),
            TaskEvidence(field="workspace", source="current_turn", quote="Academy"),
        ],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": text},
        messages=[_message("turn-2", text)],
        context={"navigation_epoch": 2},
    )
    assert planned["status"] == "PLANNED"
    assert planned["capability_id"] == "frontend.workspace.navigate"
    assert planned["tool_name"] == "navigate_workspace"
    assert planned["arguments"]["workspace"] == "Academy"
    assert planned["arguments"]["navigation_epoch"] == 2


def test_planner_requires_conversation_references_to_resolve_exactly():
    task = InterpretedTask(
        intent_id="LOAN_INFORMATION",
        intent_confidence=0.95,
        conversation_references=["missing-message"],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": "Explain the loan schedule"},
        messages=[_message("turn-3", "Explain the loan schedule")],
        context={},
    )
    assert planned["status"] == "CLARIFICATION_REQUIRED"
    assert planned["reason_code"] == "CONVERSATION_REFERENCE_UNRESOLVED"


def test_transaction_analysis_compiles_semantic_filters_to_the_registered_read_capability():
    text = "How much did I spend on coffee this month?"
    task = InterpretedTask(
        intent_id="TRANSACTION_ANALYSIS",
        requested_outcome="EXPLANATION",
        intent_confidence=0.99,
        slot_confidence=0.98,
        entity_confidence=0.99,
        slots={"period": "this_month", "flow": "outflows"},
        entities={"merchant_query": "coffee"},
        evidence=[
            TaskEvidence(field="intent", source="current_turn", quote="spend on coffee"),
            TaskEvidence(field="period", source="current_turn", quote="this month"),
            TaskEvidence(field="flow", source="current_turn", quote="spend"),
            TaskEvidence(field="merchant_query", source="current_turn", quote="coffee"),
        ],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": text},
        messages=[_message("analysis-1", text)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["task_class"] == "mcp_read"
    assert planned["capability_id"] == "banking.transactions.analyze"
    assert planned["tool_name"] == "analyze_customer_transactions"
    assert planned["arguments"] == {
        "period": "this_month",
        "flow": "outflows",
        "measure": "sum",
        "group_by": "none",
        "analysis_mode": "activity",
        "include_transfers": False,
        "merchant_query": "coffee",
    }
    assert planned["semantic_values"]["period"] == "this_month"
    assert planned["semantic_values"]["flow"] == "outflows"


def test_salary_interpretation_compiles_to_conservative_recurring_inflow_screening():
    text = "What is my salary?"
    task = InterpretedTask(
        intent_id="TRANSACTION_ANALYSIS",
        requested_outcome="EXPLANATION",
        intent_confidence=0.99,
        slot_confidence=0.98,
        slots={"analysis_mode": "recurring_inflows", "flow": "inflows"},
        evidence=[
            TaskEvidence(field="intent", source="current_turn", quote="my salary"),
            TaskEvidence(field="analysis_mode", source="current_turn", quote="salary"),
            TaskEvidence(field="flow", source="current_turn", quote="salary"),
        ],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": text},
        messages=[_message("analysis-2", text)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["capability_id"] == "banking.transactions.analyze"
    assert planned["arguments"]["analysis_mode"] == "recurring_inflows"
    assert planned["arguments"]["flow"] == "inflows"
    assert planned["arguments"]["period"] == "last_365_days"
    assert planned["arguments"]["group_by"] == "merchant"
    assert planned["arguments"]["include_transfers"] is False


def test_transaction_analysis_does_not_accept_unsupported_group_or_filter_values():
    text = "Group my spending by employer."
    task = InterpretedTask(
        intent_id="TRANSACTION_ANALYSIS",
        intent_confidence=0.99,
        slot_confidence=0.98,
        slots={"group_by": "employer"},
        evidence=[
            TaskEvidence(field="intent", source="current_turn", quote="spending"),
            TaskEvidence(field="group_by", source="current_turn", quote="by employer"),
        ],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": text},
        messages=[_message("analysis-3", text)],
        context={},
    )
    assert planned["status"] == "CLARIFICATION_REQUIRED"
    assert planned["reason_code"] == "INTENT_SEMANTIC_VALUE_UNSUPPORTED"


def test_transaction_analysis_normalizes_merchant_filter_to_occf_entity_location():
    text = "How much did I spend on coffee?"
    task = InterpretedTask(
        intent_id="TRANSACTION_ANALYSIS",
        requested_outcome="EXPLANATION",
        intent_confidence=0.99,
        slot_confidence=0.98,
        # Common model shape error: a named merchant filter is put in slots.
        # The planner should also transfer its supplied confidence to the
        # OCCF-declared entity dimension.
        slots={"flow": "outflows", "merchant_query": "coffee"},
        evidence=[
            TaskEvidence(field="intent", source="current_turn", quote="spend on coffee"),
            TaskEvidence(field="flow", source="current_turn", quote="spend"),
            TaskEvidence(field="merchant_query", source="current_turn", quote="coffee"),
        ],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": text},
        messages=[_message("analysis-merchant-slot", text)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["arguments"]["flow"] == "outflows"
    assert planned["arguments"]["merchant_query"] == "coffee"


def test_transaction_analysis_uses_registered_period_default_for_coffee_query():
    text = "How much do I spend on coffee?"
    task = InterpretedTask(
        intent_id="TRANSACTION_ANALYSIS",
        requested_outcome="EXPLANATION",
        intent_confidence=0.99,
        slot_confidence=0.98,
        entity_confidence=0.98,
        slots={"flow": "outflows"},
        entities={"merchant_query": "coffee"},
        evidence=[
            TaskEvidence(field="intent", source="current_turn", quote="spend on coffee"),
            TaskEvidence(field="flow", source="current_turn", quote="spend"),
            TaskEvidence(field="merchant_query", source="current_turn", quote="coffee"),
        ],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": text},
        messages=[_message("analysis-default-1", text)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["arguments"]["period"] == "last_30_days"
    assert planned["arguments"]["flow"] == "outflows"
    assert planned["arguments"]["merchant_query"] == "coffee"


def test_salary_query_uses_long_screening_default_without_requiring_employer():
    text = "What was my salary last month?"
    task = InterpretedTask(
        intent_id="TRANSACTION_ANALYSIS",
        requested_outcome="EXPLANATION",
        intent_confidence=0.99,
        slot_confidence=0.98,
        entity_confidence=0.98,
        slots={
            "analysis_mode": "recurring_inflows",
            "flow": "inflows",
            "period": "last_month",
        },
        evidence=[
            TaskEvidence(field="intent", source="current_turn", quote="my salary"),
            TaskEvidence(field="analysis_mode", source="current_turn", quote="salary"),
            TaskEvidence(field="flow", source="current_turn", quote="salary"),
            TaskEvidence(field="period", source="current_turn", quote="last month"),
        ],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": text},
        messages=[_message("analysis-salary-last-month", text)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["arguments"]["period"] == "last_month"
    assert planned["arguments"]["analysis_mode"] == "recurring_inflows"
    assert planned["arguments"]["flow"] == "inflows"
    assert "merchant_query" not in planned["arguments"]


def test_transaction_analysis_recovers_omitted_flow_period_and_merchant_from_registered_contract():
    utterance = "How much did I spend on coffee this month?"
    task = InterpretedTask(
        intent_id="TRANSACTION_ANALYSIS",
        requested_outcome="EXPLANATION",
        intent_confidence=0.99,
        evidence=[TaskEvidence(field="intent", source="current_turn", quote=utterance)],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance},
        messages=[_message("semantic-recovery", utterance)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["arguments"]["period"] == "this_month"
    assert planned["arguments"]["flow"] == "outflows"
    assert planned["arguments"]["measure"] == "sum"
    assert planned["arguments"]["group_by"] == "none"
    assert planned["arguments"]["include_transfers"] is False
    assert planned["arguments"]["merchant_query"] == "coffee"


def test_transaction_analysis_recovers_inflows_from_receive_wording_and_last_30_days():
    utterance = "Did I receive any money in the last 30 days?"
    task = InterpretedTask(
        intent_id="TRANSACTION_ANALYSIS",
        requested_outcome="EXPLANATION",
        intent_confidence=0.99,
        evidence=[TaskEvidence(field="intent", source="current_turn", quote=utterance)],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance},
        messages=[_message("semantic-recovery", utterance)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["arguments"]["period"] == "last_30_days"
    assert planned["arguments"]["flow"] == "inflows"
    assert planned["arguments"]["measure"] == "sum"
    assert planned["arguments"]["include_transfers"] is False
    assert "merchant_query" not in planned["arguments"]


def test_explicit_customer_flow_phrase_overrides_model_all_fallback():
    utterance = "How much did I spend this month?"
    task = InterpretedTask(
        intent_id="TRANSACTION_ANALYSIS",
        requested_outcome="EXPLANATION",
        intent_confidence=0.99,
        slots={"flow": "all"},
        evidence=[
            TaskEvidence(field="intent", source="current_turn", quote=utterance),
        ],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance},
        messages=[_message("semantic-recovery", utterance)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["arguments"]["flow"] == "outflows"


def test_longer_registered_all_activity_phrase_suppresses_nested_flow_words():
    from services.agent_runtime.task_planner import _semantic_value_from_customer_text

    flow_spec = get_registry()["intents"]["TRANSACTION_ANALYSIS"]["execution_plan"][
        "semantic_inputs"
    ]["flow"]
    assert _semantic_value_from_customer_text(flow_spec, "Show income versus expenses") == "all"
