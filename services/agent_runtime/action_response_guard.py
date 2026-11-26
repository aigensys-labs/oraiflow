"""One-shot final-response guards for acknowledged frontend actions.

CopilotKit/AG-UI can replay a turn after a browser tool returns. The tool result
must first be validated and converted into a customer-facing assistant
acknowledgement. That acknowledgement carries standard message identity and
receipt metadata so the graph can then stop replay safely.
"""

from __future__ import annotations

from typing import Any

from shared.pending_task import message_type, message_value, pending_task_from_message

RECEIPT_KEY = "oraiflow_frontend_action_receipt"
WORKFLOW_KEY = "oraiflow_transfer_workflow"
ACTION_ACK_NAME = "oraiflow_action_ack"
ACTION_ACK_ID_PREFIX = "oraiflow-action-ack:"
_SUCCESS_STATUSES = {
    "draft_opened",
    "opened",
    "selected",
    "cleared",
    "success",
}


def _get(message: Any, key: str, default: Any = None) -> Any:
    if isinstance(message, dict):
        return message.get(key, default)
    return getattr(message, key, default)


def _message_type(message: Any) -> str:
    return message_type(message)


def _metadata(message: Any) -> dict[str, Any]:
    """Merge LangChain and serialized AG-UI metadata without dropping either."""
    merged: dict[str, Any] = {}
    for key in ("metadata", "additional_kwargs"):
        value = _get(message, key, None)
        if not isinstance(value, dict):
            continue
        nested = value.get("additional_kwargs")
        if isinstance(nested, dict):
            merged.update(nested)
        merged.update(value)
    return merged


def has_successful_frontend_ack_after_latest_user(messages: list[Any] | None) -> bool:
    """Return True only after this turn has a customer-facing action acknowledgement.

    A raw browser ToolMessage is deliberately insufficient: it still needs to
    be validated and converted into an assistant acknowledgement. The action
    acknowledgement must follow the most recent customer turn, so a receipt
    from an earlier interaction cannot block new customer input. LangChain
    objects and serialized AG-UI/CopilotKit dictionaries are both supported.
    """
    items = list(messages or [])
    latest_user_index = max(
        (index for index, message in enumerate(items) if _message_type(message) == "human"),
        default=-1,
    )
    if latest_user_index < 0:
        return False

    current_turn = items[latest_user_index + 1 :]

    # A browser ToolMessage is an execution receipt, not yet a customer-facing
    # final response. It must first pass through _handle_transfer_workflow() or
    # _frontend_action_acknowledgement(), which validate the correlated action
    # and emit a durable assistant acknowledgement. Treating the raw ToolMessage
    # as terminal here skips that acknowledgement path; AG-UI can leave the turn
    # unresolved and later re-enter semantic interpretation, potentially opening
    # a second transfer. Only an assistant acknowledgement below may stop the
    # graph. This ordering is shared by the model-backed and demo AG-UI graphs.

    for message in current_turn:
        if _message_type(message) != "ai":
            continue
        name = str(_get(message, "name", "") or "")
        message_id = str(_get(message, "id", "") or "")
        if name == ACTION_ACK_NAME or message_id.startswith(ACTION_ACK_ID_PREFIX):
            return True

        metadata = _metadata(message)
        receipt = metadata.get(RECEIPT_KEY)
        if isinstance(receipt, dict):
            capability = str(receipt.get("capability_id", ""))
            status = str(receipt.get("status", "")).lower()
            if capability.startswith("frontend.") and status in _SUCCESS_STATUSES:
                return True
        typed_task = pending_task_from_message(message)
        if typed_task is not None:
            if typed_task.domain == "transfer":
                workflow = typed_task.state.get("workflow", {})
                if isinstance(workflow, dict) and (
                    str(workflow.get("stage", "")) == "DRAFT_OPENED"
                    and str(workflow.get("last_action_status", "")).lower() == "draft_opened"
                ):
                    return True
            # A typed task envelope is authoritative; do not inspect legacy
            # transfer metadata on a message owned by another task domain.
            continue
        workflow = metadata.get(WORKFLOW_KEY)
        if isinstance(workflow, dict):  # read-only compatibility for old checkpoints
            if (
                str(workflow.get("stage", "")) == "DRAFT_OPENED"
                and str(workflow.get("last_action_status", "")).lower() == "draft_opened"
            ):
                return True
    return False
