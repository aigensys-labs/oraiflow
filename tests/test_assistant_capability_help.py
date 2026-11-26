from services.agent_runtime.assistant_capabilities import (
    assistant_capability_help_for_request,
)
from services.agent_runtime.intent_contracts import discover_intent_candidates


def test_capability_help_is_registered_and_discovered_semantically():
    request = "What can you help me with?"
    candidates = discover_intent_candidates(request, limit=20)
    match = next(item for item in candidates if item["intent_id"] == "ASSISTANT_CAPABILITY_HELP")
    assert match["match_kind"] == "semantic_phrase"
    assert match["score"] >= match["minimum_score"]


def test_capability_help_uses_registered_assistant_catalogue():
    answer = assistant_capability_help_for_request("What can you help me with?")
    assert answer is not None
    assert "Accounts & portfolio" in answer
    assert "Transactions & activity" in answer
    assert "Loans & repayments" in answer
    assert "Transfer preparation" in answer


def test_capability_help_does_not_trigger_for_generic_social_greeting():
    assert assistant_capability_help_for_request("Hello") is None


def test_model_agent_uses_configured_route_order_for_capability_help_before_social_fallback():
    from shared.contracts.decision_routing import load_decision_routing_policy

    routes = load_decision_routing_policy().pre_assessment_routes
    enabled = [route for route in routes if route.enabled]
    help_route = next(route for route in enabled if route.handler == "assistant_capabilities")
    social_route = next(route for route in enabled if route.handler == "simple_social")
    assert help_route.priority > social_route.priority
    assert enabled.index(help_route) < enabled.index(social_route)


def test_capability_help_variants_resolve_from_registered_semantics():
    for request in (
        "How can you help?",
        "What do you help with?",
        "What services can you provide?",
    ):
        answer = assistant_capability_help_for_request(request)
        assert answer is not None, request
        assert "Here are the main ways I can help" in answer


def test_model_agent_has_no_duplicate_phrase_regex_for_capability_help():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "services/agent_runtime/agent.py").read_text()
    assert "normalised_request" not in source
    assert "CAPABILITY_HELP_MESSAGE" not in source
