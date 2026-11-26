from types import SimpleNamespace

from services.agent_runtime.academy import (
    academy_direct_guidance_for_request,
    academy_personal_snapshot_intent,
    render_financial_health_explanation,
)
from services.agent_runtime.intent_contracts import discover_intent_candidates
from services.agent_runtime.task_interpretation import (
    AssessmentToolArguments,
    InterpretedTask,
    TaskEvidence,
    TurnInterpretationAssessment,
    academy_prompt_catalogue,
    bind_structured_assessment_model,
    build_assessment_prompt,
    parse_assessment_payload,
    safe_canonical_read_assessment,
    task_prompt_catalogue,
    workspace_prompt_catalogue,
)
from services.agent_runtime.task_planner import plan_interpreted_task
from services.agent_runtime.workspace_knowledge import workspace_help_for_request
from shared.contracts.registry import get_registry


FINANCIAL_HEALTH_QUESTION = (
    "Explain my Financial Health badge. What does my recorded loan-to-equity ratio mean, "
    "and what could improve or worsen it?"
)
BORROWING_QUESTION = (
    "Explain borrowing and leverage in plain language. How does taking another loan "
    "affect my recorded Financial Health badge?"
)
CREDIT_COMPARISON_QUESTION = (
    "How is a credit score different from my ORAIflow Financial Health badge, "
    "and what habits can help maintain a healthy credit history?"
)
CASH_FLOW_QUESTION = (
    "Help me understand my monthly cash flow and how to leave enough room for bills, "
    "savings and loan instalments."
)


def test_interpreter_catalogue_contains_registry_phrases_and_transfer_slots():
    catalogue = task_prompt_catalogue()
    transfer = next(item for item in catalogue if item["intent_id"] == "TRANSFER_PREPARATION")
    assert "send money to" in transfer["examples"]
    assert "transfer money to" in transfer["examples"]
    assert "send" in transfer["keywords"]
    assert transfer["required_slots"] == ["amount"]
    assert transfer["required_entities"] == ["beneficiary"]


def test_assessment_prompt_includes_transfer_knowledge_and_registered_academy_topics():
    registry = get_registry()
    prompt = build_assessment_prompt(
        intent_candidates=discover_intent_candidates("send 50 EUR to John"),
        catalogue=task_prompt_catalogue(registry),
        workspace_catalogue=workspace_prompt_catalogue(registry, query="what is Academy?"),
        academy_catalogue=academy_prompt_catalogue(registry, query=CREDIT_COMPARISON_QUESTION),
    )
    assert "TRANSFER_PREPARATION" in prompt
    assert "send money to" in prompt
    assert "beneficiary" in prompt
    assert "Academy" in prompt
    assert "CREDIT_SCORE_BASICS" in prompt
    assert "FINANCIAL_HEALTH_BADGE" in prompt
    assert "Never call or select MCP tools" in prompt


def test_academy_prompt_catalogue_does_not_match_transfer_stopwords():
    assert academy_prompt_catalogue(query="send 50 EUR to John") == []


def test_academy_prompt_catalogue_includes_both_sides_of_credit_score_comparison():
    topic_ids = {item["topic_id"] for item in academy_prompt_catalogue(query=CREDIT_COMPARISON_QUESTION)}
    assert "CREDIT_SCORE_BASICS" in topic_ids
    assert "FINANCIAL_HEALTH_BADGE" in topic_ids


def test_workspace_prompt_catalogue_does_not_treat_credit_or_loan_to_equity_as_workspace_request():
    assert workspace_prompt_catalogue(query=CREDIT_COMPARISON_QUESTION) == []
    assert workspace_prompt_catalogue(query=FINANCIAL_HEALTH_QUESTION) == []
    assert [item["workspace_id"] for item in workspace_prompt_catalogue(query="what is Academy?")] == ["Academy"]


def test_workspace_questions_resolve_from_registered_catalogue():
    message = workspace_help_for_request("what is Academy?")
    assert message is not None
    assert "guided financial-literacy workspace" in message
    assert "learning paths" in message
    assert workspace_help_for_request("what is Academy about?") is not None


def test_personal_snapshot_requires_topic_local_ownership_reference():
    assert academy_personal_snapshot_intent(FINANCIAL_HEALTH_QUESTION) == "FINANCIAL_HEALTH_EXPLANATION"
    assert academy_personal_snapshot_intent(BORROWING_QUESTION) == "FINANCIAL_HEALTH_EXPLANATION"
    assert academy_personal_snapshot_intent(CREDIT_COMPARISON_QUESTION) == "FINANCIAL_HEALTH_EXPLANATION"
    assert academy_personal_snapshot_intent(CASH_FLOW_QUESTION) is None


def test_cash_flow_question_uses_maintained_education_without_false_financial_health_read():
    answer = academy_direct_guidance_for_request(CASH_FLOW_QUESTION)
    assert answer is not None
    assert "Budgeting and cash flow" in answer
    assert academy_personal_snapshot_intent(CASH_FLOW_QUESTION) is None


def test_financial_health_renderer_preserves_general_credit_education_and_personal_snapshot():
    data = {
        "positions": [{
            "currency": "EUR",
            "recorded_equity_minor": 2351830,
            "recorded_deposit_assets_minor": 2351830,
            "total_recorded_liabilities_minor": 500000,
            "loan_liabilities_minor": 500000,
            "loan_to_equity_bps": 2126,
            "debt_to_assets_bps": 2126,
            "status": "LOW_LEVERAGE",
        }]
    }
    answer = render_financial_health_explanation(data, CREDIT_COMPARISON_QUESTION)
    assert "Credit scores and credit reports" in answer
    assert "Financial Health badge" in answer
    assert "Loans / recorded equity" in answer
    assert "EUR 23,518.30" in answer
    assert "not a bureau credit score" in answer


def test_provider_native_structured_adapter_is_preferred_when_available():
    marker = object()

    class FakeModel:
        def with_structured_output(self, schema):
            assert schema is AssessmentToolArguments
            return marker

    adapted, mode = bind_structured_assessment_model(FakeModel())
    assert adapted is marker
    assert mode == "native_structured_output"


def test_provider_without_native_structured_adapter_uses_explicit_compatibility_failure():
    with __import__("pytest").raises(NotImplementedError):
        bind_structured_assessment_model(object())


def test_direct_assessment_payload_is_strict_and_legacy_nested_payload_is_readable():
    payload = {
        "schema_version": "1.0.0",
        "resolution": "RESOLVED",
        "tasks": [{
            "intent_id": "TRANSFER_PREPARATION",
            "requested_outcome": "ACTION",
            "resolution": "READY",
            "slots": {"amount": 50, "currency": "EUR"},
            "entities": {"beneficiary": "John"},
            "evidence": [{"field": "intent", "source": "current_turn", "quote": "send 50 EUR to John"}],
            "intent_confidence": 0.99,
            "slot_confidence": 0.98,
            "entity_confidence": 0.98,
        }],
    }
    parsed, info = parse_assessment_payload(payload)
    assert parsed is not None
    assert info["code"] == "ASSESSMENT_VALID"
    assert parsed.tasks[0].slots == {"amount": "50", "currency": "EUR"}

    invalid = dict(payload)
    invalid["capability_id"] = "banking.contacts.search"
    parsed, info = parse_assessment_payload(invalid)
    assert parsed is None
    assert info["code"] == "ASSESSMENT_SCHEMA_INVALID"


def test_safe_read_fallback_only_accepts_unambiguous_registered_read():
    question = "Explain my Financial Health badge."
    candidates = discover_intent_candidates(question)
    assessment = safe_canonical_read_assessment(question, candidates)
    assert assessment is not None
    assert [task.intent_id for task in assessment.tasks] == ["FINANCIAL_HEALTH_EXPLANATION"]

    transfer_candidates = discover_intent_candidates("send 50 EUR to John")
    assert safe_canonical_read_assessment("send 50 EUR to John", transfer_candidates) is None


def test_transfer_interpretation_maps_to_contact_search_without_executing_a_payment():
    registry = get_registry()
    text = "send 50 EUR to John"
    task = InterpretedTask(
        intent_id="TRANSFER_PREPARATION",
        requested_outcome="ACTION",
        resolution="READY",
        slots={"amount": "50", "currency": "EUR"},
        entities={"beneficiary": "John"},
        evidence=[
            TaskEvidence(field="intent", source="current_turn", quote=text),
            TaskEvidence(field="amount", source="current_turn", quote="50"),
            TaskEvidence(field="currency", source="current_turn", quote="EUR"),
            TaskEvidence(field="beneficiary", source="current_turn", quote="John"),
        ],
        intent_confidence=0.99,
        slot_confidence=0.98,
        entity_confidence=0.98,
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": text},
        messages=[SimpleNamespace(type="human", id="turn-1", content=text)],
        context={},
        registry=registry,
    )
    assert planned["status"] == "PLANNED"
    assert planned["capability_id"] == "banking.contacts.search"
    assert planned["tool_name"] == "search_contacts"
    assert planned["arguments"] == {"name_query": "John"}
    assert planned["execution_plan"]["response_mode"] == "editable_transfer_draft"
    assert planned["capability_id"] != "frontend.transfer.prepare_draft"


def test_transfer_without_currency_preserves_amount_and_beneficiary_for_currency_clarification():
    text = "send 50 to John"
    task = InterpretedTask(
        intent_id="TRANSFER_PREPARATION",
        requested_outcome="ACTION",
        resolution="READY",
        slots={"amount": "50"},
        entities={"beneficiary": "John"},
        evidence=[
            TaskEvidence(field="intent", source="current_turn", quote=text),
            TaskEvidence(field="amount", source="current_turn", quote="50"),
            TaskEvidence(field="beneficiary", source="current_turn", quote="John"),
        ],
        intent_confidence=0.99,
        slot_confidence=0.98,
        entity_confidence=0.98,
    )
    planned = plan_interpreted_task(
        task,
        sources={"current_turn": text},
        messages=[SimpleNamespace(type="human", id="turn-2", content=text)],
        context={},
    )
    assert planned["status"] == "PLANNED"
    assert planned["arguments"] == {"name_query": "John"}
    assert "currency" not in planned["arguments"]


def test_existing_transfer_parser_can_retain_explicit_amount_and_recipient_without_guessing_currency():
    from shared.contact_resolution import extract_contact_transfer_request

    parsed = extract_contact_transfer_request(
        "send 50 to John", {"accounts": []}, allow_currency_missing=True,
    )
    assert parsed is not None
    assert parsed["amount"] == "50"
    assert parsed["beneficiary_query"] == "John"
    assert parsed["currency"] == ""
    # The long-standing default remains strict for callers that require currency.
    assert extract_contact_transfer_request("send 50 to John", {"accounts": []}) is None


def test_explicit_transfer_recovery_is_bounded_and_still_requires_registered_planner_validation():
    from services.agent_runtime.task_interpretation import safe_explicit_transfer_assessment

    for text, expected_currency in (("send 50 EUR to John", "EUR"), ("send 50 to John", "")):
        assessment = safe_explicit_transfer_assessment(text, {"accounts": []})
        assert assessment is not None
        task = assessment.tasks[0]
        assert task.intent_id == "TRANSFER_PREPARATION"
        assert task.slots["amount"] == "50"
        assert task.slots.get("currency", "") == expected_currency
        assert task.entities["beneficiary"] == "John"
        planned = plan_interpreted_task(
            task,
            sources={"current_turn": text},
            messages=[SimpleNamespace(type="human", id=f"turn-{expected_currency or 'none'}", content=text)],
            context={"accounts": []},
        )
        assert planned["status"] == "PLANNED"
        assert planned["capability_id"] == "banking.contacts.search"
        assert planned["arguments"] == {"name_query": "John"}

    assert safe_explicit_transfer_assessment("send money later", {"accounts": []}) is None
    assert safe_explicit_transfer_assessment("send 50 to my savings account", {"accounts": []}) is None


def test_assessment_prompt_explains_confidence_applicability_and_helpful_broad_checkin():
    prompt = build_assessment_prompt(
        intent_candidates=[], catalogue=task_prompt_catalogue(),
    )
    assert "If you extract any slots, provide slot_confidence" in prompt
    assert "broad check-in" in prompt
    assert "overall recorded" in prompt
