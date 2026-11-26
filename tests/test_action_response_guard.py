from types import SimpleNamespace

from services.agent_runtime.action_response_guard import has_successful_frontend_ack_after_latest_user
from shared.pending_task import PendingTaskContext, attach_pending_task


def _human(text):
    return SimpleNamespace(type="human", content=text, additional_kwargs={})


def _assistant(text="", *, receipt=None, workflow=None):
    metadata = {}
    if receipt is not None:
        metadata["oraiflow_frontend_action_receipt"] = receipt
    if workflow is not None:
        metadata["oraiflow_transfer_workflow"] = workflow
    return SimpleNamespace(type="ai", content=text, additional_kwargs=metadata)


def test_successful_transfer_draft_ack_suppresses_same_turn_generic_response_replay():
    messages = [
        _human("everyday"),
        _assistant(
            "Your editable transfer draft is open. Nothing has been previewed, approved or submitted.",
            receipt={
                "capability_id": "frontend.transfer.prepare_draft",
                "workflow_id": "workflow-12345678",
                "action_id": "action-12345678",
                "status": "draft_opened",
            },
            workflow={"stage": "DRAFT_OPENED", "last_action_status": "draft_opened"},
        ),
    ]
    assert has_successful_frontend_ack_after_latest_user(messages) is True


def test_successful_navigation_ack_suppresses_same_turn_response_replay():
    messages = [
        _human("show me my contacts"),
        _assistant(
            "I've opened the Contacts workspace for you.",
            receipt={
                "capability_id": "frontend.workspace.navigate",
                "tool_call_id": "nav-1234567890abcdef",
                "status": "opened",
            },
        ),
    ]
    assert has_successful_frontend_ack_after_latest_user(messages) is True


def test_new_customer_turn_is_not_blocked_by_an_earlier_action_acknowledgement():
    messages = [
        _human("everyday"),
        _assistant(
            "Your editable transfer draft is open.",
            receipt={"capability_id": "frontend.transfer.prepare_draft", "status": "draft_opened"},
        ),
        _human("what is my savings balance?"),
    ]
    assert has_successful_frontend_ack_after_latest_user(messages) is False


def test_rejected_or_unmatched_receipt_does_not_suppress_customer_response():
    for status in ("rejected", "UNMATCHED_RECEIPT", "INVALID_RECEIPT"):
        messages = [
            _human("everyday"),
            _assistant("The action did not complete.", receipt={
                "capability_id": "frontend.transfer.prepare_draft", "status": status,
            }),
        ]
        assert has_successful_frontend_ack_after_latest_user(messages) is False


def test_draft_opened_workflow_marker_is_a_fallback_when_receipt_metadata_was_rehydrated_away():
    messages = [
        _human("everyday"),
        _assistant("Your editable transfer draft is open.", workflow={
            "stage": "DRAFT_OPENED", "last_action_status": "draft_opened",
        }),
    ]
    assert has_successful_frontend_ack_after_latest_user(messages) is True



def test_standard_ack_name_and_id_survive_serialized_message_metadata_loss():
    # AG-UI/CopilotKit serializers may not retain arbitrary additional_kwargs.
    # Standard message identity must still prevent a second response from
    # overwriting the verified action acknowledgement.
    messages = [
        {"role": "user", "content": "everyday", "id": "turn-1"},
        {
            "role": "assistant",
            "name": "oraiflow_action_ack",
            "id": "oraiflow-action-ack:action-12345678",
            "content": "Your editable transfer draft is open.",
        },
        {"role": "assistant", "content": "I’m here to help with ORAIflow banking and workspace tasks."},
    ]
    assert has_successful_frontend_ack_after_latest_user(messages) is True


def test_action_ack_from_prior_turn_does_not_suppress_a_new_turn():
    messages = [
        {"role": "user", "content": "everyday", "id": "turn-1"},
        {"role": "assistant", "name": "oraiflow_action_ack", "id": "oraiflow-action-ack:act-1", "content": "Draft open."},
        {"role": "user", "content": "what is my savings balance?", "id": "turn-2"},
    ]
    assert has_successful_frontend_ack_after_latest_user(messages) is False


def test_success_receipt_is_case_insensitive_and_supports_serialized_metadata():
    messages = [
        {"role": "user", "content": "show contacts"},
        {"role": "assistant", "metadata": {"oraiflow_frontend_action_receipt": {
            "capability_id": "frontend.workspace.navigate", "status": "OPENED",
        }}, "content": "I've opened Contacts."},
    ]
    assert has_successful_frontend_ack_after_latest_user(messages) is True


def test_typed_transfer_task_is_the_authoritative_draft_opened_marker():
    assistant = _assistant("Your editable transfer draft is open.", receipt=None)
    attach_pending_task(assistant, PendingTaskContext(
        task_id="trf-typed-1", domain="transfer", intent_id="TRANSFER_PREPARATION",
        stage="DRAFT_OPENED", status="COMPLETED",
        state={"workflow": {"stage": "DRAFT_OPENED", "last_action_status": "draft_opened"}},
    ))
    assert has_successful_frontend_ack_after_latest_user([_human("everyday"), assistant]) is True


def test_typed_navigation_owner_blocks_stale_legacy_transfer_marker():
    assistant = _assistant("I've opened Contacts.", workflow={
        "stage": "DRAFT_OPENED", "last_action_status": "draft_opened",
    })
    attach_pending_task(assistant, PendingTaskContext(
        task_id="nav-typed-1", domain="navigation", intent_id="WORKSPACE_NAVIGATION",
        stage="NAVIGATION_REQUESTED", status="COMPLETED", state={"workspace": "Contacts"},
    ))
    assert has_successful_frontend_ack_after_latest_user([_human("Contacts"), assistant]) is False


def test_serialized_agui_metadata_recognizes_successful_transfer_draft_ack():
    from services.agent_runtime.action_response_guard import has_successful_frontend_ack_after_latest_user
    from shared.pending_task import PendingTaskContext

    completed = PendingTaskContext(
        task_id="transfer-serialized-ack",
        domain="transfer",
        intent_id="TRANSFER_PREPARATION",
        stage="DRAFT_OPENED",
        status="COMPLETED",
        state={
            "workflow": {
                "stage": "DRAFT_OPENED",
                "last_action_status": "draft_opened",
            },
        },
    ).model_dump(mode="json")
    messages = [
        {"role": "user", "content": "latter"},
        {
            "role": "assistant",
            "content": "The editable transfer draft is open.",
            "metadata": {
                "oraiflow_frontend_action_receipt": {
                    "capability_id": "frontend.transfer.prepare_draft",
                    "status": "draft_opened",
                },
                "oraiflow_pending_task": completed,
            },
        },
    ]
    assert has_successful_frontend_ack_after_latest_user(messages) is True


def test_verified_draft_tool_result_is_not_mistaken_for_final_acknowledgement():
    import json

    from shared.contracts.registry import create_result_envelope

    workflow_id = "workflow-serialized-1234"
    action_id = "action-serialized-1234"
    call = {
        "name": "prepare_transfer_draft",
        "args": {
            "beneficiary": "Lucas",
            "amount": "50",
            "currency": "EUR",
            "from_account_id": "acc-001",
            "beneficiary_contact_id": "contact-lucas",
            "workflow_id": workflow_id,
            "action_id": action_id,
        },
        "id": action_id,
        "type": "tool_call",
    }
    draft_result = create_result_envelope(
        "frontend.transfer.prepare_draft",
        "SUCCESS",
        data={
            "status": "draft_opened",
            "workflow_id": workflow_id,
            "action_id": action_id,
        },
    )
    messages = [
        {"role": "user", "content": "send 50 EUR to Lucas from everyday", "id": "turn-transfer-1"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {
            "role": "tool",
            "name": "prepare_transfer_draft",
            "tool_call_id": action_id,
            "content": json.dumps(draft_result),
        },
    ]
    # The receipt is valid, but the graph must still process it and emit its
    # final acknowledgement before the replay guard can terminate the turn.
    assert has_successful_frontend_ack_after_latest_user(messages) is False

    messages.append({
        "role": "assistant",
        "name": "oraiflow_action_ack",
        "id": f"oraiflow-action-ack:{action_id}",
        "content": "Your editable transfer draft is open. Nothing has been submitted.",
        "metadata": {
            "oraiflow_frontend_action_receipt": {
                "capability_id": "frontend.transfer.prepare_draft",
                "workflow_id": workflow_id,
                "action_id": action_id,
                "status": "draft_opened",
            },
        },
    })
    # Only now is the turn terminal; a subsequent unrelated ToolMessage cannot
    # cause a second interpretation or overwrite the acknowledgement.
    messages.append({
        "role": "tool", "name": "search_contacts", "tool_call_id": "late-contact-search",
        "content": "{\"data\":[{\"full_name\":\"Lucas\"}]}",
    })
    assert has_successful_frontend_ack_after_latest_user(messages) is True


def test_unmatched_serialized_draft_tool_result_does_not_stop_turn():
    import json

    from shared.contracts.registry import create_result_envelope

    rejected = create_result_envelope(
        "frontend.transfer.prepare_draft",
        "SUCCESS",
        data={
            "status": "draft_opened",
            "workflow_id": "workflow-different-1234",
            "action_id": "action-different-1234",
        },
    )
    messages = [
        {"role": "user", "content": "latter"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "name": "prepare_transfer_draft",
                "args": {
                    "beneficiary": "John Mitchell",
                    "amount": "50",
                    "currency": "EUR",
                    "from_account_id": "acc-001",
                    "beneficiary_contact_id": "contact-john-mitchell",
                    "workflow_id": "workflow-expected-1234",
                    "action_id": "action-expected-1234",
                },
                "id": "action-expected-1234",
                "type": "tool_call",
            }],
        },
        {
            "role": "tool",
            "name": "prepare_transfer_draft",
            "tool_call_id": "action-expected-1234",
            "content": json.dumps(rejected),
        },
    ]
    assert has_successful_frontend_ack_after_latest_user(messages) is False
