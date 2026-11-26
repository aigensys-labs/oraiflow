"""Turn-ownership policy for deterministic saved-contact response rendering."""

from __future__ import annotations

import re
from typing import Any

from shared.pending_task import latest_task_owner, message_metadata, message_type, message_value

_TRANSFER_INTENT = "TRANSFER_PREPARATION"
_CONTACT_INTENT = "SAVED_CONTACT_LOOKUP"
_ORDINAL_OR_SHORT_SELECTION = re.compile(
    r"^(?:the\s+)?(?:latter|former|first|second|last|other|one|two|1|2|option\s*[1-9]\d*)[.!?\s]*$",
    re.IGNORECASE,
)


def _latest_user_index(messages: list[Any]) -> int:
    return max(
        (index for index, message in enumerate(messages) if message_type(message) == "human"),
        default=-1,
    )


def _latest_user_text(messages: list[Any]) -> str:
    index = _latest_user_index(messages)
    if index < 0:
        return ""
    content = message_value(messages[index], "content", "")
    if isinstance(content, list):
        content = " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return str(content or "").strip()


def _has_transfer_task_ownership(messages: list[Any]) -> bool:
    owned = latest_task_owner(messages)
    if owned is not None:
        task, owner_index = owned
        if task.domain == "transfer":
            latest_user_index = _latest_user_index(messages)
            # Active state survives into the next customer reply. A completed
            # DRAFT_OPENED marker owns only the turn in which the ack was emitted;
            # it must not suppress a genuinely new contact lookup later.
            if task.status == "ACTIVE":
                return True
            if (
                task.status == "COMPLETED"
                and task.stage == "DRAFT_OPENED"
                and owner_index > latest_user_index
            ):
                return True

    # Defensive transcript fallback for adapters that drop custom metadata but
    # retain the visible bounded disambiguation prompt. Only used when the
    # customer's latest message is a short selection/ordinal reply.
    latest_text = _latest_user_text(messages)
    if not _ORDINAL_OR_SHORT_SELECTION.fullmatch(latest_text):
        return False
    latest_human_index = _latest_user_index(messages)
    preceding_assistant = next(
        (
            message
            for message in reversed(messages[:latest_human_index])
            if message_type(message) == "ai"
        ),
        None,
    )
    prompt = str(message_value(preceding_assistant, "content", "") or "").casefold()
    return (
        "which person did you mean" in prompt
        or ("saved contact" in prompt and "no transfer draft" in prompt)
        or ("full name" in prompt and "option number" in prompt and "transfer" in prompt)
    )


def is_standalone_saved_contact_lookup(
    messages: list[Any] | tuple[Any, ...],
    discovered_intent_candidates: list[dict[str, Any]] | None = None,
) -> bool:
    """Return True only when a contact lookup owns this turn independently.

    Transfer preparation has precedence over its prerequisite contact search.
    A short disambiguation response such as ``latter`` is never a new standalone
    contact lookup, even if semantic discovery also proposes SAVED_CONTACT_LOOKUP.
    """
    items = list(messages or [])
    if _has_transfer_task_ownership(items):
        return False
    if _ORDINAL_OR_SHORT_SELECTION.fullmatch(_latest_user_text(items)):
        return False

    discovered_ids = {
        str(item.get("intent_id", ""))
        for item in (discovered_intent_candidates or [])
        if isinstance(item, dict)
    }
    current_turn_start = _latest_user_index(items)
    current_plan = None
    for message in reversed(items[current_turn_start + 1 :]):
        if message_type(message) != "ai":
            continue
        plan = message_metadata(message).get("oraiflow_task_plan")
        if isinstance(plan, dict) and isinstance(plan.get("tasks"), list):
            current_plan = plan
            break
    planned_ids = {
        str(task.get("intent_id", ""))
        for task in (current_plan or {}).get("tasks", [])
        if isinstance(task, dict)
    }
    if _TRANSFER_INTENT in discovered_ids or _TRANSFER_INTENT in planned_ids:
        return False
    return _CONTACT_INTENT in discovered_ids or _CONTACT_INTENT in planned_ids
