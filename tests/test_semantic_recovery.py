from services.agent_runtime.decision_router import build_agent_decision_map
from services.agent_runtime.intent_contracts import (
    classify_canonical_read_intent,
    discover_intent_candidates,
    intent_discovery_context,
)
from services.agent_runtime.task_interpretation import (
    build_assessment_prompt,
    task_prompt_catalogue,
)
from shared.contracts.decision_routing import load_decision_routing_policy
from shared.contracts.registry import get_registry


def test_original_registered_evidence_preassessment_routes_remain_enabled():
    policy = load_decision_routing_policy()
    routes = {route.id: route.enabled for route in policy.pre_assessment_routes}
    assert routes == {
        "academy_personal_data": True,
        "explicit_workspace_navigation": True,
        "workspace_knowledge": True,
        "academy_general_education": True,
        "connector_result_status": True,
        "canonical_read_fast_path": True,
        "assistant_capability_guide": True,
        "simple_social": True,
    }
    assert policy.interpreter.must_receive_decision_map is True


def test_registered_evidence_account_balance_fast_path_is_preserved():
    assert classify_canonical_read_intent("Show my account balances") == "ACCOUNT_OVERVIEW"


def test_typo_tolerant_discovery_adds_only_advisory_transaction_hints():
    candidates = discover_intent_candidates("what was last transction?", limit=5)
    assert candidates
    assert candidates[0]["intent_id"] == "LATEST_TRANSACTION_DETAIL"
    assert candidates[0]["match_kind"] == "typo_tolerant_hint"
    assert candidates[0]["matched_typo_token"] == "transaction"
    assert candidates[0]["auto_resolve"] is False
    assert classify_canonical_read_intent("what was last transction?") is None


def test_unmatched_paraphrase_gets_registry_candidate_without_becoming_a_fast_path():
    account_candidates = discover_intent_candidates("what accounts do I have?", limit=5)
    assert any(item["intent_id"] == "ACCOUNT_OVERVIEW" for item in account_candidates)
    assert classify_canonical_read_intent("what accounts do I have?") is None

    loan_candidates = discover_intent_candidates("where do I have the next installment?", limit=5)
    assert any(item["intent_id"] == "LOAN_INFORMATION" for item in loan_candidates)
    assert classify_canonical_read_intent("where do I have the next installment?") is None


def test_llm_gets_authoritative_intent_semantics_without_executable_capability_ids():
    registry = get_registry()
    decision_map = build_agent_decision_map(registry)
    prompt = build_assessment_prompt(
        intent_candidates=intent_discovery_context("what was last transction?"),
        catalogue=task_prompt_catalogue(registry),
        decision_map=decision_map,
    )

    assert "AUTHORITATIVE ORAIflow DECISION MAP" in prompt
    assert "banking.transactions.recent" not in prompt
    assert '"capability_id"' not in prompt
    assert '"policy"' not in prompt
    assert '"entry_sequence"' not in prompt
    assert "registered intent semantics" in prompt
    assert "execution_owner" in prompt
    assert "Academy" in prompt
    assert "Loans" in prompt
    assert "Interpret meaning rather than requiring an exact registered sentence" in prompt
    assert "transction" not in prompt.casefold()

    latest = next(
        item
        for item in intent_discovery_context("what was last transction?")
        if item["intent_id"] == "LATEST_TRANSACTION_DETAIL"
    )
    assert "last transaction" in latest["registered_phrases"]
    assert any(
        item["workspace_id"] == "Academy" for item in decision_map["resources"]["workspaces"]
    )
    assert len(decision_map["capabilities"]) >= 6


def test_discovery_hints_keep_typo_information_but_never_add_execution_authority():
    hints = intent_discovery_context("what was last transction?")
    latest = next(item for item in hints if item["intent_id"] == "LATEST_TRANSACTION_DETAIL")
    assert latest["matched_typo_token"] == "transaction"
    assert "last transaction" in latest["registered_phrases"]
    assert "tool_name" not in latest
