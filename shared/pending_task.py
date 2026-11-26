"""Versioned conversational task context shared by clarification handlers.

Domain workflows retain their domain-specific state machine, while pending
work is carried through a typed envelope. Message metadata is read from both
LangChain messages and serialized AG-UI/CopilotKit transcript dictionaries.
The runtime only resumes context attached to the latest assistant response;
older metadata cannot hijack a new turn.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PENDING_TASK_KEY = "oraiflow_pending_task"


class PendingTaskContext(BaseModel):
    """Serializable context for a customer task that is awaiting a required clarification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    task_id: str = Field(min_length=1, max_length=160)
    domain: Literal["transfer", "navigation", "education", "application"]
    intent_id: str = Field(min_length=1, max_length=100)
    stage: str = Field(min_length=1, max_length=100)
    status: Literal["ACTIVE", "COMPLETED", "CANCELLED"] = "ACTIVE"
    source_message_id: str | None = Field(default=None, max_length=160)
    state: dict[str, Any] = Field(default_factory=dict)


def message_value(message: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a LangChain object or a serialized message."""
    if isinstance(message, dict):
        return message.get(key, default)
    return getattr(message, key, default)


def message_type(message: Any) -> str:
    """Normalize LangChain and AG-UI message role/type spellings."""
    value = str(message_value(message, "type", "") or message_value(message, "role", "")).lower()
    return {"user": "human", "assistant": "ai"}.get(value, value)


def message_metadata(message: Any) -> dict[str, Any]:
    """Merge message metadata containers, preferring LangChain additional_kwargs.

    AG-UI transcript payloads commonly serialize custom message fields under
    ``metadata`` while LangChain stores them in ``additional_kwargs``. Reading
    both avoids losing workflow ownership at the transport/checkpoint boundary.
    """
    merged: dict[str, Any] = {}
    for key in ("metadata", "additional_kwargs"):
        value = message_value(message, key, None)
        if isinstance(value, dict):
            nested = value.get("additional_kwargs")
            if isinstance(nested, dict):
                merged.update(nested)
            merged.update(value)
    return merged


def attach_pending_task(message: Any, task: PendingTaskContext | dict[str, Any]) -> Any:
    """Attach a validated pending-task snapshot without replacing message identity."""
    validated = task if isinstance(task, PendingTaskContext) else PendingTaskContext.model_validate(task)
    metadata = dict(message_value(message, "additional_kwargs", {}) or {})
    metadata[PENDING_TASK_KEY] = validated.model_dump(mode="json")
    message.additional_kwargs = metadata
    return message


def pending_task_from_message(message: Any) -> PendingTaskContext | None:
    """Recover a task snapshot from native or serialized message metadata."""
    raw = message_metadata(message).get(PENDING_TASK_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return PendingTaskContext.model_validate(raw)
    except (ValidationError, TypeError, ValueError):
        return None


def latest_task_owner(
    messages: list[Any] | tuple[Any, ...],
) -> tuple[PendingTaskContext, int] | None:
    """Return the latest assistant-owned task and its transcript index."""
    items = list(messages or [])
    latest_human = max(
        (index for index, message in enumerate(items) if message_type(message) == "human"),
        default=-1,
    )
    assistant_after = [
        index for index in range(latest_human + 1, len(items))
        if message_type(items[index]) == "ai"
    ]
    if assistant_after:
        owner_index = assistant_after[-1]
    else:
        assistant_before = [
            index for index in range(0, latest_human)
            if message_type(items[index]) == "ai"
        ]
        if not assistant_before:
            return None
        owner_index = assistant_before[-1]
    task = pending_task_from_message(items[owner_index])
    return (task, owner_index) if task is not None else None


def latest_task_context(
    messages: list[Any] | tuple[Any, ...], *, domain: str | None = None,
) -> PendingTaskContext | None:
    """Return the latest assistant-owned task, including its terminal status.

    This is useful at response boundaries: a completed transfer task can still
    own the current turn even though it must no longer be resumed as active work.
    """
    owned = latest_task_owner(messages)
    if owned is None:
        return None
    task, _owner_index = owned
    if domain and task.domain != domain:
        return None
    return task


def latest_pending_task(
    messages: list[Any] | tuple[Any, ...], *, domain: str | None = None,
) -> PendingTaskContext | None:
    """Read only the latest assistant-owned ACTIVE task context."""
    task = latest_task_context(messages, domain=domain)
    if task is None or task.status != "ACTIVE":
        return None
    return task
