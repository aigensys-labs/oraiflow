from __future__ import annotations

import pytest

from services.agent_runtime.interpretation import (
    confidence_gate_configuration,
    validate_interpretation_assessment,
    with_required_interpretation_assessment,
)


def transfer_assessment():
    return {
        "intent_id": "TRANSFER_PREPARATION",
        "intent_confidence": 0.94,
        "slot_confidence": 0.96,
        "entity_confidence": 0.72,
        "capability_id": "banking.contacts.search",
        "capability_selection_confidence": 0.91,
        "resolution": "READY",
        "slots": {"amount": "50"},
        "entities": {"beneficiary": "John"},
        "evidence": [
            {"field": "intent", "source": "current_turn", "quote": "Send 50 to John"},
            {"field": "amount", "source": "current_turn", "quote": "50"},
            {"field": "beneficiary", "source": "current_turn", "quote": "John"},
        ],
    }


def test_four_dimensional_assessment_passes_in_observe_mode(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    for name in (
        "ORAI_INTENT_CONFIDENCE_THRESHOLD",
        "ORAI_SLOT_CONFIDENCE_THRESHOLD",
        "ORAI_ENTITY_CONFIDENCE_THRESHOLD",
        "ORAI_CAPABILITY_CONFIDENCE_THRESHOLD",
    ):
        monkeypatch.delenv(name, raising=False)
    assessment, decision = validate_interpretation_assessment(
        transfer_assessment(),
        expected_capability_id="banking.contacts.search",
        sources={"current_turn": "Send 50 to John", "conversation": "Send 50 to John"},
    )
    assert assessment is not None
    assert decision["decision"] == "PASS"
    assert decision["confidence_mode"] == "observe"
    assert set(decision["scores"]) == {"intent", "slot", "entity", "capability"}


def test_numeric_evidence_uses_token_boundaries(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    payload = transfer_assessment()
    payload["slots"]["amount"] = "500"
    assessment, decision = validate_interpretation_assessment(
        payload,
        expected_capability_id="banking.contacts.search",
        sources={"current_turn": "Send 50 to John", "conversation": "Send 50 to John"},
    )
    assert assessment is not None
    assert decision["code"] == "VALUE_EVIDENCE_MISSING"


def test_wrong_capability_and_intent_relationship_is_rejected(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    payload = transfer_assessment()
    payload["capability_id"] = "banking.transactions.recent"
    _, decision = validate_interpretation_assessment(
        payload,
        expected_capability_id="banking.transactions.recent",
        sources={"current_turn": "Send 50 to John", "conversation": "Send 50 to John"},
    )
    assert decision["code"] == "INTENT_CAPABILITY_MISMATCH"


def test_confidence_enforcement_requires_all_four_explicit_thresholds(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "enforce")
    monkeypatch.setenv("ORAI_INTENT_CONFIDENCE_THRESHOLD", "0.8")
    monkeypatch.setenv("ORAI_SLOT_CONFIDENCE_THRESHOLD", "0.8")
    monkeypatch.setenv("ORAI_ENTITY_CONFIDENCE_THRESHOLD", "0.8")
    monkeypatch.delenv("ORAI_CAPABILITY_CONFIDENCE_THRESHOLD", raising=False)
    config = confidence_gate_configuration()
    assert config["valid"] is False
    assert config["ready"] is False
    assert config["missing"] == ["ORAI_CAPABILITY_CONFIDENCE_THRESHOLD"]


def test_enforced_low_entity_confidence_requests_clarification(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "enforce")
    monkeypatch.setenv("ORAI_INTENT_CONFIDENCE_THRESHOLD", "0.8")
    monkeypatch.setenv("ORAI_SLOT_CONFIDENCE_THRESHOLD", "0.8")
    monkeypatch.setenv("ORAI_ENTITY_CONFIDENCE_THRESHOLD", "0.8")
    monkeypatch.setenv("ORAI_CAPABILITY_CONFIDENCE_THRESHOLD", "0.8")
    payload = transfer_assessment()
    payload["intent_id"] = "TRANSACTION_EXPLANATION"
    payload["capability_id"] = "banking.transactions.recent"
    payload["slots"] = {}
    payload["entities"] = {}
    payload["evidence"] = [
        {"field": "intent", "source": "current_turn", "quote": "Explain my last payment"}
    ]
    _, decision = validate_interpretation_assessment(
        payload,
        expected_capability_id="banking.transactions.recent",
        sources={"current_turn": "Explain my last payment", "conversation": "Explain my last payment"},
    )
    assert decision["decision"] == "CLARIFY"
    assert decision["dimensions"] == ["entity"]


def test_natural_language_comparative_preference_is_a_grounded_semantic_enum(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    payload = {
        "intent_id": "TRANSFER_PREPARATION",
        "intent_confidence": 0.95,
        "slot_confidence": 0.89,
        "entity_confidence": 0.91,
        "capability_id": "banking.accounts.list",
        "capability_selection_confidence": 0.94,
        "resolution": "READY",
        "slots": {"source_account_preference": "highest_balance"},
        "entities": {},
        "evidence": [
            {"field": "intent", "source": "current_turn", "quote": "with the highest amount"},
            {"field": "source_account_preference", "source": "current_turn", "quote": "with the highest amount"},
        ],
    }
    _, decision = validate_interpretation_assessment(
        payload,
        expected_capability_id="banking.accounts.list",
        sources={"current_turn": "with the highest amount", "conversation": "send 70 to Lucas\nwith the highest amount"},
    )
    assert decision["decision"] == "PASS"


def test_contact_candidate_search_defers_entity_threshold_until_verified_candidates_exist(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "enforce")
    monkeypatch.setenv("ORAI_INTENT_CONFIDENCE_THRESHOLD", "0.8")
    monkeypatch.setenv("ORAI_SLOT_CONFIDENCE_THRESHOLD", "0.8")
    monkeypatch.setenv("ORAI_ENTITY_CONFIDENCE_THRESHOLD", "0.8")
    monkeypatch.setenv("ORAI_CAPABILITY_CONFIDENCE_THRESHOLD", "0.8")
    payload = transfer_assessment()
    payload["entity_confidence"] = 0.55
    _, decision = validate_interpretation_assessment(
        payload,
        expected_capability_id="banking.contacts.search",
        sources={"current_turn": "Send 50 to John", "conversation": "Send 50 to John"},
    )
    assert decision["decision"] == "PASS"
    assert decision["deferred_dimensions"] == ["entity"]


def test_frontend_action_schema_adds_flat_assessment_transport_without_mutating_browser_contract():
    from services.agent_runtime.interpretation import add_assessment_parameter

    original = {
        "name": "select_account",
        "description": "Select an account",
        "parameters": {
            "type": "object",
            "properties": {"account_id": {"type": "string"}},
            "required": ["account_id"],
            "additionalProperties": False,
        },
    }
    enriched = add_assessment_parameter(original)
    assert "orai_intent_id" in enriched["parameters"]["properties"]
    assert "orai_intent_confidence" in enriched["parameters"]["properties"]
    assert "orai_evidence" in enriched["parameters"]["properties"]
    assert "interpretation_assessment" not in enriched["parameters"]["properties"]
    assert "orai_intent_id" not in original["parameters"]["properties"]
    assert enriched["parameters"]["properties"]["orai_evidence"]["items"]["type"] == "string"


def test_registered_account_overview_supports_its_declared_accounts_read():
    from services.agent_runtime.interpretation import _intent_supports_capability

    assert _intent_supports_capability("ACCOUNT_OVERVIEW", "banking.accounts.list")
    assert _intent_supports_capability("ACCOUNT_TOTAL_BALANCE", "banking.accounts.list")
    assert not _intent_supports_capability("ACCOUNT_TOTAL_BALANCE", "banking.accounts.balance")


def test_model_facing_mcp_tool_schema_uses_flat_assessment_transport():
    from pydantic import BaseModel

    class ReadInput(BaseModel):
        limit: int = 10

    assessed = with_required_interpretation_assessment(ReadInput, "AssessedReadInput")
    assert "orai_intent_id" in assessed.model_fields
    assert "orai_intent_confidence" in assessed.model_fields
    assert "orai_evidence" in assessed.model_fields
    assert "interpretation_assessment" not in assessed.model_fields
    assert "orai_intent_id" not in ReadInput.model_fields
    provider_schema = assessed.model_json_schema()
    assert "$defs" not in provider_schema
    assert "interpretation_assessment" not in provider_schema.get("properties", {})
    # Local models commonly emit amount as a JSON number. The provider-facing
    # schema must accept it so the runtime can normalize it to a canonical str.
    parsed = assessed.model_validate({
        "limit": 10,
        "name_query": "John",
        "orai_intent_id": "TRANSFER_PREPARATION",
        "orai_slots": {"amount": 50, "currency": "EUR"},
    })
    assert parsed.orai_slots["amount"] == 50
    from services.agent_runtime.interpretation import assessment_from_tool_arguments
    canonical, _ = assessment_from_tool_arguments(
        parsed.model_dump(), expected_capability_id="banking.contacts.search",
        inferred_intent_id="TRANSFER_PREPARATION",
    )
    assert canonical["slots"]["amount"] == "50"


def test_assessment_schema_defaults_empty_collections_without_inventing_scores():
    from services.agent_runtime.interpretation import InterpretationAssessment

    assessment = InterpretationAssessment.model_validate({
        "intent_id": "TRANSFER_PREPARATION",
        "capability_id": "banking.contacts.search",
    })
    assert assessment.resolution == "READY"
    assert assessment.slots == {}
    assert assessment.entities == {}
    assert assessment.evidence == []
    assert assessment.intent_confidence is None
    assert assessment.slot_confidence is None
    assert assessment.entity_confidence is None
    assert assessment.capability_selection_confidence is None


def test_workspace_reference_can_be_resolved_from_prior_assistant_context(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    payload = {
        "intent_id": "WORKSPACE_NAVIGATION",
        "intent_confidence": 0.96,
        "slot_confidence": 0.91,
        "entity_confidence": 0.94,
        "capability_id": "frontend.workspace.navigate",
        "capability_selection_confidence": 0.95,
        "resolution": "READY",
        "slots": {},
        "entities": {"workspace": "Controls & audit"},
        "evidence": [
            {"field": "intent", "source": "current_turn", "quote": "take me there"},
            {"field": "workspace", "source": "assistant_context", "quote": "The audit view is in the Controls & audit workspace."},
        ],
    }
    assessment, decision = validate_interpretation_assessment(
        payload,
        expected_capability_id="frontend.workspace.navigate",
        sources={
            "current_turn": "take me there",
            "conversation": "where is audit?\ntake me there",
            "assistant_context": "The audit view is in the Controls & audit workspace.",
            "application_context": "{}",
            "capability_result": "",
        },
    )
    assert assessment is not None
    assert decision["decision"] == "PASS"


def test_assistant_context_cannot_ground_transfer_beneficiary_or_amount(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    payload = transfer_assessment()
    payload["evidence"] = [
        {"field": "intent", "source": "current_turn", "quote": "Please prepare the transfer"},
        {"field": "amount", "source": "assistant_context", "quote": "You asked to send 50 EUR."},
        {"field": "beneficiary", "source": "assistant_context", "quote": "The recipient is John."},
    ]
    _, decision = validate_interpretation_assessment(
        payload,
        expected_capability_id="banking.contacts.search",
        sources={
            "current_turn": "Please prepare the transfer",
            "conversation": "Please prepare the transfer",
            "assistant_context": "You asked to send 50 EUR. The recipient is John.",
            "application_context": "{}",
            "capability_result": "",
        },
    )
    assert decision["code"] == "VALUE_EVIDENCE_MISSING"
    assert set(decision["fields"]) == {"amount", "beneficiary"}


def test_semantic_workspace_alias_must_resolve_to_a_unique_registered_page(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    payload = {
        "intent_id": "WORKSPACE_NAVIGATION",
        "intent_confidence": 0.95,
        "slot_confidence": 0.95,
        "entity_confidence": 0.95,
        "capability_id": "frontend.workspace.navigate",
        "capability_selection_confidence": 0.95,
        "resolution": "READY",
        "slots": {},
        "entities": {"workspace": "Controls & audit"},
        "evidence": [
            {"field": "intent", "source": "current_turn", "quote": "open audit"},
            {"field": "workspace", "source": "current_turn", "quote": "audit"},
        ],
    }
    _, decision = validate_interpretation_assessment(
        payload,
        expected_capability_id="frontend.workspace.navigate",
        sources={"current_turn": "open audit", "conversation": "open audit", "assistant_context": "", "application_context": "{}", "capability_result": ""},
    )
    assert decision["decision"] == "PASS"
    payload["entities"]["workspace"] = "Admin console"
    _, invalid = validate_interpretation_assessment(
        payload,
        expected_capability_id="frontend.workspace.navigate",
        sources={"current_turn": "open audit", "conversation": "open audit", "assistant_context": "", "application_context": "{}", "capability_result": ""},
    )
    assert invalid["code"] == "VALUE_EVIDENCE_MISSING"


def test_everyday_short_name_can_be_evidenced_against_account_catalogue(monkeypatch):
    import json

    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    payload = {
        "intent_id": "ACCOUNT_SELECTION",
        "intent_confidence": 0.93,
        "slot_confidence": 0.91,
        "entity_confidence": 0.97,
        "capability_id": "frontend.account.select",
        "capability_selection_confidence": 0.94,
        "resolution": "READY",
        "slots": {},
        "entities": {"account_name": "Everyday account"},
        "evidence": [
            {"field": "intent", "source": "current_turn", "quote": "select Everyday"},
            {"field": "account_name", "source": "current_turn", "quote": "Everyday"},
        ],
    }
    context = json.dumps({"accounts": [{"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR"}]})
    _, decision = validate_interpretation_assessment(
        payload,
        expected_capability_id="frontend.account.select",
        sources={"current_turn": "select Everyday", "conversation": "select Everyday", "assistant_context": "", "application_context": context, "capability_result": ""},
    )
    assert decision["decision"] == "PASS"


def test_registered_capability_catalogue_includes_frontend_but_not_runtime_owned_draft():
    from services.agent_runtime.interpretation import registered_capability_vocabulary

    vocabulary = registered_capability_vocabulary()
    assert "frontend.workspace.navigate -> navigate_workspace" in vocabulary
    assert "frontend.account.select -> select_account" in vocabulary
    assert "banking.contacts.search -> search_contacts" in vocabulary
    assert "frontend.transfer.prepare_draft" not in vocabulary
    assert "prepare_transfer_draft" not in vocabulary


def test_assessment_sources_separate_assistant_context_and_only_validated_tool_results():
    from types import SimpleNamespace
    from shared.contracts.registry import create_result_envelope
    from services.agent_runtime.interpretation import assessment_sources
    import json

    result = create_result_envelope(
        "banking.transactions.recent",
        "SUCCESS",
        data=[{
            "transaction_id": "txn-001", "account_name": "Everyday account",
            "description": "Salary · AIGENSYS", "category": "INCOME",
            "amount_minor": 1100000, "currency": "EUR", "status": "BOOKED",
            "occurred_at": "2026-11-21T22:18:00Z",
        }],
        evidence={"source": "mcp", "scope": "authenticated_customer_transactions", "completeness": "COMPLETE"},
        trace_id="trace-test",
    )
    messages = [
        SimpleNamespace(type="human", content="where is audit?", name=None),
        SimpleNamespace(type="ai", content="The audit view is in Controls & audit. I can take you there.", name=None, tool_calls=[]),
        SimpleNamespace(type="tool", content=json.dumps(result), name="get_recent_transactions"),
        SimpleNamespace(type="tool", content="not a valid evidence envelope", name="get_recent_transactions"),
    ]
    sources = assessment_sources(messages, "take me there", {"selected_account_id": None})
    assert "Controls & audit" in sources["assistant_context"]
    assert "txn-001" in sources["capability_result"]
    assert "not a valid evidence envelope" not in sources["capability_result"]
    assert "take me there" in sources["current_turn"]


def test_transaction_selector_contract_rejects_an_older_item_not_supported_by_recent_read(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    payload = {
        "intent_id": "TRANSACTION_EXPLANATION",
        "intent_confidence": 0.95,
        "slot_confidence": 0.91,
        "entity_confidence": 0.90,
        "capability_id": "banking.transactions.recent",
        "capability_selection_confidence": 0.93,
        "resolution": "READY",
        "slots": {"transaction_selector": "last_item"},
        "entities": {},
        "evidence": [
            {"field": "intent", "source": "current_turn", "quote": "the last one"},
            {"field": "transaction_selector", "source": "current_turn", "quote": "the last one"},
            {"field": "transaction_selector", "source": "capability_result", "quote": '"transaction_id":"txn-001"'},
        ],
    }
    _, decision = validate_interpretation_assessment(
        payload,
        expected_capability_id="banking.transactions.recent",
        sources={
            "current_turn": "the last one",
            "conversation": "latest transactions?\nthe last one",
            "assistant_context": "Here are your latest transactions.",
            "application_context": "{}",
            "capability_result": '{"capability_id":"banking.transactions.recent","data":[{"transaction_id":"txn-001"}]}',
        },
    )
    assert decision["decision"] == "CLARIFY"
    assert decision["code"] == "INTENT_SEMANTIC_VALUE_UNSUPPORTED"


def test_last_item_reference_without_verified_prior_result_is_not_grounded(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    payload = {
        "intent_id": "TRANSACTION_EXPLANATION",
        "intent_confidence": 0.95,
        "slot_confidence": 0.91,
        "entity_confidence": 0.90,
        "capability_id": "banking.transactions.recent",
        "capability_selection_confidence": 0.93,
        "resolution": "READY",
        "slots": {"transaction_selector": "last_item"},
        "entities": {},
        "evidence": [
            {"field": "intent", "source": "current_turn", "quote": "the last one"},
            {"field": "transaction_selector", "source": "current_turn", "quote": "the last one"},
        ],
    }
    _, decision = validate_interpretation_assessment(
        payload,
        expected_capability_id="banking.transactions.recent",
        sources={"current_turn": "the last one", "conversation": "the last one", "assistant_context": "", "application_context": "{}", "capability_result": ""},
    )
    assert decision["code"] == "INTENT_SEMANTIC_VALUE_UNSUPPORTED"


def test_assistant_context_cannot_replace_customer_intent_evidence(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    payload = {
        "intent_id": "WORKSPACE_NAVIGATION",
        "intent_confidence": 0.9,
        "slot_confidence": 0.9,
        "entity_confidence": 0.9,
        "capability_id": "frontend.workspace.navigate",
        "capability_selection_confidence": 0.9,
        "resolution": "READY",
        "slots": {},
        "entities": {"workspace": "Controls & audit"},
        "evidence": [
            {"field": "intent", "source": "assistant_context", "quote": "I can take you to Controls & audit."},
            {"field": "workspace", "source": "assistant_context", "quote": "Controls & audit"},
        ],
    }
    _, decision = validate_interpretation_assessment(
        payload,
        expected_capability_id="frontend.workspace.navigate",
        sources={"current_turn": "", "conversation": "", "assistant_context": "I can take you to Controls & audit.", "application_context": "{}", "capability_result": ""},
    )
    assert decision["code"] == "CUSTOMER_INSTRUCTION_EVIDENCE_MISSING"


def test_assistant_quote_must_exist_verbatim_in_assistant_context(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    payload = {
        "intent_id": "WORKSPACE_NAVIGATION",
        "intent_confidence": 0.96,
        "slot_confidence": 0.91,
        "entity_confidence": 0.94,
        "capability_id": "frontend.workspace.navigate",
        "capability_selection_confidence": 0.95,
        "resolution": "READY",
        "slots": {},
        "entities": {"workspace": "Controls & audit"},
        "evidence": [
            {"field": "intent", "source": "current_turn", "quote": "take me there"},
            {"field": "workspace", "source": "assistant_context", "quote": "The Risk Management workspace is open."},
        ],
    }
    assessment, decision = validate_interpretation_assessment(
        payload,
        expected_capability_id="frontend.workspace.navigate",
        sources={"current_turn": "take me there", "conversation": "take me there", "assistant_context": "The audit view is in Controls & audit.", "application_context": "{}", "capability_result": ""},
    )
    assert decision["decision"] == "PASS"
    assert decision["evidence_anchor"]["model_evidence_rejected"] == 1
    assert assessment is not None and any(
        item.field == "workspace" and item.source == "assistant_context"
        and "Controls & audit" in item.quote
        for item in assessment.evidence
    )


def test_customer_text_sanitizer_removes_internal_contract_identifiers():
    from services.agent_runtime.interpretation import sanitize_customer_text

    text = "I can help with `ACCOUNT_TOTAL_BALANCE` using `list_customer_accounts`."
    cleaned = sanitize_customer_text(text)
    assert "ACCOUNT_TOTAL_BALANCE" not in cleaned
    assert "list_customer_accounts" not in cleaned
    assert "I can help with" in cleaned


def test_presentation_vocabulary_is_complete_and_declares_verified_sources():
    from services.agent_runtime.interpretation import registered_presentation_vocabulary

    vocabulary = registered_presentation_vocabulary()
    assert "render_account_summary" in vocabulary
    assert "render_transaction_list" in vocabulary
    assert "render_transaction_detail" in vocabulary
    assert "banking.transactions.recent" in vocabulary
    assert "runtime binds" not in vocabulary  # catalog is factual; prompt supplies policy


def test_assistant_context_workspace_reference_requires_deictic_customer_turn(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    payload = {
        "intent_id": "WORKSPACE_NAVIGATION",
        "intent_confidence": 0.96, "slot_confidence": 0.91,
        "entity_confidence": 0.94,
        "capability_id": "frontend.workspace.navigate",
        "capability_selection_confidence": 0.95,
        "resolution": "READY", "slots": {},
        "entities": {"workspace": "Controls & audit"},
        "evidence": [
            {"field": "intent", "source": "current_turn", "quote": "open it"},
            {"field": "workspace", "source": "assistant_context", "quote": "Controls & audit"},
        ],
    }
    _, allowed = validate_interpretation_assessment(
        payload, expected_capability_id="frontend.workspace.navigate",
        sources={
            "current_turn": "open it", "conversation": "show me audit\nopen it",
            "assistant_context": "The audit view is in Controls & audit.",
            "application_context": "{}", "capability_result": "",
        },
    )
    assert allowed["decision"] == "PASS"

    payload["evidence"][0]["quote"] = "what is the audit workspace?"
    _, denied = validate_interpretation_assessment(
        payload, expected_capability_id="frontend.workspace.navigate",
        sources={
            "current_turn": "what is the audit workspace?",
            "conversation": "what is the audit workspace?",
            "assistant_context": "The audit view is in Controls & audit.",
            "application_context": "{}", "capability_result": "",
        },
    )
    assert denied["code"] == "VALUE_EVIDENCE_MISSING"


def test_semantic_balance_and_account_enums_require_meaningful_customer_language(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    payload = {
        "intent_id": "ACCOUNT_TOTAL_BALANCE", "intent_confidence": 0.93,
        "slot_confidence": 0.92, "entity_confidence": 0.95,
        "capability_id": "banking.accounts.list", "capability_selection_confidence": 0.94,
        "resolution": "READY", "slots": {"balance_scope": "total"}, "entities": {},
        "evidence": [
            {"field": "intent", "source": "current_turn", "quote": "what's my sum?"},
            {"field": "balance_scope", "source": "current_turn", "quote": "what's my sum?"},
        ],
    }
    _, good = validate_interpretation_assessment(
        payload, expected_capability_id="banking.accounts.list",
        sources={"current_turn": "what's my sum?", "conversation": "what's my sum?", "assistant_context": "", "application_context": "{}", "capability_result": ""},
    )
    assert good["decision"] == "PASS"

    payload["evidence"][0]["quote"] = "what's my remaining amount?"
    payload["evidence"][1]["quote"] = "what's my remaining amount?"
    _, ambiguous = validate_interpretation_assessment(
        payload, expected_capability_id="banking.accounts.list",
        sources={"current_turn": "what's my remaining amount?", "conversation": "what's my remaining amount?", "assistant_context": "", "application_context": "{}", "capability_result": ""},
    )
    assert ambiguous["code"] == "VALUE_EVIDENCE_MISSING"


def test_highest_balance_enum_cannot_be_grounded_by_unrelated_account_wording(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    payload = {
        "intent_id": "TRANSFER_PREPARATION", "intent_confidence": 0.95,
        "slot_confidence": 0.89, "entity_confidence": 0.91,
        "capability_id": "banking.accounts.list", "capability_selection_confidence": 0.94,
        "resolution": "READY", "slots": {"source_account_preference": "highest_balance"},
        "entities": {}, "evidence": [
            {"field": "intent", "source": "current_turn", "quote": "use my Everyday account"},
            {"field": "source_account_preference", "source": "current_turn", "quote": "use my Everyday account"},
        ],
    }
    _, decision = validate_interpretation_assessment(
        payload, expected_capability_id="banking.accounts.list",
        sources={"current_turn": "use my Everyday account", "conversation": "use my Everyday account", "assistant_context": "", "application_context": "{}", "capability_result": ""},
    )
    assert decision["code"] == "VALUE_EVIDENCE_MISSING"


@pytest.mark.parametrize("capability_id, expected_intent", [
    ("frontend.workspace.navigate", "WORKSPACE_NAVIGATION"),
    ("frontend.account.select", "ACCOUNT_SELECTION"),
])
def test_registry_can_infer_only_unique_intent_for_a_capability(capability_id, expected_intent):
    from services.agent_runtime.interpretation import unique_intent_for_capability
    assert unique_intent_for_capability(capability_id) == expected_intent


def test_shared_contacts_capability_does_not_infer_transfer_when_multiple_intents_own_it():
    from services.agent_runtime.interpretation import unique_intent_for_capability
    assert unique_intent_for_capability("banking.contacts.search") is None


def test_flat_tool_assessment_transport_reconstructs_and_anchors_transfer(monkeypatch):
    from services.agent_runtime.interpretation import (
        assessment_from_tool_arguments, unique_intent_for_capability,
    )
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    arguments = {
        "name_query": "John",
        "orai_intent_id": "TRANSFER_PREPARATION",
        "orai_slots": {"amount": 50, "currency": "EUR"},
        "orai_entities": {},
        # No explicit quotes: the runtime must attach exact evidence to model
        # values from the authenticated customer turn, not invent new values.
    }
    raw, transport = assessment_from_tool_arguments(
        arguments,
        expected_capability_id="banking.contacts.search",
        inferred_intent_id=unique_intent_for_capability("banking.contacts.search"),
    )
    assert transport["code"] == "FLAT_ASSESSMENT_TRANSPORT"
    raw["entities"]["beneficiary"] = arguments["name_query"]
    assessment, decision = validate_interpretation_assessment(
        raw,
        expected_capability_id="banking.contacts.search",
        sources={
            "current_turn": "send 50 EUR to John",
            "conversation": "send 50 EUR to John",
            "assistant_context": "",
            "application_context": "{}",
            "capability_result": "",
        },
    )
    assert decision["decision"] == "PASS"
    assert assessment is not None
    assert assessment.slots == {"amount": "50", "currency": "EUR"}
    assert any(item.field == "beneficiary" and item.quote for item in assessment.evidence)
    assert decision["missing_dimensions"] == ["intent", "slot", "entity", "capability"]


def test_observe_mode_does_not_fabricate_missing_confidence_but_enforce_mode_blocks(monkeypatch):
    payload = {
        "intent_id": "WORKSPACE_NAVIGATION",
        "capability_id": "frontend.workspace.navigate",
        "entities": {"workspace": "Controls & audit"},
    }
    sources = {
        "current_turn": "take me there",
        "conversation": "take me there",
        "assistant_context": "The audit view is in Controls & audit.",
        "application_context": "{}",
        "capability_result": "",
    }
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    assessment, observed = validate_interpretation_assessment(
        payload, expected_capability_id="frontend.workspace.navigate", sources=sources,
    )
    assert observed["decision"] == "PASS"
    assert observed["missing_dimensions"] == ["intent", "slot", "entity", "capability"]
    assert assessment is not None and assessment.intent_confidence is None

    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "enforce")
    for name in (
        "ORAI_INTENT_CONFIDENCE_THRESHOLD", "ORAI_SLOT_CONFIDENCE_THRESHOLD",
        "ORAI_ENTITY_CONFIDENCE_THRESHOLD", "ORAI_CAPABILITY_CONFIDENCE_THRESHOLD",
    ):
        monkeypatch.setenv(name, "0.8")
    _, enforced = validate_interpretation_assessment(
        payload, expected_capability_id="frontend.workspace.navigate", sources=sources,
    )
    assert enforced["decision"] == "CLARIFY"
    assert enforced["code"] == "CONFIDENCE_MISSING"


def test_schema_rejection_reports_safe_field_paths_without_echoing_values(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    _, decision = validate_interpretation_assessment(
        {"intent_id": "TRANSFER_PREPARATION", "capability_id": "banking.contacts.search", "intent_confidence": "not-a-number"},
        expected_capability_id="banking.contacts.search",
        sources={"current_turn": "send 50 EUR to John", "conversation": "send 50 EUR to John"},
    )
    assert decision["code"] == "ASSESSMENT_SCHEMA_INVALID"
    assert any("intent_confidence" in item for item in decision["issues"])
    assert "50" not in " ".join(decision["issues"])


def test_account_total_and_recent_transaction_variants_are_registry_fast_paths():
    from services.agent_runtime.intent_contracts import classify_canonical_read_intent
    assert classify_canonical_read_intent("what's my sum?") == "ACCOUNT_TOTAL_BALANCE"
    assert classify_canonical_read_intent("what's the total sum?") == "ACCOUNT_TOTAL_BALANCE"
    assert classify_canonical_read_intent("latest trans?") == "RECENT_TRANSACTIONS"
    assert classify_canonical_read_intent("the last transaction") == "LATEST_TRANSACTION_DETAIL"
    assert classify_canonical_read_intent("what's my remaining amount?") is None


def test_amount_decimal_formatting_and_currency_aliases_anchor_without_changing_values(monkeypatch):
    monkeypatch.setenv("ORAI_CONFIDENCE_GATE_MODE", "observe")
    payload = transfer_assessment()
    payload["slots"]["amount"] = "50.00"
    payload["slots"]["currency"] = "EUR"
    assessment, decision = validate_interpretation_assessment(
        payload,
        expected_capability_id="banking.contacts.search",
        sources={
            "current_turn": "Send 50 euros to John",
            "conversation": "Send 50 euros to John",
            "assistant_context": "",
            "application_context": "{}",
            "capability_result": "",
        },
    )
    assert decision["decision"] == "PASS"
    assert assessment is not None
    assert assessment.slots["amount"] == "50.00"
    assert assessment.slots["currency"] == "EUR"


def test_assessment_transport_fields_are_stripped_before_occf_arguments():
    from services.agent_runtime.interpretation import strip_assessment_tool_arguments

    arguments = {
        "name_query": "John",
        "orai_intent_id": "TRANSFER_PREPARATION",
        "orai_intent_confidence": 0.98,
        "orai_slots": {"amount": "50", "currency": "EUR"},
        "orai_evidence": ["amount|current_turn|50"],
    }
    assert strip_assessment_tool_arguments(arguments) == {"name_query": "John"}


def test_registry_does_not_infer_intent_when_capability_serves_multiple_intents():
    from services.agent_runtime.interpretation import unique_intent_for_capability

    assert unique_intent_for_capability("banking.accounts.list") is None


def test_flat_transport_schema_has_no_nested_assessment_or_schema_definitions():
    from services.agent_runtime.interpretation import _assessment_transport_schema

    schema = _assessment_transport_schema()
    assert "$defs" not in schema
    assert "interpretation_assessment" not in schema["properties"]
    assert set(schema["properties"]) >= {
        "orai_intent_id", "orai_intent_confidence", "orai_slot_confidence",
        "orai_entity_confidence", "orai_capability_selection_confidence",
        "orai_resolution", "orai_slots", "orai_entities", "orai_evidence",
    }
    assert schema["required"] == []


def test_customer_sanitizer_removes_internal_ids_without_leaving_backtick_artifacts():
    from services.agent_runtime.interpretation import sanitize_customer_text

    cleaned = sanitize_customer_text(
        "I can show balances via `ACCOUNT_TOTAL_BALANCE` or use `banking.accounts.list`."
    )
    assert "ACCOUNT_TOTAL_BALANCE" not in cleaned
    assert "banking.accounts.list" not in cleaned
    assert "``" not in cleaned
