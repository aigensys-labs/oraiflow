"""Verified customer-facing acknowledgements for browser action receipts."""

from __future__ import annotations

from typing import Any

from shared.contracts.registry import (
    ContractViolation,
    parse_result_envelope,
    validate_capability_input,
)
from shared.pending_task import message_type, message_value


def transfer_draft_receipt_message(
    messages: list[Any],
    *,
    source_accounts: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Build a final acknowledgement from a matching draft action + browser receipt.

    This fallback is for the edge case where the workflow metadata is missing
    after CopilotKit rehydrates a browser ToolMessage. It does not claim success
    from the ToolMessage alone: the preceding runtime-generated tool call,
    typed input contract, result envelope and workflow/action IDs must all match.
    """
    if not messages:
        return None
    result_message = messages[-1]
    if (
        message_type(result_message) != "tool"
        or message_value(result_message, "name", "") != "prepare_transfer_draft"
    ):
        return None

    raw = str(message_value(result_message, "content", ""))
    try:
        envelope = parse_result_envelope("frontend.transfer.prepare_draft", raw)
    except (TypeError, ValueError, ContractViolation):
        return (
            "I couldn't verify that the editable transfer draft opened. Please check the draft view. No payment was submitted.",
            {"capability_id": "frontend.transfer.prepare_draft", "status": "INVALID_RECEIPT"},
        )

    tool_call_id = str(message_value(result_message, "tool_call_id", "") or "")
    matching_call = None
    for message in reversed(messages[:-1]):
        for call in message_value(message, "tool_calls", None) or []:
            if not isinstance(call, dict) or call.get("name") != "prepare_transfer_draft":
                continue
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            call_id = str(call.get("id", ""))
            action_id = str(args.get("action_id", ""))
            if tool_call_id and tool_call_id in {call_id, action_id}:
                matching_call = (call, args)
                break
        if matching_call:
            break

    if not matching_call:
        return (
            "I received a transfer-draft result but couldn't match it to the action requested in this conversation. Please check the draft view. No payment was submitted.",
            {"capability_id": "frontend.transfer.prepare_draft", "status": "UNMATCHED_RECEIPT"},
        )

    _call, args = matching_call
    try:
        validate_capability_input("frontend.transfer.prepare_draft", args)
    except ContractViolation:
        return (
            "I couldn't verify the transfer-draft request. Please check the draft view. No payment was submitted.",
            {"capability_id": "frontend.transfer.prepare_draft", "status": "INVALID_ACTION_INPUT"},
        )

    outcome = str(envelope.get("outcome", ""))
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    if outcome != "SUCCESS" or data.get("status") != "draft_opened":
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        reason = str(
            error.get("message")
            or data.get("reason")
            or "the application did not confirm the draft"
        )
        return (
            f"I couldn't open the editable transfer draft: {reason}. No payment was submitted.",
            {
                "capability_id": "frontend.transfer.prepare_draft",
                "status": str(data.get("status") or outcome or "REJECTED"),
            },
        )

    workflow_id = str(args.get("workflow_id", ""))
    action_id = str(args.get("action_id", ""))
    if data.get("workflow_id") != workflow_id or data.get("action_id") != action_id:
        return (
            "I couldn't match the browser confirmation to this transfer draft. Please check the draft view. No payment was submitted.",
            {"capability_id": "frontend.transfer.prepare_draft", "status": "MISMATCHED_RECEIPT"},
        )

    source_id = str(args.get("from_account_id", ""))
    source = next(
        (
            item
            for item in (source_accounts or [])
            if isinstance(item, dict) and str(item.get("account_id", "")) == source_id
        ),
        None,
    )
    source_name = str(source.get("account_name", "")) if source else ""
    amount = str(args.get("amount", "")).strip()
    currency = str(args.get("currency", "")).upper()
    beneficiary = str(args.get("beneficiary", "the saved recipient")).strip()
    transfer_label = " ".join(part for part in (amount, currency) if part)
    if source_name:
        reply = (
            f"Your editable transfer draft for {transfer_label} to {beneficiary} from {source_name} is open. "
            "You can review or close it. Nothing has been previewed, approved or submitted."
        )
    else:
        reply = (
            f"Your editable transfer draft for {transfer_label} to {beneficiary} is open. "
            "You can review or close it. Nothing has been previewed, approved or submitted."
        )
    receipt = {
        "capability_id": "frontend.transfer.prepare_draft",
        "workflow_id": workflow_id,
        "action_id": action_id,
        "status": "draft_opened",
    }
    return reply, receipt
