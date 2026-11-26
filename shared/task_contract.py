"""Shared versioned task outcome contract used by Agent Runtime and UI receipts."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class TurnTaskOutcome(BaseModel):
    """Typed, independently traceable outcome for one interpreted task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    task_id: str = Field(min_length=1, max_length=160)
    intent_id: str = Field(min_length=1, max_length=80)
    status: Literal[
        "PLANNED",
        "SUCCEEDED",
        "PARTIAL",
        "EMPTY",
        "CLARIFICATION_REQUIRED",
        "REJECTED",
        "FAILED",
        "UNAVAILABLE",
        "NOT_EXECUTED",
    ]
    capability_ids: tuple[str, ...] = ()
    result_references: tuple[str, ...] = ()
    trace_ids: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    reason_code: str | None = Field(default=None, max_length=100)


class TurnTaskPlanEntry(BaseModel):
    """Internal, versioned plan for one independently interpreted task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=160)
    intent_id: str = Field(min_length=1, max_length=80)
    status_hint: Literal[
        "PLANNED", "CLARIFICATION_REQUIRED", "REJECTED", "UNAVAILABLE", "NOT_EXECUTED"
    ]
    reason_code: str | None = Field(default=None, max_length=100)
    missing_fields: tuple[str, ...] = ()
    # Safe semantic continuation state. These values are customer-provided or
    # validated context, not executable arguments; legacy plans omit them.
    requested_outcome: str | None = Field(default=None, max_length=40)
    semantic_slots: dict[str, str] = Field(default_factory=dict, max_length=40)
    semantic_entities: dict[str, str] = Field(default_factory=dict, max_length=40)
    semantic_evidence: tuple[dict[str, str], ...] = Field(default_factory=tuple, max_length=40)
    capability_ids: tuple[str, ...] = ()
    capability_id: str | None = Field(default=None, max_length=120)
    kind: Literal["mcp", "frontend"] | None = None
    tool_name: str | None = Field(default=None, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)
    presentation_id: str | None = Field(default=None, max_length=120)
    presentation_name: str | None = Field(default=None, max_length=120)
    response_mode: str | None = Field(default=None, max_length=80)
    tool_call_ids: tuple[str, ...] = ()


class TurnTaskPlan(BaseModel):
    """Versioned runtime plan; model output cannot construct this directly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    # Wall-clock deadline persists across the MCP round-trip so the second
    # model phase cannot silently double the configured per-turn budget.
    model_deadline_epoch: float | None = Field(default=None, gt=0)
    tasks: tuple[TurnTaskPlanEntry, ...] = Field(default_factory=tuple, max_length=5)
