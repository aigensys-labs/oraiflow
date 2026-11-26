from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from shared.contracts.decision_routing import (
    DecisionRoutingPolicy,
    load_decision_routing_policy,
    ordered_pre_assessment_routes,
    task_dispatch_for_class,
)
from shared.contracts.registry import get_registry
from services.agent_runtime.decision_router import (
    build_agent_decision_map,
    classify_intent_task_class,
    render_registered_resource_response,
)
from services.agent_runtime.task_interpretation import (
    InterpretedTask,
    TaskEvidence,
    build_assessment_prompt,
    task_prompt_catalogue,
)
from services.agent_runtime.task_planner import plan_interpreted_task

ROOT = Path(__file__).resolve().parents[1]


def test_decision_routing_policy_is_strict_versioned_and_human_editable():
    policy = load_decision_routing_policy()
    assert policy.schema_version == "1.0.0"
    assert policy.policy_id == "oraiflow.default"
    assert [item.priority for item in policy.pre_assessment_routes] == sorted(
        [item.priority for item in policy.pre_assessment_routes], reverse=True
    )
    assert {item.task_class for item in policy.task_dispatch} == {
        "resource",
        "mcp_read",
        "presentation",
        "workflow",
        "frontend_action",
        "clarification",
        "unsupported",
    }
    assert policy.interpreter.assessment_calls_per_new_turn == 1
    assert policy.interpreter.may_select_tools is False
    assert policy.interpreter.may_authorize_actions is False
    assert policy.confidence.confidence_grants_authority is False


def test_decision_policy_rejects_duplicate_priorities_and_unknown_handlers():
    source = json.loads((ROOT / "shared/contracts/decision_routing.json").read_text())
    duplicate_priority = copy.deepcopy(source)
    duplicate_priority["pre_assessment_routes"][1]["priority"] = duplicate_priority[
        "pre_assessment_routes"
    ][0]["priority"]
    with pytest.raises(ValidationError):
        DecisionRoutingPolicy.model_validate(duplicate_priority)

    unknown_handler = copy.deepcopy(source)
    unknown_handler["pre_assessment_routes"][0]["handler"] = "run_arbitrary_python"
    with pytest.raises(ValidationError):
        DecisionRoutingPolicy.model_validate(unknown_handler)

    wrong_dispatch_owner = copy.deepcopy(source)
    wrong_dispatch_owner["task_dispatch"][0]["handler"] = "occf_read"
    with pytest.raises(ValidationError):
        DecisionRoutingPolicy.model_validate(wrong_dispatch_owner)


def test_decision_policy_can_disable_or_reorder_only_registered_routes():
    policy = load_decision_routing_policy()
    enabled = ordered_pre_assessment_routes(policy)
    assert enabled[0].handler == "academy_personal_read"
    disabled_policy = policy.model_copy(
        update={
            "pre_assessment_routes": [
                (
                    route.model_copy(update={"enabled": False})
                    if route.handler == "academy_personal_read"
                    else route
                )
                for route in policy.pre_assessment_routes
            ]
        }
    )
    assert ordered_pre_assessment_routes(disabled_policy)[0].handler == "workspace_navigation"

    payload = policy.model_dump(mode="json")
    by_handler = {item["handler"]: item for item in payload["pre_assessment_routes"]}
    # Move assistant capability discovery ahead of the navigation route using
    # the human policy priority field, without changing route implementation.
    by_handler["assistant_capabilities"]["priority"] = 110
    by_handler["academy_personal_read"]["priority"] = 100
    payload["pre_assessment_routes"] = sorted(
        by_handler.values(),
        key=lambda item: item["priority"],
        reverse=True,
    )
    reordered = DecisionRoutingPolicy.model_validate(payload)
    reordered_map = build_agent_decision_map(get_registry(), reordered)
    assert (
        reordered_map["policy"]["pre_assessment_routes"][0]["handler"] == "assistant_capabilities"
    )
    assert task_dispatch_for_class("resource", policy).handler == "registered_resource"


def test_decision_map_is_derived_from_occf_and_contains_no_duplicate_phrase_parser():
    registry = get_registry()
    policy = load_decision_routing_policy()
    decision_map = build_agent_decision_map(registry, policy)
    assert {item["intent_id"] for item in decision_map["intents"]} == set(registry["intents"])
    assert {item["capability_id"] for item in decision_map["capabilities"]} <= set(
        registry["capabilities"]
    )
    assert {item["workspace_id"] for item in decision_map["resources"]["workspaces"]} == set(
        registry["application_policy"]["workspaces"]
    )
    assert {item["topic_id"] for item in decision_map["resources"]["academy_topics"]} == {
        item["topic_id"] for item in registry["application_policy"]["academy_topics"]
    }
    assert all(
        "examples" not in item and "keywords" not in item for item in decision_map["intents"]
    )
    transfer = next(
        item for item in decision_map["intents"] if item["intent_id"] == "TRANSFER_PREPARATION"
    )
    assert transfer["optional_slots"] == ["currency", "source_account", "source_account_preference"]
    assert transfer["slot_values"]["source_account_preference"] == [
        "highest_balance",
        "lowest_balance",
    ]
    assert decision_map["policy"]["registry_version"] == "1.5.17"
    assert decision_map["policy"]["schema_version"] == "1.0.0"
    assert "workflow_rule" in decision_map
    assert "The runtime maps validated intent" in " ".join(decision_map["authority_rules"])


def test_human_policy_json_conforms_to_published_json_schema():
    from jsonschema import Draft202012Validator

    policy_payload = json.loads((ROOT / "shared/contracts/decision_routing.json").read_text())
    schema = json.loads((ROOT / "shared/contracts/decision_routing.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(policy_payload)
    # Keep the published schema generated from the strict runtime Pydantic model.
    generated = DecisionRoutingPolicy.model_json_schema()
    generated["$schema"] = schema["$schema"]
    generated["$id"] = schema["$id"]
    assert generated == schema


def test_prompt_map_honors_human_route_priority_and_task_confidence_profiles():
    policy = load_decision_routing_policy()
    decision_map = build_agent_decision_map(get_registry(), policy)
    routes = decision_map["policy"]["pre_assessment_routes"]
    assert [item["priority"] for item in routes] == sorted(
        [item["priority"] for item in routes], reverse=True
    )
    dispatch = decision_map["policy"]["task_dispatch"]
    assert dispatch["workflow"]["applicable_confidence_dimensions"] == ["intent", "slot", "entity"]
    assert dispatch["workflow"]["low_confidence_behavior"] == "preserve_workflow_and_clarify"
    assert dispatch["resource"]["low_confidence_behavior"] == "clarify_resource_or_topic"


def test_intent_task_classes_are_derived_from_registered_execution_plans():
    registry = get_registry()
    assert classify_intent_task_class("ACADEMY_EDUCATION", registry) == "resource"
    assert classify_intent_task_class("WORKSPACE_INFORMATION", registry) == "resource"
    assert classify_intent_task_class("ASSISTANT_CAPABILITY_HELP", registry) == "resource"
    assert classify_intent_task_class("LOAN_INFORMATION", registry) == "mcp_read"
    assert classify_intent_task_class("RECENT_TRANSACTIONS", registry) == "presentation"
    assert classify_intent_task_class("TRANSFER_PREPARATION", registry) == "workflow"


def test_compact_decision_map_is_injected_as_the_single_authoritative_catalogue():
    registry = get_registry()
    decision_map = build_agent_decision_map(registry)
    prompt = build_assessment_prompt(
        intent_candidates=[],
        catalogue=task_prompt_catalogue(registry),
        decision_map=decision_map,
    )
    assert "AUTHORITATIVE ORAIflow DECISION MAP" in prompt
    assert "do not ask the application which tools or skills exist" in prompt.casefold()
    assert "ACADEMY_EDUCATION" in prompt
    assert "TRANSFER_PREPARATION" in prompt
    # Intent semantics are model-visible; executable capability identity is
    # runtime-owned and must not appear in the interpreter prompt.
    assert "banking.contacts.search" not in prompt
    assert '"capability_id"' not in prompt
    assert "Loans" in prompt
    assert "CREDIT_SCORE_BASICS" in prompt
    assert "Never call or select MCP tools" in prompt
    transaction = next(
        item for item in decision_map["intents"] if item["intent_id"] == "TRANSACTION_EXPLANATION"
    )
    selector = transaction["semantic_inputs"]["transaction_selector"]
    assert selector["default"] == "latest"
    assert selector["default_policy"] == "when_unspecified"
    assert selector["allowed_values"] == ["latest"]
    assert "newest" in selector["description"]
    assert "argument_mappings" not in selector
    assert "Task resolution NEEDS_CLARIFICATION is a provisional assessment" in prompt
    assert (
        "the payment I made most recently" not in prompt
    )  # The model must generalize, not memorize one phrase.
    # The new decision map stays materially smaller than the old full catalogue
    # projection (which included every phrase, keyword and complete topic text).
    assert len(json.dumps(decision_map, ensure_ascii=False)) < 24_000
    transaction_intent = next(
        item for item in decision_map["intents"] if item["intent_id"] == "TRANSACTION_ANALYSIS"
    )
    assert "semantic_inputs" in transaction_intent
    assert "argument_mappings" not in json.dumps(decision_map, ensure_ascii=False)
    assert any(
        "TRANSACTION_ANALYSIS" in item.get("serves_intents", [])
        for item in decision_map["capabilities"]
    )


def test_resource_intent_plans_without_fabricating_a_backend_capability():
    text = "What is credit scoring?"
    task = InterpretedTask(
        intent_id="ACADEMY_EDUCATION",
        requested_outcome="EDUCATION",
        resolution="READY",
        entities={"topic_id": "CREDIT_SCORE_BASICS"},
        evidence=[TaskEvidence(field="intent", source="current_turn", quote=text)],
        intent_confidence=0.94,
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": text},
        messages=[SimpleNamespace(type="human", id="turn-resource-1", content=text)],
        context={},
    )
    assert planned["status"] == "RESOURCE_READY"
    assert planned["task_class"] == "resource"
    assert planned["resource_id"] == "academy_topics"
    assert planned["resource_references"] == {"topic_id": "CREDIT_SCORE_BASICS"}
    assert planned.get("capability_id") is None


def test_resource_intent_explains_academy_without_an_executable_mcp_tool():
    text = "What is Academy?"
    task = InterpretedTask(
        intent_id="ACADEMY_EDUCATION",
        requested_outcome="INFORMATION",
        resolution="READY",
        evidence=[TaskEvidence(field="intent", source="current_turn", quote=text)],
        intent_confidence=0.95,
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": text},
        messages=[SimpleNamespace(type="human", id="turn-resource-2", content=text)],
        context={},
    )
    answer = render_registered_resource_response(
        planned["resource_id"],
        planned["resource_references"],
        text,
    )
    assert planned["status"] == "RESOURCE_READY"
    assert answer and "Academy" in answer and "Learning paths" in answer


def test_workspace_resource_intent_resolves_registered_workspace_metadata():
    text = "What is the Loans workspace for?"
    task = InterpretedTask(
        intent_id="WORKSPACE_INFORMATION",
        requested_outcome="EXPLANATION",
        resolution="READY",
        entities={"workspace": "Loans"},
        evidence=[TaskEvidence(field="intent", source="current_turn", quote=text)],
        intent_confidence=0.92,
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": text},
        messages=[SimpleNamespace(type="human", id="turn-resource-3", content=text)],
        context={},
    )
    answer = render_registered_resource_response(
        planned["resource_id"],
        planned["resource_references"],
        text,
    )
    assert planned["status"] == "RESOURCE_READY"
    assert planned["resource_references"]["workspace"] == "Loans"
    assert answer and "Loans" in answer


def test_transfer_intent_contract_exposes_optional_currency_and_source_preferences():
    text = "send 50 EUR to John from account with the highest amount"
    task = InterpretedTask(
        intent_id="TRANSFER_PREPARATION",
        requested_outcome="ACTION",
        resolution="READY",
        slots={"amount": "50", "currency": "EUR", "source_account_preference": "highest_balance"},
        entities={"beneficiary": "John"},
        evidence=[TaskEvidence(field="intent", source="current_turn", quote=text)],
        intent_confidence=0.96,
        slot_confidence=0.94,
        entity_confidence=0.95,
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": text},
        messages=[SimpleNamespace(type="human", id="transfer-highest-1", content=text)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["task_class"] == "workflow"
    assert planned["dispatch_handler"] == "registered_workflow"
    assert planned["capability_id"] == "banking.contacts.search"
    assert planned["assessment"]["slots"]["source_account_preference"] == "highest_balance"


def test_transfer_intent_contract_rejects_undeclared_slots_and_preferences():
    text = "send 50 EUR to John"
    task = InterpretedTask(
        intent_id="TRANSFER_PREPARATION",
        requested_outcome="ACTION",
        resolution="READY",
        slots={
            "amount": "50",
            "currency": "EUR",
            "unknown_router_override": "banking.accounts.list",
        },
        entities={"beneficiary": "John"},
        evidence=[TaskEvidence(field="intent", source="current_turn", quote=text)],
        intent_confidence=0.96,
        slot_confidence=0.94,
        entity_confidence=0.95,
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": text},
        messages=[SimpleNamespace(type="human", id="transfer-invalid-1", content=text)],
        context={},
    )
    assert planned["status"] == "REJECTED"
    assert planned["reason_code"] == "UNDECLARED_INTENT_SLOT"

    valid_fields_but_invalid_value = task.model_copy(
        update={
            "slots": {"amount": "50", "currency": "EUR", "source_account_preference": "largest"}
        }
    )
    planned = plan_interpreted_task(
        valid_fields_but_invalid_value,
        sources={"current_turn": text},
        messages=[SimpleNamespace(type="human", id="transfer-invalid-2", content=text)],
        context={},
    )
    assert planned["status"] == "CLARIFICATION_REQUIRED"
    assert planned["reason_code"] == "INTENT_SLOT_VALUE_UNSUPPORTED"


def test_decision_policy_does_not_define_or_duplicate_capability_schemas():
    policy_payload = json.loads((ROOT / "shared/contracts/decision_routing.json").read_text())
    assert "capabilities" not in policy_payload
    assert "input_schema" not in json.dumps(policy_payload)
    assert "authorization" not in policy_payload
