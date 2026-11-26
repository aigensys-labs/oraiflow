import json
from types import SimpleNamespace

from shared.contracts.registry import create_result_envelope, get_registry
from shared.financial_health import calculate_recorded_financial_health
from shared.turn_contract import derive_turn_outcome


def _tool_message(capability_id, outcome="SUCCESS"):
    data = calculate_recorded_financial_health(
        [{"currency": "EUR", "balance_minor": 2_000_000, "status": "ACTIVE"}],
        [{"currency": "EUR", "outstanding_principal_minor": 500_000, "demo_only": True}],
    )
    payload = create_result_envelope(
        capability_id,
        outcome,
        data=data if outcome == "SUCCESS" else ({} if outcome == "EMPTY" else None),
        trace_id="trace-current",
        error=(
            {"code": "UNAVAILABLE", "retryable": True}
            if outcome not in {"SUCCESS", "EMPTY"}
            else None
        ),
    )
    tool_name = get_registry()["capabilities"][capability_id]["name"]
    return SimpleNamespace(type="tool", name=tool_name, content=json.dumps(payload))


def test_turn_outcome_uses_only_validated_current_turn_mcp_results():
    old_result = _tool_message("banking.financial_health.overview")
    current_result = _tool_message("banking.financial_health.overview")
    messages = [
        SimpleNamespace(type="human", id="turn-old", content="old request"),
        old_result,
        SimpleNamespace(type="human", id="turn-current", content="show my financial health"),
        current_result,
    ]
    outcome = derive_turn_outcome(messages, fallback_turn_id="fallback")
    assert outcome.turn_id == "turn-current"
    assert outcome.outcome == "CAPABILITY_SUCCEEDED"
    assert outcome.successful_capabilities == ("banking.financial_health.overview",)
    assert outcome.trace_ids == ("trace-current",)


def test_turn_outcome_does_not_reuse_success_from_a_previous_turn():
    messages = [
        SimpleNamespace(type="human", id="turn-old", content="show my financial health"),
        _tool_message("banking.financial_health.overview"),
        SimpleNamespace(type="human", id="turn-new", content="tell me a joke"),
    ]
    outcome = derive_turn_outcome(messages, fallback_turn_id="fallback")
    assert outcome.turn_id == "turn-new"
    assert outcome.outcome == "RESPONSE_ONLY"
    assert not outcome.successful_capabilities


def test_turn_outcome_marks_malformed_mcp_result_as_failure_not_success():
    name = get_registry()["capabilities"]["banking.financial_health.overview"]["name"]
    messages = [
        SimpleNamespace(type="human", id="turn-1", content="show my financial health"),
        SimpleNamespace(type="tool", name=name, content='{"outcome":"SUCCESS","data":{}}'),
    ]
    outcome = derive_turn_outcome(messages, fallback_turn_id="fallback")
    assert outcome.outcome == "CAPABILITY_FAILED"
    assert outcome.failed_capabilities == ("banking.financial_health.overview",)
    assert not outcome.successful_capabilities


def _frontend_tool_message(capability_id, *, outcome="SUCCESS", status=None):
    default_status = get_registry()["capabilities"][capability_id]["result_semantics"][
        "success_status"
    ]
    data = {"status": status or default_status}
    if capability_id == "frontend.workspace.navigate":
        data["workspace"] = "Academy"
    elif capability_id == "frontend.account.select":
        data["account_id"] = "acc-001"
    elif capability_id == "frontend.transfer.prepare_draft":
        data.update({"workflow_id": "workflow-12345678", "action_id": "action-12345678"})
    envelope = create_result_envelope(
        capability_id,
        outcome,
        data=data if outcome == "SUCCESS" else None,
        error={"code": "ACTION_REJECTED", "retryable": False} if outcome != "SUCCESS" else None,
        trace_id=f"trace-{capability_id}",
    )
    name = get_registry()["capabilities"][capability_id]["name"]
    return SimpleNamespace(type="tool", name=name, content=json.dumps(envelope))


def test_turn_outcome_records_real_occf_frontend_result_envelopes():
    cases = {
        "frontend.workspace.navigate": "opened",
        "frontend.account.select": "selected",
        "frontend.account.clear_selection": "cleared",
        "frontend.transfer.prepare_draft": "draft_opened",
    }
    for capability_id, expected_status in cases.items():
        message = _frontend_tool_message(capability_id)
        # The payload is the exact versioned envelope shape emitted by
        # executeFrontendCapability, not a hand-written status-only mock.
        payload = json.loads(message.content)
        assert payload["outcome"] == "SUCCESS"
        assert payload["data"]["status"] == expected_status
        outcome = derive_turn_outcome(
            [SimpleNamespace(type="human", id="turn-action", content="do the action"), message],
            fallback_turn_id="fallback",
        )
        assert outcome.outcome == "FRONTEND_ACTION_SUCCEEDED"
        assert outcome.successful_frontend_actions == (capability_id,)
        assert outcome.trace_ids == (f"trace-{capability_id}",)
        assert not outcome.failed_frontend_actions


def test_turn_outcome_records_rejected_frontend_envelope_as_failure():
    message = _frontend_tool_message("frontend.workspace.navigate", outcome="REJECTED")
    outcome = derive_turn_outcome(
        [SimpleNamespace(type="human", id="turn-action", content="open Academy"), message],
        fallback_turn_id="fallback",
    )
    assert outcome.outcome == "FRONTEND_ACTION_FAILED"
    assert outcome.failed_frontend_actions == ("frontend.workspace.navigate",)
    assert not outcome.successful_frontend_actions


def test_turn_outcome_does_not_accept_valid_envelope_with_wrong_action_status():
    message = _frontend_tool_message("frontend.workspace.navigate", status="selected")
    outcome = derive_turn_outcome(
        [SimpleNamespace(type="human", id="turn-action", content="open Academy"), message],
        fallback_turn_id="fallback",
    )
    assert outcome.outcome == "FRONTEND_ACTION_FAILED"
    assert outcome.failed_frontend_actions == ("frontend.workspace.navigate",)


def test_turn_outcome_treats_malformed_frontend_receipt_as_failure():
    name = get_registry()["capabilities"]["frontend.workspace.navigate"]["name"]
    message = SimpleNamespace(
        type="tool", name=name, content='{"status":"opened","workspace":"Academy"}'
    )
    outcome = derive_turn_outcome(
        [SimpleNamespace(type="human", id="turn-action", content="open Academy"), message],
        fallback_turn_id="fallback",
    )
    assert outcome.outcome == "FRONTEND_ACTION_FAILED"
    assert outcome.failed_frontend_actions == ("frontend.workspace.navigate",)


def test_task_outcomes_correlate_browser_presentation_receipt_by_original_call_id():
    registry = get_registry()
    mcp_name = registry["capabilities"]["banking.transactions.recent"]["name"]
    presentation_name = registry["presentations"]["render_transaction_list"]["name"]
    mcp_result = create_result_envelope(
        "banking.transactions.recent",
        "EMPTY",
        data=[],
        trace_id="trace-transactions",
    )
    task_id = "task-transactions-001"
    plan_message = SimpleNamespace(
        type="ai",
        additional_kwargs={
            "oraiflow_task_plan": {
                "schema_version": "1.0.0",
                "tasks": [
                    {
                        "task_id": task_id,
                        "intent_id": "RECENT_TRANSACTIONS",
                        "status_hint": "PLANNED",
                        "capability_ids": ["banking.transactions.recent"],
                        "tool_call_ids": ["mcp-call-1", "presentation-call-1"],
                        "presentation_id": "render_transaction_list",
                        "presentation_name": presentation_name,
                    }
                ],
            },
            "oraiflow_task_call_map": {
                "mcp-call-1": [task_id],
                "presentation-call-1": [task_id],
            },
        },
    )
    messages = [
        SimpleNamespace(type="human", id="turn-1", content="my transactions"),
        plan_message,
        SimpleNamespace(
            type="tool",
            name=mcp_name,
            tool_call_id="mcp-call-1",
            content=json.dumps(mcp_result),
            additional_kwargs={},
        ),
        # Browser ToolMessages can omit Agent-specific metadata; tool_call_id
        # remains the stable correlation key.
        SimpleNamespace(
            type="tool",
            name=presentation_name,
            tool_call_id="presentation-call-1",
            content='{"status":"rendered"}',
            additional_kwargs={},
        ),
    ]
    outcome = derive_turn_outcome(messages, fallback_turn_id="fallback")
    assert len(outcome.task_outcomes) == 1
    assert outcome.task_outcomes[0].task_id == task_id
    assert outcome.task_outcomes[0].status == "EMPTY"
    assert set(outcome.task_outcomes[0].result_references) == {"mcp-call-1", "presentation-call-1"}


def test_task_outcome_reports_presentation_only_success_by_tool_call_correlation():
    presentation_name = get_registry()["presentations"]["render_account_summary"]["name"]
    task_id = "task-account-summary-001"
    messages = [
        SimpleNamespace(type="human", id="turn-2", content="show my accounts"),
        SimpleNamespace(
            type="ai",
            additional_kwargs={
                "oraiflow_task_plan": {
                    "tasks": [
                        {
                            "task_id": task_id,
                            "intent_id": "ACCOUNT_OVERVIEW",
                            "status_hint": "PLANNED",
                            "capability_ids": [],
                            "tool_call_ids": ["render-call-1"],
                            "presentation_name": presentation_name,
                        }
                    ]
                },
                "oraiflow_task_call_map": {"render-call-1": [task_id]},
            },
        ),
        SimpleNamespace(
            type="tool",
            name=presentation_name,
            tool_call_id="render-call-1",
            content='{"status":"rendered"}',
            additional_kwargs={},
        ),
    ]
    outcome = derive_turn_outcome(messages, fallback_turn_id="fallback")
    assert outcome.task_outcomes[0].status == "SUCCEEDED"
    assert outcome.task_outcomes[0].result_references == ("render-call-1",)
