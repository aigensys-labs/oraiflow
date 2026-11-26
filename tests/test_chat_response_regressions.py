from types import SimpleNamespace

import pytest

from services.agent_runtime.academy import (
    academy_direct_guidance_for_request,
    academy_topic_matches,
)
from services.agent_runtime.intent_contracts import discover_intent_candidates
from services.agent_runtime.interpretation import validate_interpretation_assessment
from services.agent_runtime.task_interpretation import (
    InterpretedTask,
    TaskEvidence,
    build_assessment_prompt,
    task_prompt_catalogue,
    rank_saved_contacts_for_context,
)
from services.agent_runtime.task_planner import plan_interpreted_task
from shared.contact_resolution import extract_contact_transfer_request
from shared.contracts.registry import get_registry


def _human(text: str):
    return SimpleNamespace(type="human", id="test-human", content=text)


def test_common_transfer_preparation_wording_is_an_advisory_candidate_not_an_auto_route():
    candidates = discover_intent_candidates("Help me prepare a transfer of 50 EUR.")
    transfer = next(item for item in candidates if item["intent_id"] == "TRANSFER_PREPARATION")
    assert transfer["match_kind"] == "semantic_phrase"
    assert transfer["auto_resolve"] is False
    assert transfer["matched_phrase"] == "help me prepare a transfer"


def test_transfer_parser_never_treats_preposition_of_as_recipient():
    text = "Help me prepare a transfer of 50 EUR."
    assert extract_contact_transfer_request(text, allow_currency_missing=True) is None


def test_transfer_parser_still_extracts_explicit_pay_name_amount_and_currency():
    parsed = extract_contact_transfer_request("Pay John 50 EUR", allow_currency_missing=True)
    assert parsed is not None
    assert parsed["beneficiary_query"] == "John"
    assert parsed["amount"] == "50"
    assert parsed["currency"] == "EUR"


def test_credit_scores_plural_matches_registered_credit_score_topic():
    matches = academy_topic_matches("Credit scores depend on")
    assert matches
    assert matches[0][0]["topic_id"] == "CREDIT_SCORE_BASICS"
    answer = academy_direct_guidance_for_request("Credit scores depend on")
    assert answer
    assert "Credit scores" in answer or "credit score" in answer.casefold()


def test_saved_contact_lookup_is_a_distinct_llm_assessed_read_only_intent():
    utterance = "Find John in my saved contacts."
    candidates = discover_intent_candidates(utterance)
    candidate = next(item for item in candidates if item["intent_id"] == "SAVED_CONTACT_LOOKUP")
    assert candidate["auto_resolve"] is False

    catalogue = task_prompt_catalogue()
    contact_intent = next(item for item in catalogue if item["intent_id"] == "SAVED_CONTACT_LOOKUP")
    assert contact_intent["required_entities"] == ["contact_name"]
    assert "standalone read-only lookup" in contact_intent["entity_descriptions"]["contact_name"]

    prompt = build_assessment_prompt(
        intent_candidates=candidates,
        catalogue=catalogue,
    )
    assert "SAVED_CONTACT_LOOKUP" in prompt
    assert "Do not ask what the customer wants to do with John" in prompt
    assert "always return an InterpretedTask" in prompt
    assert "prevents a coherent follow-up" in prompt


def test_named_saved_contact_is_prioritized_in_bounded_interpreter_context():
    contacts = [{"full_name": f"Person {index}", "status": "ACTIVE"} for index in range(100)] + [
        {"full_name": "John Smith", "status": "ACTIVE"}
    ]
    selected = rank_saved_contacts_for_context(
        contacts,
        "Find John in my saved contacts.",
        limit=20,
    )
    assert selected[0] == {"name": "John Smith", "status": "ACTIVE"}
    prompt = build_assessment_prompt(
        intent_candidates=discover_intent_candidates("Find John in my saved contacts."),
        catalogue=task_prompt_catalogue(),
        application_context={"saved_contacts": selected},
    )
    assert "John Smith" in prompt


def test_unrelated_financial_analysis_does_not_receive_arbitrary_saved_contacts():
    contacts = [{"full_name": f"Person {index}", "status": "ACTIVE"} for index in range(100)]
    selected = rank_saved_contacts_for_context(
        contacts,
        "How much did I spend on coffee?",
    )
    assert selected == []


def test_saved_contact_lookup_plans_existing_contacts_capability_without_transfer_fields():
    utterance = "Find John in my saved contacts."
    task = InterpretedTask(
        intent_id="SAVED_CONTACT_LOOKUP",
        requested_outcome="INFORMATION",
        resolution="READY",
        entities={"contact_name": "John"},
        evidence=[
            TaskEvidence(field="intent", source="current_turn", quote=utterance),
            TaskEvidence(field="contact_name", source="current_turn", quote="John"),
        ],
        intent_confidence=0.97,
        entity_confidence=0.96,
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance},
        messages=[_human(utterance)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["intent_id"] == "SAVED_CONTACT_LOOKUP"
    assert planned["capability_id"] == "banking.contacts.search"
    assert planned["arguments"] == {"name_query": "John"}
    assert planned["kind"] == "mcp"


def test_saved_contact_lookup_assessment_does_not_require_transfer_amount_or_currency(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    assessment, decision = validate_interpretation_assessment(
        {
            "intent_id": "SAVED_CONTACT_LOOKUP",
            "intent_confidence": 0.97,
            "slot_confidence": None,
            "entity_confidence": 0.96,
            "capability_id": "banking.contacts.search",
            "capability_selection_confidence": None,
            "resolution": "READY",
            "slots": {},
            "entities": {"contact_name": "John"},
            "evidence": [
                {
                    "field": "intent",
                    "source": "current_turn",
                    "quote": "Find John in my saved contacts.",
                },
                {"field": "contact_name", "source": "current_turn", "quote": "John"},
            ],
        },
        expected_capability_id="banking.contacts.search",
        sources={"current_turn": "Find John in my saved contacts."},
        applicable_confidence_dimensions={"intent", "entity"},
    )
    assert assessment is not None
    assert decision["decision"] == "PASS"


def test_saved_contact_lookup_intent_is_registered_against_existing_read_only_capability():
    intent = get_registry()["intents"]["SAVED_CONTACT_LOOKUP"]
    plan = intent["execution_plan"]
    capability = get_registry()["capabilities"][plan["capability"]]
    assert plan["capability"] == "banking.contacts.search"
    assert capability["execution"]["effect"] == "read_only"
    assert capability["authorization"]["required_capability"] == "contacts:read"
    assert "TRANSFER_PREPARATION" not in intent["description"]


def test_transfer_named_account_with_generic_account_suffix_is_normalized_from_context():
    utterance = "Lucas from Everyday account"
    prior = "Help me prepare a transfer of 50 EUR."
    task = InterpretedTask(
        intent_id="TRANSFER_PREPARATION",
        requested_outcome="ACTION",
        resolution="READY",
        slots={
            "amount": "50",
            "currency": "EUR",
            "source_account_preference": "Everyday account",
        },
        entities={"beneficiary": "Lucas"},
        evidence=[
            TaskEvidence(field="intent", source="conversation", quote=prior),
            TaskEvidence(field="amount", source="conversation", quote="50 EUR"),
            TaskEvidence(field="currency", source="conversation", quote="EUR"),
            TaskEvidence(field="beneficiary", source="current_turn", quote="Lucas"),
            TaskEvidence(
                field="source_account_preference", source="current_turn", quote="Everyday account"
            ),
        ],
        intent_confidence=0.98,
        slot_confidence=0.97,
        entity_confidence=0.97,
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance, "conversation": prior + "\n" + utterance},
        messages=[_human(prior), _human(utterance)],
        context={
            "accounts": [{"account_id": "acct-ui-1", "account_name": "Everyday", "currency": "EUR"}]
        },
    )
    assert planned["status"] == "PLANNED"
    assert planned["assessment"]["slots"]["source_account"] == "Everyday"
    assert "source_account_preference" not in planned["assessment"]["slots"]


def test_latest_transaction_explicit_phrase_has_safe_defaulted_contract_recovery():
    from services.agent_runtime.task_interpretation import safe_canonical_read_assessment

    utterance = "Explain my latest transaction."
    candidates = discover_intent_candidates(utterance)
    assessment = safe_canonical_read_assessment(utterance, candidates)
    assert assessment is not None
    assert assessment.tasks[0].intent_id == "TRANSACTION_EXPLANATION"
    planned = plan_interpreted_task(
        assessment.tasks[0],
        sources={"current_turn": utterance, "conversation": utterance},
        messages=[_human(utterance)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["arguments"]["limit"] == 1
    assert planned["semantic_values"]["transaction_selector"] == "latest"


def test_planner_preserves_partial_transfer_as_a_specific_missing_beneficiary_task():
    utterance = "Help me prepare a transfer of 50 EUR."
    task = InterpretedTask(
        intent_id="TRANSFER_PREPARATION",
        requested_outcome="ACTION",
        resolution="NEEDS_CLARIFICATION",
        slots={"amount": "50", "currency": "EUR"},
        entities={},
        evidence=[
            TaskEvidence(field="intent", source="current_turn", quote=utterance),
            TaskEvidence(field="amount", source="current_turn", quote="50"),
            TaskEvidence(field="currency", source="current_turn", quote="EUR"),
        ],
        intent_confidence=0.97,
        slot_confidence=0.96,
        entity_confidence=None,
        clarification_question="Who should receive the transfer?",
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": utterance},
        messages=[_human(utterance)],
        context={
            "accounts": [{"account_id": "acct-1", "account_name": "Everyday", "currency": "EUR"}]
        },
    )
    assert planned["status"] == "CLARIFICATION_REQUIRED"
    assert "beneficiary" in planned["missing_fields"]
    assert (
        "50" not in planned["clarification_question"]
        or "amount" not in planned["clarification_question"].casefold()
    )


def test_transfer_named_account_preference_is_normalized_to_source_account_from_context():
    utterance = "Lucas from everyday account"
    prior = "Help me prepare a transfer of 50 EUR."
    task = InterpretedTask(
        intent_id="TRANSFER_PREPARATION",
        requested_outcome="ACTION",
        resolution="READY",
        slots={
            "amount": "50",
            "currency": "EUR",
            "source_account_preference": "Everyday",
        },
        entities={"beneficiary": "Lucas"},
        evidence=[
            TaskEvidence(field="intent", source="conversation", quote=prior),
            TaskEvidence(field="amount", source="conversation", quote="50 EUR"),
            TaskEvidence(field="currency", source="conversation", quote="EUR"),
            TaskEvidence(field="beneficiary", source="current_turn", quote="Lucas"),
            TaskEvidence(
                field="source_account_preference", source="current_turn", quote="everyday account"
            ),
        ],
        intent_confidence=0.98,
        slot_confidence=0.97,
        entity_confidence=0.97,
    )
    planned = plan_interpreted_task(
        task,
        sources={
            "current_turn": utterance,
            "conversation": prior + "\n" + utterance,
        },
        messages=[_human(prior), _human(utterance)],
        context={
            "accounts": [{"account_id": "acct-ui-1", "account_name": "Everyday", "currency": "EUR"}]
        },
    )
    assert planned["status"] == "PLANNED"
    assert planned["assessment"]["slots"]["source_account"] == "Everyday"
    assert "source_account_preference" not in planned["assessment"]["slots"]
