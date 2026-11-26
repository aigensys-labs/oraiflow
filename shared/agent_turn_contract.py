"""Versioned Agent turn boundary assembled from AG-UI/OCCF inputs.

CopilotKit/AG-UI remains the transport protocol. This envelope is the
server-validated application contract derived from its message, context and
registered tools; it contains identifiers and capability availability, never
customer financial values or authorization tokens.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentIntentCandidate(BaseModel):
    """Candidate intent emitted by the agent before contract and evidence validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    capability_id: str | None = Field(default=None, max_length=120)
    capability_name: str | None = Field(default=None, max_length=120)
    presentation_id: str | None = Field(default=None, max_length=120)
    response_mode: str | None = Field(default=None, max_length=80)
    match_kind: str = Field(default="keyword_overlap", max_length=40)
    score: int = Field(ge=0, le=2000)


class AgentTurnContext(BaseModel):
    """Request-side, per-turn contract presented to the Agent model/runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.1.0"] = "1.1.0"
    registry_version: str = Field(min_length=1, max_length=32)
    turn_id: str = Field(min_length=1, max_length=160)
    workspace_id: str | None = Field(default=None, max_length=80)
    available_mcp_capability_ids: tuple[str, ...] = ()
    available_frontend_capability_ids: tuple[str, ...] = ()
    available_presentation_ids: tuple[str, ...] = ()
    intent_candidates: tuple[AgentIntentCandidate, ...] = ()


class AgentTurnResponse(BaseModel):
    """Response-side completion receipt attached to the final assistant message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.1.0"] = "1.1.0"
    turn_id: str = Field(min_length=1, max_length=160)
    message_id: str = Field(min_length=1, max_length=160)
    response_kind: Literal[
        "TEXT",
        "CLARIFICATION",
        "CAPABILITY_REQUEST",
        "FRONTEND_ACTION_REQUEST",
        "PRESENTATION",
        "ERROR",
    ]
    finalized: bool
    outcome: dict[str, Any]


def build_agent_turn_context(
    registry: dict[str, Any],
    *,
    turn_id: str,
    workspace_id: str | None,
    available_action_names: set[str],
    intent_candidates: tuple[AgentIntentCandidate, ...] = (),
) -> AgentTurnContext:
    """Create the Agent's resource view from the canonical registry and UI offer.

    MCP reads are available only when OCCF marks them agent-invocable. Browser
    actions are additionally filtered to actions exposed in this AG-UI run;
    only capabilities with the explicit `execution.model_invocable` contract
    can become model tools. Runtime-owned draft actions remain available to
    the workflow but are not proposed by the model. Renderers are exposed only
    when the current UI registered their render tool.
    """
    capabilities = registry.get("capabilities", {})
    mcp_ids = tuple(
        sorted(
            capability_id
            for capability_id, item in capabilities.items()
            if item.get("kind") == "mcp"
            and item.get("authorization", {}).get("agent_invocable") is True
            and item.get("execution", {}).get("effect") in {"read", "read_only"}
        )
    )
    frontend_ids = tuple(
        sorted(
            capability_id
            for capability_id, item in capabilities.items()
            if item.get("kind") == "frontend"
            and item.get("authorization", {}).get("agent_invocable") is True
            and item.get("execution", {}).get("model_invocable") is True
            and item.get("name") in available_action_names
        )
    )
    presentation_ids = tuple(
        sorted(
            presentation_id
            for presentation_id, item in registry.get("presentations", {}).items()
            if item.get("name") in available_action_names
        )
    )
    context = AgentTurnContext(
        registry_version=str(registry.get("registry_version", "unknown")),
        turn_id=str(turn_id)[:160],
        workspace_id=workspace_id,
        available_mcp_capability_ids=mcp_ids,
        available_frontend_capability_ids=frontend_ids,
        available_presentation_ids=presentation_ids,
        intent_candidates=intent_candidates,
    )
    from shared.contracts.registry import get_registry, validate_schema

    validate_schema(
        "agent_turn_context",
        "request",
        get_registry()["agent_turn_context_schema"],
        context.model_dump(mode="json"),
    )
    return context


def filter_available_ui_actions(
    registry: dict[str, Any],
    actions: list[Any] | tuple[Any, ...],
    *,
    available_action_names: set[str],
) -> tuple[Any, ...]:
    """Select only registered UI tools exposed in this AG-UI turn.

    The OCCF authorization policy and explicit `execution.model_invocable`
    declaration determine whether a frontend action can be proposed by the
    model. Runtime-owned actions are excluded generically; no capability-name
    denylist is maintained here.
    Registered presentation tools are render-only resources and are included
    only when the browser exposed the corresponding renderer in this run.
    """
    frontend_by_name = {
        str(item.get("name")): item
        for item in registry.get("capabilities", {}).values()
        if item.get("kind") == "frontend" and item.get("name")
    }
    presentation_names = {
        str(item.get("name"))
        for item in registry.get("presentations", {}).values()
        if item.get("name")
    }
    selected: list[Any] = []
    for action in actions or ():
        name = action.get("name", "") if isinstance(action, dict) else getattr(action, "name", "")
        if not name or name not in available_action_names:
            continue
        frontend = frontend_by_name.get(str(name))
        if frontend is not None:
            if frontend.get("authorization", {}).get("agent_invocable") is not True:
                continue
            if frontend.get("execution", {}).get("model_invocable") is not True:
                continue
            selected.append(action)
            continue
        if str(name) in presentation_names:
            selected.append(action)
    return tuple(selected)


def replace_assistant_message_content(
    message: Any,
    *,
    content: str,
    additional_kwargs: dict[str, Any] | None = None,
) -> Any:
    """Finalize an existing assistant message without changing its identity.

    AG-UI clients may already be rendering streamed tokens under this message
    ID. Final policy validation must update that same message, not append a new
    assistant message that visually overwrites the useful streamed response.
    Tool-call proposals are cleared because this helper is only for a validated
    customer-facing final response.
    """
    from copy import deepcopy

    metadata = deepcopy(getattr(message, "additional_kwargs", {}) or {})
    if additional_kwargs:
        metadata.update(deepcopy(additional_kwargs))
    updates = {
        "content": str(content or ""),
        "tool_calls": [],
        "invalid_tool_calls": [],
        "tool_call_chunks": [],
        "additional_kwargs": metadata,
    }
    if hasattr(message, "model_copy"):
        return message.model_copy(update=updates)

    # Lightweight adapter for protocol-level tests and compatible message
    # implementations. Production LangChain messages use model_copy above.
    cloned = deepcopy(message)
    for field, value in updates.items():
        setattr(cloned, field, value)
    return cloned


def attach_agent_turn_response(message: Any, *, turn_id: str, outcome: dict[str, Any]) -> Any:
    """Return a message with a validated response receipt, preserving its identity/content."""
    from copy import deepcopy
    from uuid import uuid4
    from shared.contracts.registry import get_registry, validate_schema

    calls = getattr(message, "tool_calls", None) or []
    call_names = [str(item.get("name", "")) for item in calls if isinstance(item, dict)]
    if call_names:
        registry = get_registry()
        frontend_names = {
            str(item.get("name"))
            for item in registry.get("capabilities", {}).values()
            if item.get("kind") == "frontend"
        }
        presentation_names = {
            str(item.get("name")) for item in registry.get("presentations", {}).values()
        }
        if any(name in presentation_names for name in call_names):
            response_kind = "PRESENTATION"
        elif any(name in frontend_names for name in call_names):
            response_kind = "FRONTEND_ACTION_REQUEST"
        else:
            response_kind = "CAPABILITY_REQUEST"
        finalized = False
    else:
        content = getattr(message, "content", "")
        text = (
            content
            if isinstance(content, str)
            else " ".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
                if isinstance(content, list)
            )
        )
        normalized = str(text or "").casefold()
        response_kind = (
            "CLARIFICATION"
            if any(
                term in normalized
                for term in ("which", "could you clarify", "do you mean", "are you asking")
            )
            else "TEXT"
        )
        finalized = True

    # A turn may contain several internal assistant/tool-request messages.
    # Never reuse the human turn ID as a fallback message ID: each message
    # needs a distinct stable identity for AG-UI reconciliation.
    message_id = str(getattr(message, "id", None) or f"assistant-{uuid4().hex}")[:160]
    normalized_outcome = deepcopy(outcome)
    # The outer AgentTurnResponse envelope remains 1.1.0, while its nested
    # TurnOutcome advanced to 1.2.0 to add typed per-task outcomes. Upgrade
    # previously persisted/in-process 1.1 receipts at this boundary; no result
    # status or capability identity is inferred during the migration.
    if isinstance(normalized_outcome, dict) and normalized_outcome.get("schema_version") == "1.1.0":
        normalized_outcome["schema_version"] = "1.2.0"
        normalized_outcome.setdefault("task_outcomes", [])
    receipt = AgentTurnResponse(
        turn_id=str(turn_id)[:160],
        message_id=message_id,
        response_kind=response_kind,
        finalized=finalized,
        outcome=normalized_outcome,
    )
    receipt_payload = receipt.model_dump(mode="json")
    validate_schema(
        "agent_turn_response",
        "response",
        get_registry()["agent_turn_response_schema"],
        receipt_payload,
    )
    metadata = deepcopy(getattr(message, "additional_kwargs", {}) or {})
    metadata["oraiflow_agent_turn_response"] = receipt_payload
    if hasattr(message, "model_copy"):
        return message.model_copy(update={"id": message_id, "additional_kwargs": metadata})
    setattr(message, "id", message_id)
    setattr(message, "additional_kwargs", metadata)
    return message
