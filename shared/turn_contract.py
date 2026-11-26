"""Versioned customer-turn outcome contract.

A turn outcome is derived from validated capability result envelopes emitted
since the latest customer message. It records execution facts; it is not an
LLM assertion, authorization decision, or replacement for OCCF validation.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.task_contract import TurnTaskOutcome
from shared.pending_task import message_metadata, message_type, message_value

from shared.contracts.registry import (
    ContractViolation, capability_by_name, get_capability, get_registry,
    validate_result_envelope, validate_schema,
)


class TurnOutcome(BaseModel):
    """Normalized outcome of the latest customer turn, including terminal action state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.2.0"] = "1.2.0"
    turn_id: str = Field(min_length=1, max_length=160)
    trace_ids: tuple[str, ...] = ()
    outcome: Literal[
        "RESPONSE_ONLY",
        "CAPABILITY_SUCCEEDED",
        "CAPABILITY_PARTIAL_OR_EMPTY",
        "CAPABILITY_FAILED",
        "MULTIPLE_CAPABILITY_OUTCOMES",
        "FRONTEND_ACTION_SUCCEEDED",
        "FRONTEND_ACTION_FAILED",
        "MULTIPLE_EXECUTION_OUTCOMES",
    ]
    successful_capabilities: tuple[str, ...] = ()
    partial_or_empty_capabilities: tuple[str, ...] = ()
    failed_capabilities: tuple[str, ...] = ()
    successful_frontend_actions: tuple[str, ...] = ()
    failed_frontend_actions: tuple[str, ...] = ()
    task_outcomes: tuple[TurnTaskOutcome, ...] = ()


def _message_type(message: Any) -> str:
    return message_type(message)


def _message_content(message: Any) -> Any:
    return message_value(message, "content", "")


def _turn_start_index(messages: list[Any]) -> int:
    return max(
        (index for index, message in enumerate(messages) if _message_type(message) == "human"),
        default=-1,
    )


def _derive_task_outcomes(items: list[Any], start: int) -> tuple[TurnTaskOutcome, ...]:
    """Correlate runtime task plans with their actual validated ToolMessages."""
    task_specs: dict[str, dict[str, Any]] = {}
    result_messages: dict[str, list[Any]] = {}
    call_to_tasks: dict[str, list[str]] = {}
    for message in items[start + 1:]:
        metadata = message_metadata(message)
        if _message_type(message) == "ai":
            plan = metadata.get("oraiflow_task_plan")
            if isinstance(plan, dict):
                for spec in plan.get("tasks", []):
                    if isinstance(spec, dict) and spec.get("task_id"):
                        task_id = str(spec["task_id"])
                        task_specs[task_id] = dict(spec)
                        for call_id in spec.get("tool_call_ids", []) or []:
                            call_to_tasks.setdefault(str(call_id), []).append(task_id)
            mapping = metadata.get("oraiflow_task_call_map", {})
            if isinstance(mapping, dict):
                for call_id, task_ids in mapping.items():
                    values = task_ids if isinstance(task_ids, list) else [task_ids]
                    call_to_tasks[str(call_id)] = [str(value) for value in values if value]
        elif _message_type(message) == "tool":
            task_ids = metadata.get("oraiflow_task_ids")
            if not isinstance(task_ids, list):
                task_id = metadata.get("oraiflow_task_id")
                task_ids = [task_id] if task_id else []
            if not task_ids:
                task_ids = call_to_tasks.get(str(message_value(message, "tool_call_id", "")), [])
            for task_id in task_ids:
                if task_id:
                    result_messages.setdefault(str(task_id), []).append(message)

    outcomes: list[TurnTaskOutcome] = []
    for task_id, spec in task_specs.items():
        intent_id = str(spec.get("intent_id", "UNKNOWN"))
        capability_ids = tuple(str(value) for value in spec.get("capability_ids", []) if value)
        status = str(spec.get("status_hint", "PLANNED"))
        reason_code = spec.get("reason_code")
        result_refs: list[str] = []
        trace_ids: list[str] = []
        results = result_messages.get(task_id, [])
        if results:
            statuses: list[str] = []
            for message in results:
                tool_name = str(message_value(message, "name", "") or "")
                registered = capability_by_name(tool_name, kind="mcp") or capability_by_name(tool_name, kind="frontend")
                if not registered:
                    # Presentation renderers are registered application
                    # resources, not banking capabilities. A correlated
                    # ToolMessage is their explicit browser receipt.
                    presentation = next((
                        (presentation_id, item)
                        for presentation_id, item in get_registry().get("presentations", {}).items()
                        if str(item.get("name", "")) == tool_name
                    ), None)
                    if presentation is not None:
                        raw_content = _message_content(message)
                        if isinstance(raw_content, str) and raw_content.strip().casefold() in {"error", "failed", "rejected"}:
                            statuses.append("FAILED")
                        else:
                            statuses.append("SUCCEEDED")
                        result_refs.append(str(message_value(message, "tool_call_id", "") or ""))
                        continue
                    statuses.append("FAILED")
                    continue
                capability_id = registered[0]
                try:
                    raw = _message_content(message)
                    envelope = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
                    validated = validate_result_envelope(capability_id, envelope)
                except (TypeError, ValueError, ContractViolation):
                    statuses.append("FAILED")
                    continue
                result_refs.append(str(message_value(message, "tool_call_id", "") or ""))
                trace = str(validated.get("trace_id", ""))
                if trace:
                    trace_ids.append(trace)
                outcome = str(validated.get("outcome", ""))
                if outcome == "SUCCESS":
                    expected_status = get_capability(capability_id).get("result_semantics", {}).get("success_status")
                    actual_status = (validated.get("data") or {}).get("status") if isinstance(validated.get("data"), dict) else None
                    statuses.append("SUCCEEDED" if not expected_status or actual_status == expected_status else "FAILED")
                elif outcome == "PARTIAL":
                    statuses.append("PARTIAL")
                elif outcome == "EMPTY":
                    statuses.append("EMPTY")
                elif outcome in {"UNAVAILABLE", "TIMEOUT", "TRANSPORT_UNAVAILABLE"}:
                    statuses.append("UNAVAILABLE")
                    if not reason_code and isinstance(validated.get("error"), dict):
                        reason_code = validated["error"].get("code")
                else:
                    statuses.append("FAILED")
                    if not reason_code and isinstance(validated.get("error"), dict):
                        reason_code = validated["error"].get("code")
            if statuses and all(value == "SUCCEEDED" for value in statuses):
                status = "SUCCEEDED"
            elif "FAILED" in statuses:
                status = "FAILED" if not any(value == "SUCCEEDED" for value in statuses) else "PARTIAL"
            elif "UNAVAILABLE" in statuses:
                status = "UNAVAILABLE"
            elif "PARTIAL" in statuses:
                status = "PARTIAL"
            elif "EMPTY" in statuses:
                status = "EMPTY"
        outcomes.append(TurnTaskOutcome(
            task_id=task_id,
            intent_id=intent_id,
            status=status if status in {
                "PLANNED", "SUCCEEDED", "PARTIAL", "EMPTY", "CLARIFICATION_REQUIRED",
                "REJECTED", "FAILED", "UNAVAILABLE", "NOT_EXECUTED",
            } else "FAILED",
            capability_ids=capability_ids,
            result_references=tuple(value for value in result_refs if value),
            trace_ids=tuple(sorted(set(trace_ids))),
            missing_fields=tuple(str(value) for value in spec.get("missing_fields", []) if value),
            reason_code=str(reason_code) if reason_code else None,
        ))
    return tuple(outcomes)


def derive_turn_outcome(messages: list[Any] | tuple[Any, ...], *, fallback_turn_id: str) -> TurnOutcome:
    """Derive an immutable outcome from current-turn, registry-validated results.

    Only ToolMessages after the latest HumanMessage are considered. MCP
    envelopes are validated against the exact registered capability contract.
    Browser action receipts are recognized only for registered frontend tools
    and explicit success statuses.
    """
    items = list(messages or [])
    start = _turn_start_index(items)
    turn_id = str(fallback_turn_id or "unknown-turn")[:160]
    if start >= 0:
        message_id = message_value(items[start], "id", None)
        if message_id:
            turn_id = str(message_id)[:160]

    successful: set[str] = set()
    partial_or_empty: set[str] = set()
    failed: set[str] = set()
    frontend_success: set[str] = set()
    frontend_failed: set[str] = set()
    trace_ids: set[str] = set()

    for message in items[start + 1:]:
        if _message_type(message) != "tool":
            continue
        tool_name = str(message_value(message, "name", "") or "")
        registered_mcp = capability_by_name(tool_name, kind="mcp")
        if registered_mcp:
            capability_id = registered_mcp[0]
            raw = _message_content(message)
            try:
                envelope = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
                validated = validate_result_envelope(capability_id, envelope)
            except (TypeError, ValueError, ContractViolation):
                # An invalid/malformed tool response is a failure, never success.
                failed.add(capability_id)
                continue
            trace_ids.add(str(validated.get("trace_id", "")))
            outcome = validated.get("outcome")
            if outcome == "SUCCESS":
                successful.add(capability_id)
            elif outcome in {"EMPTY", "PARTIAL"}:
                partial_or_empty.add(capability_id)
            else:
                failed.add(capability_id)
            continue

        registered_frontend = capability_by_name(tool_name, kind="frontend")
        if registered_frontend:
            capability_id = registered_frontend[0]
            raw = _message_content(message)
            try:
                payload = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
                validated = validate_result_envelope(capability_id, payload)
            except (TypeError, ValueError, ContractViolation):
                # An unvalidated browser receipt is an explicit failure, never
                # a success inferred from an ad-hoc status string.
                frontend_failed.add(capability_id)
                continue
            trace_ids.add(str(validated.get("trace_id", "")))
            if validated.get("outcome") == "SUCCESS":
                expected_status = get_capability(capability_id).get("result_semantics", {}).get("success_status")
                actual_status = (validated.get("data") or {}).get("status")
                if expected_status and actual_status == expected_status:
                    frontend_success.add(capability_id)
                else:
                    # A schema-valid result with the wrong semantic status is
                    # not proof that this particular action completed.
                    frontend_failed.add(capability_id)
            else:
                frontend_failed.add(capability_id)

    mcp_buckets = sum(bool(bucket) for bucket in (successful, partial_or_empty, failed))
    has_mcp = bool(successful or partial_or_empty or failed)
    has_frontend = bool(frontend_success or frontend_failed)
    if (mcp_buckets > 1) and has_frontend:
        outcome = "MULTIPLE_EXECUTION_OUTCOMES"
    elif mcp_buckets > 1:
        outcome = "MULTIPLE_CAPABILITY_OUTCOMES"
    elif frontend_success and frontend_failed:
        outcome = "MULTIPLE_EXECUTION_OUTCOMES"
    elif has_mcp and has_frontend:
        outcome = "MULTIPLE_EXECUTION_OUTCOMES"
    elif successful:
        outcome = "CAPABILITY_SUCCEEDED"
    elif partial_or_empty:
        outcome = "CAPABILITY_PARTIAL_OR_EMPTY"
    elif failed:
        outcome = "CAPABILITY_FAILED"
    elif frontend_success:
        outcome = "FRONTEND_ACTION_SUCCEEDED"
    elif frontend_failed:
        outcome = "FRONTEND_ACTION_FAILED"
    else:
        outcome = "RESPONSE_ONLY"

    outcome_contract = TurnOutcome(
        turn_id=turn_id,
        trace_ids=tuple(sorted(item for item in trace_ids if item)),
        outcome=outcome,
        successful_capabilities=tuple(sorted(successful)),
        partial_or_empty_capabilities=tuple(sorted(partial_or_empty)),
        failed_capabilities=tuple(sorted(failed)),
        successful_frontend_actions=tuple(sorted(frontend_success)),
        failed_frontend_actions=tuple(sorted(frontend_failed)),
        task_outcomes=_derive_task_outcomes(items, start),
    )
    # The versioned turn-outcome schema in OCCF is the wire-contract source of
    # truth; the Pydantic model supplies the typed Python representation.
    validate_schema("turn_outcome", "schema", get_registry()["turn_outcome_schema"], outcome_contract.model_dump(mode="json"))
    return outcome_contract
