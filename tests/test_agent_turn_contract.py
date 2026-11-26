from types import SimpleNamespace

import pytest

from shared.agent_turn_contract import (
    AgentIntentCandidate,
    AgentTurnContext,
    AgentTurnResponse,
    attach_agent_turn_response,
)


def test_agent_turn_context_is_versioned_and_rejects_unregistered_shape_fields():
    context = AgentTurnContext(
        registry_version="1.4.4",
        turn_id="turn-example-001",
        workspace_id="Activity",
        available_mcp_capability_ids=("banking.transactions.recent",),
        available_frontend_capability_ids=("frontend.workspace.navigate",),
        available_presentation_ids=("PRESENTATION_RECENT_TRANSACTIONS",),
        intent_candidates=(
            AgentIntentCandidate(
                intent_id="RECENT_TRANSACTIONS",
                description="Retrieve recent activity.",
                capability_id="banking.transactions.recent",
                capability_name="get_recent_transactions",
                response_mode="presentation",
                match_kind="semantic_phrase",
                score=1108,
            ),
        ),
    )
    assert context.schema_version == "1.1.0"
    assert context.intent_candidates[0].intent_id == "RECENT_TRANSACTIONS"
    with pytest.raises(Exception):
        AgentTurnContext(
            registry_version="1.4.4",
            turn_id="turn-example-002",
            unexpected_customer_data="must not be accepted",
        )


def test_agent_turn_response_receipt_preserves_message_identity_content_and_metadata():
    message = SimpleNamespace(
        id="assistant-message-123",
        content="Here are your six recent transactions.",
        tool_calls=[],
        additional_kwargs={"provider_metadata": {"finish_reason": "stop"}},
    )
    outcome = {
        "schema_version": "1.1.0",
        "turn_id": "turn-example-001",
        "trace_ids": ["trace-001"],
        "outcome": "CAPABILITY_SUCCEEDED",
        "successful_capabilities": ["banking.transactions.recent"],
        "partial_or_empty_capabilities": [],
        "failed_capabilities": [],
        "successful_frontend_actions": [],
        "failed_frontend_actions": [],
    }
    response = attach_agent_turn_response(message, turn_id="turn-example-001", outcome=outcome)
    receipt = AgentTurnResponse.model_validate(
        response.additional_kwargs["oraiflow_agent_turn_response"]
    )
    assert response.id == "assistant-message-123"
    assert response.content == message.content
    assert response.additional_kwargs["provider_metadata"] == {"finish_reason": "stop"}
    assert receipt.response_kind == "TEXT"
    assert receipt.finalized is True
    assert receipt.outcome["successful_capabilities"] == ["banking.transactions.recent"]
    assert receipt.outcome["schema_version"] == "1.2.0"
    assert receipt.outcome["task_outcomes"] == []


def test_agent_turn_response_marks_registered_tool_proposals_as_not_finalized():
    message = SimpleNamespace(
        id="assistant-tool-request-1",
        content="",
        tool_calls=[{"name": "get_recent_transactions", "id": "call-1"}],
        additional_kwargs={},
    )
    response = attach_agent_turn_response(
        message,
        turn_id="turn-example-003",
        outcome={
            "schema_version": "1.1.0",
            "turn_id": "turn-example-003",
            "trace_ids": [],
            "outcome": "RESPONSE_ONLY",
            "successful_capabilities": [],
            "partial_or_empty_capabilities": [],
            "failed_capabilities": [],
            "successful_frontend_actions": [],
            "failed_frontend_actions": [],
        },
    )
    receipt = AgentTurnResponse.model_validate(
        response.additional_kwargs["oraiflow_agent_turn_response"]
    )
    assert receipt.response_kind == "CAPABILITY_REQUEST"
    assert receipt.finalized is False


def test_response_finalization_reuses_streamed_message_id_and_clears_private_tool_calls():
    from copy import deepcopy

    class Message:
        def __init__(self):
            self.id = "streamed-assistant-777"
            self.content = "Here are your six recent transactions."
            self.tool_calls = [{"name": "customer_response_policy", "id": "policy-1"}]
            self.invalid_tool_calls = []
            self.tool_call_chunks = []
            self.additional_kwargs = {"provider_metadata": {"finish_reason": "tool_calls"}}

        def model_copy(self, update):
            clone = deepcopy(self)
            for key, value in update.items():
                setattr(clone, key, value)
            return clone

    from shared.agent_turn_contract import replace_assistant_message_content

    original = Message()
    finalized = replace_assistant_message_content(
        original,
        content="Here are your six recent transactions: salary and five purchases.",
        additional_kwargs={"oraiflow_turn_outcome": {"outcome": "CAPABILITY_SUCCEEDED"}},
    )

    assert finalized.id == original.id
    assert finalized.content.startswith("Here are your six recent transactions:")
    assert finalized.tool_calls == []
    assert finalized.invalid_tool_calls == []
    assert finalized.tool_call_chunks == []
    assert finalized.additional_kwargs["provider_metadata"] == {"finish_reason": "tool_calls"}
    assert finalized.additional_kwargs["oraiflow_turn_outcome"]["outcome"] == "CAPABILITY_SUCCEEDED"
    assert original.content == "Here are your six recent transactions."
    assert original.tool_calls


def test_turn_context_only_advertises_registered_tools_exposed_in_this_ui_run():
    from shared.agent_turn_contract import build_agent_turn_context
    from shared.contracts.registry import get_registry

    registry = get_registry()
    context = build_agent_turn_context(
        registry,
        turn_id="turn-ui-resource-test",
        workspace_id="AI assistant",
        available_action_names={"navigate_workspace", "render_transaction_list"},
        intent_candidates=(),
    )

    assert context.available_mcp_capability_ids
    assert context.available_frontend_capability_ids == ("frontend.workspace.navigate",)
    assert context.available_presentation_ids == ("render_transaction_list",)
    assert "frontend.transfer.prepare_draft" not in context.available_frontend_capability_ids


def test_ui_tool_filter_uses_registered_effects_and_current_browser_offer():
    from copy import deepcopy

    from shared.agent_turn_contract import filter_available_ui_actions
    from shared.contracts.registry import get_registry

    registry = deepcopy(get_registry())
    registry["capabilities"]["frontend.test.runtime_draft"] = {
        "name": "prepare_test_draft",
        "kind": "frontend",
        "authorization": {"agent_invocable": True},
        "execution": {"effect": "draft_only", "model_invocable": False},
    }
    registry["capabilities"]["frontend.test.payment_side_effect"] = {
        "name": "submit_test_payment",
        "kind": "frontend",
        "authorization": {"agent_invocable": True},
        "execution": {"effect": "payment_execution", "model_invocable": False},
    }
    actions = [
        {"name": "navigate_workspace", "parameters": {"type": "object"}},
        {"name": "prepare_transfer_draft", "parameters": {"type": "object"}},
        {"name": "prepare_test_draft", "parameters": {"type": "object"}},
        {"name": "submit_test_payment", "parameters": {"type": "object"}},
        {"name": "render_transaction_list", "parameters": {"type": "object"}},
        {"name": "unregistered_browser_action", "parameters": {"type": "object"}},
    ]
    selected = filter_available_ui_actions(
        registry,
        actions,
        available_action_names={item["name"] for item in actions},
    )
    assert [item["name"] for item in selected] == [
        "navigate_workspace",
        "render_transaction_list",
    ]


def test_agent_turn_response_classifies_ui_actions_from_registry_not_name_denylist():
    message = SimpleNamespace(
        id="assistant-nav-proposal",
        content="",
        tool_calls=[{"name": "navigate_workspace", "id": "call-nav"}],
        additional_kwargs={},
    )
    response = attach_agent_turn_response(
        message,
        turn_id="turn-nav-proposal",
        outcome={
            "schema_version": "1.1.0",
            "turn_id": "turn-nav-proposal",
            "trace_ids": [],
            "outcome": "RESPONSE_ONLY",
            "successful_capabilities": [],
            "partial_or_empty_capabilities": [],
            "failed_capabilities": [],
            "successful_frontend_actions": [],
            "failed_frontend_actions": [],
        },
    )
    receipt = AgentTurnResponse.model_validate(
        response.additional_kwargs["oraiflow_agent_turn_response"]
    )
    assert receipt.response_kind == "FRONTEND_ACTION_REQUEST"
    assert receipt.finalized is False


def test_agent_turn_response_assigns_unique_message_identity_when_runtime_message_has_none():
    first = SimpleNamespace(id=None, content="One", tool_calls=[], additional_kwargs={})
    second = SimpleNamespace(id=None, content="Two", tool_calls=[], additional_kwargs={})
    outcome = {
        "schema_version": "1.1.0",
        "turn_id": "same-human-turn",
        "trace_ids": [],
        "outcome": "RESPONSE_ONLY",
        "successful_capabilities": [],
        "partial_or_empty_capabilities": [],
        "failed_capabilities": [],
        "successful_frontend_actions": [],
        "failed_frontend_actions": [],
    }
    first_response = attach_agent_turn_response(first, turn_id="same-human-turn", outcome=outcome)
    second_response = attach_agent_turn_response(second, turn_id="same-human-turn", outcome=outcome)
    first_receipt = AgentTurnResponse.model_validate(
        first_response.additional_kwargs["oraiflow_agent_turn_response"]
    )
    second_receipt = AgentTurnResponse.model_validate(
        second_response.additional_kwargs["oraiflow_agent_turn_response"]
    )
    assert first_receipt.message_id == first_response.id
    assert second_receipt.message_id == second_response.id
    assert first_response.id != second_response.id
    assert first_response.id != "same-human-turn"


def test_turn_outcome_accepts_serialized_agui_user_message_identity():
    from shared.turn_contract import derive_turn_outcome

    result = derive_turn_outcome(
        [{"role": "user", "id": "agui-turn-serialized", "content": "latter"}],
        fallback_turn_id="fallback-turn",
    )
    assert result.turn_id == "agui-turn-serialized"
    assert result.outcome == "RESPONSE_ONLY"
