import json
from types import SimpleNamespace

from services.agent_runtime.task_interpretation import (
    InterpretedTask,
    TaskEvidence,
    TurnInterpretationAssessment,
    allowed_conversation_references,
    intent_validation_capability,
    parse_assessment_json,
    missing_task_field_evidence,
    repair_registered_semantic_evidence,
    safe_customer_clarification,
    task_capability_plan,
    task_prompt_catalogue,
)
from shared.contracts.registry import get_registry


def test_assessment_contract_is_versioned_and_rejects_unknown_fields():
    payload = {
        "schema_version": "1.0.0",
        "resolution": "RESOLVED",
        "tasks": [
            {
                "intent_id": "LOAN_INFORMATION",
                "requested_outcome": "EXPLANATION",
                "resolution": "READY",
                "slots": {},
                "entities": {},
                "evidence": [],
                "intent_confidence": 0.9,
                "slot_confidence": None,
                "entity_confidence": None,
                "clarification_question": None,
                "conversation_references": [],
            }
        ],
    }
    parsed, info = parse_assessment_json(json.dumps(payload))
    assert parsed is not None
    assert info["task_count"] == 1

    payload["tasks"][0]["capability_id"] = "banking.loans.overview"
    parsed, info = parse_assessment_json(json.dumps(payload))
    assert parsed is None
    assert info["code"] == "ASSESSMENT_SCHEMA_INVALID"


def test_assessment_only_contract_contains_no_capability_selection():
    schema = TurnInterpretationAssessment.model_json_schema()
    serialized = json.dumps(schema)
    assert "capability_id" not in serialized
    assert "tool_name" not in serialized


def test_runtime_maps_intents_to_registered_capabilities_not_model_choice():
    registry = get_registry()
    assert intent_validation_capability("LOAN_INFORMATION", registry) == "banking.loans.overview"
    assert (
        intent_validation_capability("TRANSFER_PREPARATION", registry) == "banking.contacts.search"
    )
    assert intent_validation_capability("ACCOUNT_OVERVIEW", registry) == "banking.accounts.list"
    plan = task_capability_plan("FINANCIAL_HEALTH_EXPLANATION", registry)
    assert plan["capability_id"] == "banking.financial_health.overview"


def test_conversation_references_must_resolve_to_actual_message_ids():
    task = InterpretedTask(
        intent_id="WORKSPACE_NAVIGATION",
        conversation_references=["message-1"],
    )
    messages = [SimpleNamespace(id="message-1"), SimpleNamespace(id="message-2")]
    assert allowed_conversation_references(task, messages)
    assert not allowed_conversation_references(
        task, [SimpleNamespace(id="message-2"), SimpleNamespace(id="message-3")]
    )


def test_prompt_catalogue_is_semantic_only_and_registry_backed():
    catalogue = task_prompt_catalogue()
    assert catalogue
    assert all(
        {
            "intent_id",
            "description",
            "response_mode",
            "examples",
            "keywords",
            "required_slots",
            "required_entities",
        }.issubset(item)
        for item in catalogue
    )
    assert {item["intent_id"] for item in catalogue} == set(get_registry()["intents"])
    transfer = next(item for item in catalogue if item["intent_id"] == "TRANSFER_PREPARATION")
    assert "send money to" in transfer["examples"]
    assert transfer["required_slots"] == ["amount"]
    assert transfer["required_entities"] == ["beneficiary"]


def test_planner_uses_registry_capability_for_loan_read_without_model_capability_field():
    from services.agent_runtime.task_planner import plan_interpreted_task

    task = InterpretedTask(
        intent_id="LOAN_INFORMATION",
        requested_outcome="EXPLANATION",
        intent_confidence=0.95,
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": "Walk me through my loan schedule."},
        messages=[
            SimpleNamespace(type="human", id="turn-1", content="Walk me through my loan schedule.")
        ],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["capability_id"] == "banking.loans.overview"
    assert planned["tool_name"] == "get_customer_loan_overview"
    assert planned["arguments"] == {}


def test_planner_refuses_transfer_when_recipient_is_not_grounded():
    from services.agent_runtime.task_planner import plan_interpreted_task

    task = InterpretedTask(
        intent_id="TRANSFER_PREPARATION",
        requested_outcome="ACTION",
        intent_confidence=0.95,
        slots={"amount": "50"},
        evidence=[],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": "Send 50 EUR."},
        messages=[SimpleNamespace(type="human", id="turn-2", content="Send 50 EUR.")],
        context={},
    )
    assert planned["status"] == "CLARIFICATION_REQUIRED"
    assert planned["reason_code"] in {
        "VALUE_EVIDENCE_MISSING",
        "REQUIRED_INPUT_MISSING",
        "BENEFICIARY_NOT_EXTRACTED",
    }


def test_missing_field_evidence_identifies_proposed_slots_without_quotations():
    assessment = TurnInterpretationAssessment(
        resolution="RESOLVED",
        tasks=[
            InterpretedTask(
                intent_id="TRANSACTION_ANALYSIS",
                slots={"period": "this_month", "flow": "outflows"},
                entities={"merchant_query": "coffee"},
                evidence=[
                    TaskEvidence(
                        field="intent",
                        source="current_turn",
                        quote="How much did I spend on coffee this month?",
                    ),
                    TaskEvidence(field="merchant_query", source="current_turn", quote="coffee"),
                ],
            )
        ],
    )
    assert missing_task_field_evidence(assessment) == [
        {
            "task_index": 0,
            "intent_id": "TRANSACTION_ANALYSIS",
            "fields": ["flow", "period"],
        }
    ]


def test_complete_field_evidence_does_not_trigger_repair():
    assessment = TurnInterpretationAssessment(
        resolution="RESOLVED",
        tasks=[
            InterpretedTask(
                intent_id="TRANSACTION_ANALYSIS",
                slots={"period": "this_month", "flow": "outflows"},
                entities={"merchant_query": "coffee"},
                evidence=[
                    TaskEvidence(
                        field="intent",
                        source="current_turn",
                        quote="How much did I spend on coffee this month?",
                    ),
                    TaskEvidence(field="period", source="current_turn", quote="this month"),
                    TaskEvidence(field="flow", source="current_turn", quote="spend"),
                    TaskEvidence(field="merchant_query", source="current_turn", quote="coffee"),
                ],
            )
        ],
    )
    assert missing_task_field_evidence(assessment) == []


def test_registered_semantic_evidence_repairs_coffee_spending_without_losing_this_month():
    from services.agent_runtime.task_planner import plan_interpreted_task

    current_text = "How much did I spend on coffee this month?"
    assessment = TurnInterpretationAssessment(
        resolution="RESOLVED",
        tasks=[
            InterpretedTask(
                intent_id="TRANSACTION_ANALYSIS",
                requested_outcome="INFORMATION",
                resolution="READY",
                slots={
                    # Deliberately wrong semantic values with superficially plausible
                    # quotes: the registered phrase map must correct these values.
                    "period": "last_30_days",
                    "flow": "inflows",
                    "measure": "count",
                    "group_by": "merchant",
                    "analysis_mode": "activity",
                    "transfer_scope": "exclude_category_transfers",
                },
                entities={"merchant_query": "coffee"},
                evidence=[
                    TaskEvidence(field="intent", source="current_turn", quote=current_text),
                    TaskEvidence(field="period", source="current_turn", quote="this month"),
                    TaskEvidence(field="flow", source="current_turn", quote="spend"),
                    TaskEvidence(field="measure", source="current_turn", quote="How much"),
                    TaskEvidence(field="merchant_query", source="current_turn", quote="coffee"),
                ],
            )
        ],
    )

    repaired, info = repair_registered_semantic_evidence(assessment, current_text)
    task = repaired.tasks[0]
    assert task.slots["period"] == "this_month"
    assert task.slots["flow"] == "outflows"
    assert task.slots["measure"] == "sum"
    assert "group_by" not in task.slots
    assert "analysis_mode" not in task.slots
    assert "transfer_scope" not in task.slots
    assert {item.field: item.quote for item in task.evidence} == {
        "intent": current_text,
        "period": "this month",
        "flow": "spend",
        "measure": "How much",
        "merchant_query": "coffee",
    }
    assert missing_task_field_evidence(repaired) == []
    assert "group_by" in info["defaulted_fields"]

    planned = plan_interpreted_task(
        task,
        sources={"current_turn": current_text},
        messages=[SimpleNamespace(type="human", id="turn-coffee", content=current_text)],
        context={},
    )
    assert planned["status"] == "PLANNED", planned
    assert planned["capability_id"] == "banking.transactions.analyze"
    assert planned["arguments"]["period"] == "this_month"
    assert planned["arguments"]["flow"] == "outflows"
    assert planned["arguments"]["measure"] == "sum"
    assert planned["arguments"]["merchant_query"] == "coffee"
    assert planned["arguments"]["group_by"] == "none"


def test_registered_semantic_evidence_uses_conditional_salary_default():
    current_text = "What was my recent salary?"
    assessment = TurnInterpretationAssessment(
        tasks=[
            InterpretedTask(
                intent_id="TRANSACTION_ANALYSIS",
                slots={
                    "period": "last_30_days",
                    "flow": "inflows",
                    "analysis_mode": "recurring_inflows",
                    "measure": "sum",
                },
                evidence=[TaskEvidence(field="intent", source="current_turn", quote=current_text)],
            )
        ],
    )
    repaired, _ = repair_registered_semantic_evidence(assessment, current_text)
    task = repaired.tasks[0]
    assert "period" not in task.slots  # planner applies the conditional 365-day default
    assert task.slots["analysis_mode"] == "recurring_inflows"
    assert task.slots["flow"] == "inflows"
    assert {item.field: item.quote for item in task.evidence}["analysis_mode"] == "salary"
    assert {item.field: item.quote for item in task.evidence}["flow"] == "salary"


def test_unrecognized_explicit_period_is_not_replaced_by_registered_default():
    current_text = "How much did I spend on coffee next month?"
    assessment = TurnInterpretationAssessment(
        tasks=[
            InterpretedTask(
                intent_id="TRANSACTION_ANALYSIS",
                slots={"period": "this_month", "flow": "outflows"},
                entities={"merchant_query": "coffee"},
                evidence=[TaskEvidence(field="intent", source="current_turn", quote=current_text)],
            )
        ],
    )
    repaired, _ = repair_registered_semantic_evidence(assessment, current_text)
    task = repaired.tasks[0]
    assert task.slots["period"] == "this_month"
    assert "period" in missing_task_field_evidence(repaired)[0]["fields"]


def test_semantic_evidence_must_support_the_exact_proposed_value():
    from services.agent_runtime.task_planner import plan_interpreted_task

    current_text = "How much did I spend on coffee?"
    task = InterpretedTask(
        intent_id="TRANSACTION_ANALYSIS",
        slots={"flow": "inflows"},
        entities={"merchant_query": "coffee"},
        evidence=[
            TaskEvidence(field="intent", source="current_turn", quote=current_text),
            # The quote is real, but it supports outflows, not inflows.
            TaskEvidence(field="flow", source="current_turn", quote="spend"),
            TaskEvidence(field="merchant_query", source="current_turn", quote="coffee"),
        ],
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": current_text},
        messages=[SimpleNamespace(type="human", id="turn-wrong-flow", content=current_text)],
        context={},
    )
    assert planned["status"] == "CLARIFICATION_REQUIRED"
    assert planned["reason_code"] == "VALUE_EVIDENCE_MISSING"


def test_recent_salary_request_uses_recurring_inflows_and_conditional_365_day_window():
    from services.agent_runtime.intent_contracts import discover_intent_candidates
    from services.agent_runtime.task_planner import plan_interpreted_task

    current_text = "What was my recent salary?"
    candidates = discover_intent_candidates(current_text)
    candidate = next(item for item in candidates if item["intent_id"] == "TRANSACTION_ANALYSIS")
    assert candidate["match_kind"] == "semantic_phrase"

    assessment = TurnInterpretationAssessment(
        tasks=[
            InterpretedTask(
                intent_id="TRANSACTION_ANALYSIS",
                slots={"analysis_mode": "activity", "flow": "all", "period": "last_30_days"},
                evidence=[TaskEvidence(field="intent", source="current_turn", quote=current_text)],
            )
        ]
    )
    repaired, _ = repair_registered_semantic_evidence(assessment, current_text)
    task = repaired.tasks[0]
    assert task.slots["analysis_mode"] == "recurring_inflows"
    assert task.slots["flow"] == "inflows"
    assert "period" not in task.slots

    planned = plan_interpreted_task(
        task,
        sources={"current_turn": current_text},
        messages=[SimpleNamespace(type="human", id="turn-salary", content=current_text)],
        context={},
    )
    assert planned["status"] == "PLANNED", planned
    assert planned["capability_id"] == "banking.transactions.analyze"
    assert planned["arguments"]["analysis_mode"] == "recurring_inflows"
    assert planned["arguments"]["flow"] == "inflows"
    assert planned["arguments"]["period"] == "last_365_days"


def test_model_clarification_cannot_echo_internal_transaction_capability_description():
    leaked_description = (
        "Analyze the authenticated customer's booked transactions using registered date windows, "
        "inflow/outflow filters, merchant/description filters and deterministic sums."
    )
    result = safe_customer_clarification(
        "I found a likely match for your request: " + leaked_description,
        [{"intent_id": "TRANSACTION_ANALYSIS", "description": leaked_description}],
    )
    assert "authenticated" not in result.casefold()
    assert "registered date windows" not in result.casefold()
    assert "deterministic sums" not in result.casefold()
    assert "recorded spending" in result
    assert "haven't retrieved transaction data" in result


def test_model_clarification_is_preserved_when_no_candidate_metadata_was_supplied():
    result = safe_customer_clarification(
        "Which part of the loan schedule would you like explained?", []
    )
    assert result == "Which part of the loan schedule would you like explained?"
