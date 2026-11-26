import json
from types import SimpleNamespace

from services.agent_runtime.frontend_receipts import transfer_draft_receipt_message
from shared.contracts.registry import create_result_envelope

WORKFLOW_ID = "workflow-12345678"
ACTION_ID = "action-12345678"
ACTION_ARGS = {
    "beneficiary": "John Carter",
    "amount": "1",
    "currency": "EUR",
    "from_account_id": "acc-001",
    "beneficiary_contact_id": "contact-12345678",
    "workflow_id": WORKFLOW_ID,
    "action_id": ACTION_ID,
}


def _messages(
    *, receipt_action_id=ACTION_ID, outcome="SUCCESS", status="draft_opened", tool_call_id=ACTION_ID
):
    call = {
        "name": "prepare_transfer_draft",
        "args": dict(ACTION_ARGS),
        "id": ACTION_ID,
        "type": "tool_call",
    }
    assistant = SimpleNamespace(type="ai", content="", tool_calls=[call])
    data = {"status": status, "workflow_id": WORKFLOW_ID, "action_id": receipt_action_id}
    envelope = create_result_envelope(
        "frontend.transfer.prepare_draft",
        outcome,
        data=data if outcome == "SUCCESS" else None,
        error=(
            {"code": "ACTION_REJECTED", "message": "Draft could not be opened"}
            if outcome != "SUCCESS"
            else None
        ),
    )
    tool = SimpleNamespace(
        type="tool",
        name="prepare_transfer_draft",
        tool_call_id=tool_call_id,
        content=json.dumps(envelope),
    )
    return [assistant, tool]


def test_successful_browser_receipt_produces_precise_editable_draft_acknowledgement():
    result = transfer_draft_receipt_message(
        _messages(),
        source_accounts=[{"account_id": "acc-001", "account_name": "Everyday account"}],
    )
    assert result is not None
    message, receipt = result
    assert (
        "editable transfer draft for 1 EUR to John Carter from Everyday account is open" in message
    )
    assert "Nothing has been previewed, approved or submitted" in message
    assert receipt["status"] == "draft_opened"
    assert receipt["workflow_id"] == WORKFLOW_ID
    assert receipt["action_id"] == ACTION_ID


def test_receipt_with_mismatched_action_id_never_claims_draft_opened():
    result = transfer_draft_receipt_message(_messages(receipt_action_id="action-other-123456"))
    assert result is not None
    message, receipt = result
    assert "couldn't match the browser confirmation" in message
    assert receipt["status"] == "MISMATCHED_RECEIPT"
    assert "is open" not in message


def test_receipt_with_mismatched_tool_call_id_never_claims_draft_opened():
    result = transfer_draft_receipt_message(_messages(tool_call_id="action-unrelated-123456"))
    assert result is not None
    message, receipt = result
    assert "couldn't match it to the action requested" in message
    assert receipt["status"] == "UNMATCHED_RECEIPT"
    assert "is open" not in message


def test_rejected_browser_action_never_claims_draft_opened():
    result = transfer_draft_receipt_message(_messages(outcome="REJECTED", status="rejected"))
    assert result is not None
    message, receipt = result
    assert "couldn't open the editable transfer draft" in message
    assert receipt["status"] == "REJECTED"
    assert "is open" not in message


def test_no_tool_receipt_returns_none_and_does_not_invent_an_acknowledgement():
    assert (
        transfer_draft_receipt_message(
            [SimpleNamespace(type="ai", content="Your draft is ready", tool_calls=[])]
        )
        is None
    )


def test_receipt_without_tool_call_id_never_matches_an_arbitrary_draft_action():
    result = transfer_draft_receipt_message(_messages(tool_call_id=""))
    assert result is not None
    message, receipt = result
    assert "couldn't match it to the action requested" in message
    assert receipt["status"] == "UNMATCHED_RECEIPT"
    assert "is open" not in message
