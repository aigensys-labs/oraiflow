"""LangGraph agent orchestration, task execution, and customer-facing response handling."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import time
import uuid
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph
from copilotkit import CopilotKitState
from langgraph.prebuilt import ToolNode

from services.agent_runtime.mcp_client import MCPToolExecutionError, call_banking_tool
from services.agent_runtime.observability import current_trace_id
from services.agent_runtime.interpretation import (
    InterpretationAssessment,
    assessment_sources,
    assessment_from_tool_arguments,
    confidence_gate_configuration,
    strip_assessment_tool_arguments,
    unique_intent_for_capability,
    validate_interpretation_assessment,
    with_required_interpretation_assessment,
    sanitize_customer_text,
)
from shared.transfer_workflow import (
    apply_frontend_dismissal,
    apply_frontend_draft_result,
    apply_verified_account_ranking,
    apply_verified_source_account_selection,
    build_transfer_workflow,
    build_explicit_transfer_workflow,
    contact_choice_prompt,
    account_choice_prompt,
    latest_pending_workflow,
    mark_draft_requested,
    resolve_contact_reply,
    resolve_currency_reply,
    currency_choice_prompt,
    resolve_search_result,
    resolve_source_account_reply,
    with_workflow,
)
from shared.transfer_slot_reconciliation import reconcile_explicit_transfer_slots
from services.agent_runtime.frontend_receipts import transfer_draft_receipt_message
from services.agent_runtime.action_response_guard import (
    ACTION_ACK_ID_PREFIX, ACTION_ACK_NAME, has_successful_frontend_ack_after_latest_user,
)
from services.agent_runtime.intent_contracts import (
    classify_canonical_read_intent,
    discover_intent_candidates,
)
from services.agent_runtime.academy import (
    academy_direct_guidance_for_request,
    academy_personal_snapshot_intent,
    render_financial_health_explanation,
    render_loan_schedule_explanation,
)
from services.agent_runtime.assistant_capabilities import assistant_capability_catalogue, assistant_capability_help_for_request
from shared.agent_turn_contract import (
    AgentIntentCandidate, AgentTurnContext, attach_agent_turn_response,
    replace_assistant_message_content, build_agent_turn_context,
    filter_available_ui_actions,
)
from services.agent_runtime.scope_policy import (
    CUSTOMER_RESPONSE_TOOL_NAME,
    CustomerResponseEnvelope,
    OUT_OF_SCOPE_MESSAGE,
    SOCIAL_REPLY_MESSAGE,
    is_simple_social_turn,
    INSTRUCTION_OVERRIDE_MESSAGE,
    finalize_response_envelope,
    is_instruction_override_attempt,
    response_protocol_fallback,
    response_tool_description,
    is_generic_scope_response,
)
from services.agent_runtime.workspace_knowledge import workspace_help_for_request
from services.agent_runtime.task_interpretation import (
    AssessmentToolArguments,
    TurnInterpretationAssessment,
    academy_prompt_catalogue,
    bind_required_assessment_model,
    bind_structured_assessment_model,
    build_assessment_prompt,
    parse_assessment_payload,
    missing_task_field_evidence,
    repair_registered_semantic_evidence,
    safe_customer_clarification,
    safe_canonical_read_assessment,
    rank_saved_contacts_for_context,
    safe_explicit_transfer_assessment,
    task_prompt_catalogue,
    workspace_prompt_catalogue,
)
from services.agent_runtime.task_planner import (
    assessment_tool_arguments,
    plan_interpreted_task,
)
from services.agent_runtime.decision_router import (
    build_agent_decision_map,
    decision_policy_identity,
    pre_assessment_routes,
    render_registered_resource_response,
)
from services.agent_runtime.navigation import (
    attach_navigation_task,
    attach_navigation_clarification_from_plan,
    is_incomplete_navigation_request,
    is_explicit_workspace_view_request,
    resolve_pending_navigation_confirmation,
    resolve_workspace_request,
    resolve_workspace_target,
    navigation_action_already_emitted,
    navigation_action_id,
)
from shared.data_policy import redact_for_model
from shared.pending_task import (
    latest_pending_task,
    message_metadata,
    message_type,
    message_value,
)
from shared.contact_lookup_policy import is_standalone_saved_contact_lookup
from shared.turn_contract import derive_turn_outcome
from shared.task_contract import TurnTaskPlan
from shared.contracts.registry import (
    ContractViolation, capability_by_name, create_result_envelope, get_capability,
    get_registry, parse_result_envelope, pydantic_model_for_schema, validate_application_context,
    validate_capability_input, validate_presentation_props, validate_result_envelope,
)

from services.agent_runtime.streaming import invoke_internal_model
from services.agent_runtime.message_content import message_content_to_text
from services.agent_runtime.capability_results import (
    capability_result_diagnostic,
    verified_list_result_diagnostic,
    verified_saved_contact_lookup_summary,
    verified_transaction_analysis_summary,
)

logger = logging.getLogger("oraiflow.agent_runtime.model")


class ORAIState(CopilotKitState):
    """Typed state shared by the LangGraph Agent Runtime nodes for one conversation turn."""

    pass


async def _call_read_capability(tool_name: str, arguments: dict[str, Any] | None = None) -> str:
    """Execute one registered MCP read under its declared contract and retry policy."""
    registered = capability_by_name(tool_name, kind="mcp")
    if not registered:
        raise ValueError(f"Unregistered MCP capability: {tool_name}")
    capability_id, contract = registered
    # Runtime-created planned calls carry a validated semantic assessment next
    # to the OCCF arguments. The StructuredTool adapter used to strip these
    # transport fields, but planner-owned reads bypass that adapter and arrive
    # here directly. Enforce the same boundary centrally so every dispatch
    # path sends only the registered capability's input fields to OCCF/MCP.
    raw_arguments = arguments if isinstance(arguments, dict) else {}
    stripped_fields = [
        str(key) for key in raw_arguments
        if key == "interpretation_assessment" or str(key).startswith("orai_")
    ]
    payload = strip_assessment_tool_arguments(raw_arguments)
    if stripped_fields:
        logger.debug(
            "capability_internal_metadata_stripped trace_id=%s capability=%s fields=%s",
            current_trace_id(), capability_id, ",".join(sorted(stripped_fields)),
        )
    trace_id = current_trace_id()
    try:
        validate_capability_input(capability_id, payload)
    except ContractViolation:
        expected_fields = set(
            (contract.get("input_schema") or {}).get("properties", {}).keys()
        )
        unexpected_fields = sorted(set(payload) - expected_fields)
        logger.warning(
            "capability_input_rejected trace_id=%s capability=%s reason=SCHEMA_VALIDATION supplied_fields=%s unexpected_fields=%s",
            trace_id, capability_id, ",".join(sorted(payload)), ",".join(unexpected_fields),
        )
        envelope = create_result_envelope(
            capability_id, "INVALID_INPUT",
            error={"code": "INVALID_INPUT", "retryable": False},
            trace_id=trace_id,
        )
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))

    execution = contract.get("execution", {})
    retry = execution.get("retry", {})
    max_attempts = max(1, int(retry.get("max_attempts", 1)))
    retry_enabled = retry.get("strategy") == "bounded_exponential" and execution.get("idempotent") is True
    if not retry_enabled:
        max_attempts = 1
    timeout_seconds = max(0.1, int(execution.get("timeout_ms", 10000)) / 1000)
    backoff_ms = retry.get("backoff_ms", [])
    retry_on = set(retry.get("retry_on", []))
    last_error_type = "MCP_UNAVAILABLE"
    started_at = time.perf_counter()

    for attempt in range(1, max_attempts + 1):
        try:
            raw = await asyncio.wait_for(
                call_banking_tool(tool_name, payload), timeout=timeout_seconds
            )
        except MCPToolExecutionError:
            # MCP was reachable and the requested tool ran far enough to
            # return an application-level error. This is not transport
            # unavailability and is never eligible for transport retries.
            last_error_type = "MCP_TOOL_EXECUTION_FAILED"
            logger.warning(
                "mcp_tool_execution_failed trace_id=%s capability=%s",
                trace_id, capability_id,
            )
            envelope = create_result_envelope(
                capability_id, "REJECTED",
                error={"code": last_error_type, "retryable": False},
                trace_id=trace_id,
            )
            break
        except Exception as exc:
            last_error_type = "MCP_TIMEOUT" if isinstance(exc, asyncio.TimeoutError) else "MCP_TRANSPORT_UNAVAILABLE"
            retry_category = "TIMEOUT" if isinstance(exc, asyncio.TimeoutError) else "TRANSPORT_UNAVAILABLE"
            if retry_enabled and retry_category in retry_on and attempt < max_attempts:
                delay_ms = backoff_ms[min(attempt - 1, len(backoff_ms) - 1)] if backoff_ms else 0
                logger.warning(
                    "capability_retry trace_id=%s capability=%s attempt=%d reason=%s delay_ms=%d",
                    trace_id, capability_id, attempt, last_error_type, int(delay_ms),
                )
                if delay_ms:
                    await asyncio.sleep(int(delay_ms) / 1000)
                continue
            envelope = create_result_envelope(
                capability_id, "UNAVAILABLE",
                error={"code": last_error_type, "retryable": True},
                trace_id=trace_id,
            )
            break

        try:
            provider_envelope = parse_result_envelope(capability_id, raw)
        except (TypeError, ValueError, ContractViolation):
            logger.warning(
                "capability_output_rejected trace_id=%s capability=%s error_type=CONTRACT_VIOLATION",
                trace_id, capability_id,
            )
            envelope = create_result_envelope(
                capability_id, "INVALID_RESPONSE",
                error={"code": "INVALID_CONNECTOR_ENVELOPE", "retryable": False},
                trace_id=trace_id,
            )
            break

        provider_outcome = provider_envelope["outcome"]
        provider_error = provider_envelope.get("error") or {}
        if (
            provider_outcome == "UNAVAILABLE"
            and provider_error.get("retryable") is True
            and retry_enabled
            and ({"MCP_TIMEOUT": "TIMEOUT", "TIMEOUT": "TIMEOUT", "MCP_TRANSPORT_UNAVAILABLE": "TRANSPORT_UNAVAILABLE", "TRANSPORT_UNAVAILABLE": "TRANSPORT_UNAVAILABLE"}.get(provider_error.get("code"), provider_error.get("code")) in retry_on)
            and attempt < max_attempts
        ):
            delay_ms = backoff_ms[min(attempt - 1, len(backoff_ms) - 1)] if backoff_ms else 0
            logger.warning(
                "capability_retry trace_id=%s capability=%s attempt=%d reason=PROVIDER_UNAVAILABLE delay_ms=%d",
                trace_id, capability_id, attempt, int(delay_ms),
            )
            if delay_ms:
                await asyncio.sleep(int(delay_ms) / 1000)
            continue

        if provider_outcome in {"SUCCESS", "EMPTY", "PARTIAL"}:
            evidence = dict(provider_envelope["evidence"])
            evidence["provider_trace_id"] = provider_envelope["trace_id"]
            evidence["trace_id"] = trace_id
            envelope = create_result_envelope(
                capability_id,
                provider_outcome,
                data=provider_envelope["data"],
                evidence=evidence,
                trace_id=trace_id,
            )
        else:
            envelope = create_result_envelope(
                capability_id,
                provider_outcome,
                error=provider_error,
                trace_id=trace_id,
            )
        break

    logger.info(
        "capability_execution_finished trace_id=%s capability=%s kind=mcp duration_ms=%d attempts=%d outcome=%s",
        trace_id, capability_id, round((time.perf_counter() - started_at) * 1000), attempt, envelope["outcome"],
    )
    return redact_for_model(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))


def _build_mcp_agent_tool(capability_id: str) -> StructuredTool:
    """Build the internal ToolNode adapter and OCCF validation boundary.

    The interpretation model is not given executable MCP tools. Runtime-created
    calls pass through this adapter only after the task planner has selected
    and validated the capability from OCCF.
    """
    contract = get_capability(capability_id)
    if (contract.get("kind") != "mcp"
            or contract.get("authorization", {}).get("agent_invocable") is not True
            or contract.get("execution", {}).get("effect") not in {"read", "read_only"}):
        raise RuntimeError(f"Capability is not an agent-invocable read-only MCP tool: {capability_id}")
    base_args_model = pydantic_model_for_schema(
        contract["input_schema"],
        "".join(part.title() for part in contract["name"].split("_")) + "Input",
    )
    # Keep the OCCF wire schema unchanged. This adapter carries the validated
    # task assessment through the established final security gate; it is not
    # exposed to the model as an executable tool.
    args_model = with_required_interpretation_assessment(
        base_args_model,
        "".join(part.title() for part in contract["name"].split("_")) + "AssessedInput",
    )

    async def invoke(**arguments):
        # ToolNode may materialize optional model-facing assessment defaults;
        # none of those fields belong on the OCCF wire contract.
        contract_arguments = strip_assessment_tool_arguments(arguments)
        return await _call_read_capability(contract["name"], contract_arguments)

    return StructuredTool.from_function(
        func=None,
        coroutine=invoke,
        name=contract["name"],
        description=contract["description"],
        args_schema=args_model,
        infer_schema=False,
    )


TOOLS = [
    _build_mcp_agent_tool(capability_id)
    for capability_id, contract in get_registry()["capabilities"].items()
    if contract.get("kind") == "mcp"
    and contract.get("authorization", {}).get("agent_invocable") is True
    and contract.get("execution", {}).get("effect") in {"read", "read_only"}
]


def copilot_action_names(state: dict[str, Any]) -> set[str]:
    """Return the frontend action names available in the current CopilotKit state."""
    actions = (state.get("copilotkit") or {}).get("actions") or []
    return {
        action.get("name", "") if isinstance(action, dict) else getattr(action, "name", "")
        for action in actions
    }


def latest_human_text(messages) -> str:
    """Extract the latest customer-authored text from a conversation message sequence."""
    for message in reversed(messages or []):
        if message_type(message) == "human":
            content = message_value(message, "content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return message_content_to_text(content)
            return ""
    return ""


def _capability_result_diagnostic(
    messages, capability_id: str,
) -> tuple[bool, dict[str, Any] | None, str | None, str | None, str | None]:
    """Runtime wrapper keeps trace identity attached to the pure validator."""
    return capability_result_diagnostic(
        list(messages or []), capability_id, trace_id=current_trace_id(),
    )


def _capability_result_for_turn(messages, capability_id: str) -> tuple[bool, dict[str, Any] | None]:
    """Compatibility wrapper returning only presence and validated data envelope."""
    seen, envelope, _reason, _outcome, _error_code = _capability_result_diagnostic(
        messages, capability_id,
    )
    return seen, envelope


def _connector_error_for_turn(messages) -> str | None:
    """Inspect only the latest MCP result in this user turn; never trust raw tool text."""
    latest_user_index = max(
        (index for index, message in enumerate(messages) if message_type(message) == "human"),
        default=-1,
    )
    for message in reversed(messages[latest_user_index + 1:]):
        if message_type(message) != "tool":
            continue
        tool_name = str(message_value(message, "name", "") or "")
        registered = capability_by_name(tool_name, kind="mcp")
        if not registered:
            continue
        try:
            envelope = parse_result_envelope(registered[0], str(message_value(message, "content", "")))
        except (TypeError, ValueError, ContractViolation):
            return tool_name
        return tool_name if envelope.get("trust_class") == "connector_error" else None
    return None


def _connector_failure_message(tool_name: str) -> AIMessage:
    registered = capability_by_name(tool_name, kind="mcp")
    message = registered[1].get("safe_failure_message") if registered else None
    return AIMessage(content=message or "I couldn't retrieve the requested banking information just now. Please try again.")




def _selected_account_context(state: ORAIState) -> dict[str, Any]:
    context_items = (state.get("copilotkit") or {}).get("context") or []
    for item in context_items:
        if not isinstance(item, dict) or not item.get("description", "").startswith("Current ORAIflow workspace"):
            continue
        raw = item.get("value", {})
        try:
            context = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
            return validate_application_context(context)
        except (TypeError, ValueError, ContractViolation):
            logger.warning(
                "application_context_rejected trace_id=%s reason=invalid_read_only_context",
                current_trace_id(),
            )
            return {}
    return {}



def _build_agent_turn_context(
    state: ORAIState,
    current_text: str,
) -> tuple[AgentTurnContext, list[dict[str, Any]]]:
    """Build the request-side contract from the current UI and OCCF registry."""
    registry = get_registry()
    context = _selected_account_context(state)
    messages = list(state.get("messages", []))
    latest_user = next((item for item in reversed(messages) if message_type(item) == "human"), None)
    turn_id = str(message_value(latest_user, "id", None) or current_trace_id() or uuid.uuid4().hex)[:160]
    action_names = copilot_action_names(state)
    discovered = discover_intent_candidates(current_text, limit=5)
    candidate_models = tuple(
        AgentIntentCandidate(
            intent_id=str(item["intent_id"]),
            description=str(item.get("description", ""))[:500],
            capability_id=item.get("capability_id"),
            capability_name=item.get("capability_name"),
            presentation_id=item.get("presentation"),
            response_mode=item.get("response_mode"),
            match_kind=str(item.get("match_kind", "keyword_overlap")),
            score=max(0, min(2000, int(item.get("score", 0)))),
        )
        for item in discovered if item.get("intent_id") in registry.get("intents", {})
    )
    turn_context = build_agent_turn_context(
        registry,
        turn_id=turn_id,
        workspace_id=context.get("workspace"),
        available_action_names=action_names,
        intent_candidates=candidate_models,
    )
    return turn_context, discovered


def _attach_agent_turn_contract(
    message: AIMessage,
    turn_context: AgentTurnContext,
    messages: list[Any] | tuple[Any, ...],
) -> AIMessage:
    """Attach the validated request and response contract without rebuilding the message."""
    turn_outcome = derive_turn_outcome(list(messages or []), fallback_turn_id=turn_context.turn_id)
    metadata = copy.deepcopy(getattr(message, "additional_kwargs", {}) or {})
    metadata["oraiflow_agent_turn_context"] = turn_context.model_dump(mode="json")
    metadata["oraiflow_turn_outcome"] = turn_outcome.model_dump(mode="json")
    message = message.model_copy(update={"additional_kwargs": metadata})
    return attach_agent_turn_response(
        message,
        turn_id=turn_context.turn_id,
        outcome=turn_outcome.model_dump(mode="json"),
    )


def _preflight_candidates(presentation: dict[str, Any], context: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    candidates = []
    for rule in presentation.get("binding", {}).get("preflight", []):
        required_context = rule.get("when_context_present")
        absent_context = rule.get("when_context_absent")
        if required_context and not context.get(required_context):
            continue
        if absent_context and context.get(absent_context):
            continue
        arguments = dict(rule.get("arguments", {}))
        for target, context_key in rule.get("arguments_from_context", {}).items():
            value = context.get(context_key)
            if value is not None:
                arguments[target] = value
        candidates.append((rule["capability"], arguments))
    return candidates


def _component_preflight_tool_calls(response: AIMessage, state: ORAIState) -> list[dict[str, Any]]:
    """Use presentation registry dependencies to schedule any mandatory read first."""
    calls = getattr(response, "tool_calls", None) or []
    messages = state.get("messages", [])
    context = _selected_account_context(state)
    existing_backend_names = {
        call.get("name") for call in calls
        if isinstance(call, dict) and capability_by_name(call.get("name", ""), kind="mcp")
    }
    preflight: list[dict[str, Any]] = []

    for call in calls:
        if not isinstance(call, dict):
            continue
        presentation = get_registry().get("presentations", {}).get(call.get("name"))
        if not presentation:
            continue
        candidates = _preflight_candidates(presentation, context)
        if any(_capability_result_for_turn(messages, capability_id)[0] for capability_id, _ in candidates):
            continue
        for capability_id, arguments in candidates:
            capability = get_capability(capability_id)
            tool_name = capability["name"]
            if tool_name in existing_backend_names:
                continue
            validate_capability_input(capability_id, arguments)
            preflight.append({
                "name": tool_name,
                "args": arguments,
                "id": f"oraiflow-preflight-{uuid.uuid4().hex}",
                "type": "tool_call",
            })
            existing_backend_names.add(tool_name)
            break
    return preflight


def _customer_safe_component_failure(component_name: str, invalid: bool = False) -> AIMessage:
    presentation = get_registry().get("presentations", {}).get(component_name, {})
    if invalid:
        return AIMessage(content="I couldn't verify the banking information returned for that request, so I haven't displayed it. Please try again.")
    return AIMessage(content=presentation.get(
        "failure_message",
        "I couldn't retrieve the requested banking information just now. Please try again.",
    ))


def _presentation_source_result(messages, presentation: dict[str, Any], context: dict[str, Any]):
    for capability_id, _arguments in _preflight_candidates(presentation, context):
        seen, envelope = _capability_result_for_turn(messages, capability_id)
        if seen:
            return capability_id, envelope
    return None, None


def _apply_presentation_transform(transform: list[dict[str, Any]], data: Any, context: dict[str, Any], evidence: dict[str, Any]):
    """Apply the registry-declared, allowlisted presentation transform pipeline."""
    result = data
    for operation in transform:
        op = operation.get("op")
        if op == "wrap_object_as_array":
            if isinstance(result, dict):
                result = [result]
        elif op == "filter_array_by_context":
            context_value = context.get(operation["context_key"])
            ignored = operation.get("ignore_values", [])
            if context_value and context_value not in ignored and isinstance(result, list):
                property_name = operation["property"]
                result = [item for item in result if isinstance(item, dict) and item.get(property_name) == context_value]
        elif op == "set_evidence_scope_from_context":
            context_value = context.get(operation["context_key"])
            if context_value:
                evidence["scope"] = f"{operation.get('prefix', '')}{context_value}"
        else:
            raise ContractViolation("presentation", "binding", f"unknown data transform operation: {op}")
    return result


def _bind_verified_component_data(response: AIMessage, state: ORAIState, context_override: dict[str, Any] | None = None) -> AIMessage:
    """Bind component props only from current-turn, schema-validated contract results."""
    calls = getattr(response, "tool_calls", None) or []
    if not calls:
        return response
    messages = state.get("messages", [])
    context = context_override if context_override is not None else _selected_account_context(state)

    for call in calls:
        presentation_name = call.get("name")
        presentation = get_registry().get("presentations", {}).get(presentation_name)
        if not presentation:
            continue
        capability_id, envelope = _presentation_source_result(messages, presentation, context)
        if not capability_id or not envelope:
            return _customer_safe_component_failure(presentation_name)
        binding = presentation["binding"]
        data = envelope.get("data")
        evidence = dict(envelope.get("evidence") or {})
        try:
            bound_data = _apply_presentation_transform(binding.get("transform", []), data, context, evidence)
            title = binding["title"]
            title_context_key = binding.get("title_context_key")
            title_template = binding.get("title_template")
            if title_context_key and title_template:
                title_context_value = context.get(title_context_key)
                if title_context_value and title_context_value != "All accounts":
                    title = title_template.format(**{title_context_key: title_context_value})
            props = {
                "title": title,
                binding["data_property"]: bound_data,
                "evidence": evidence,
            }
            validate_presentation_props(presentation_name, props)
        except ContractViolation:
            logger.warning(
                "presentation_contract_rejected trace_id=%s presentation=%s capability=%s",
                current_trace_id(), presentation_name, capability_id,
            )
            return _customer_safe_component_failure(presentation_name, invalid=True)
        call["args"] = props
    return response


def canonical_intent_step(intent_id: str, state: ORAIState) -> AIMessage | None:
    """Execute a registry-declared read using its declared response mode.

    Presentation intents schedule their canonical renderer. Grounded-explanation
    intents execute their registered MCP read and then either use a deterministic
    domain explanation or return control to the model with the validated result.
    The runtime does not assume that every read must have a frontend presentation.
    """
    intent = get_registry().get("intents", {}).get(intent_id)
    plan = intent.get("execution_plan", {}) if intent else {}
    messages = list(state.get("messages", []))
    presentation_name = plan.get("presentation")

    if not presentation_name:
        capability_id = plan.get("capability")
        capability = get_registry().get("capabilities", {}).get(capability_id, {}) if capability_id else {}
        if (
            plan.get("response_mode") != "grounded_explanation"
            or not capability_id
            or capability.get("kind") != "mcp"
            or capability.get("authorization", {}).get("agent_invocable") is not True
            or capability.get("execution", {}).get("effect") != "read_only"
        ):
            logger.warning("canonical_intent_unavailable trace_id=%s intent=%s reason=no_registered_response_contract", current_trace_id(), intent_id)
            return AIMessage(content="I couldn't match that request to an available read-only ORAIflow service. Could you clarify what information you need?")

        seen, envelope = _capability_result_for_turn(messages, capability_id)
        if seen:
            if envelope is None:
                return AIMessage(content="I couldn't validate the information returned by ORAIflow, so I won't present it as verified. Please try again.")
            outcome = str(envelope.get("outcome", ""))
            data = envelope.get("data")
            if outcome == "EMPTY":
                if intent_id == "LOAN_INFORMATION":
                    return AIMessage(content="I couldn't find a loan record in your ORAIflow profile. I can still explain principal, interest and APR generally, but I won't invent loan terms or repayment dates.")
                return AIMessage(content="No matching information was found in the records available to ORAIflow.")
            if outcome != "SUCCESS":
                return AIMessage(content="The requested information was incomplete or unavailable, so I can't give you a reliable summary. Please try again.")
            if intent_id == "LOAN_INFORMATION" and isinstance(data, dict):
                return AIMessage(content=render_loan_schedule_explanation(data))
            # The successful MCP ToolMessage remains in the conversation. The
            # model may now explain it naturally under the same validated turn.
            return None

        arguments = dict(plan.get("arguments", {}))
        try:
            validate_capability_input(capability_id, arguments)
        except ContractViolation:
            logger.warning("canonical_intent_invalid_plan trace_id=%s intent=%s capability=%s", current_trace_id(), intent_id, capability_id)
            return AIMessage(content="I couldn't prepare a valid read request for that information. Please try again.")
        call_id = f"oraiflow-contract-read-{uuid.uuid4().hex}"
        message = AIMessage(content="", tool_calls=[{
            "name": capability.get("name"),
            "args": arguments,
            "id": call_id,
            "type": "tool_call",
        }])
        message.additional_kwargs["oraiflow_runtime_owned_read_ids"] = [call_id]
        return message

    presentation = get_registry().get("presentations", {}).get(presentation_name)
    if not presentation:
        return AIMessage(content="I couldn't complete that request using an approved application presentation. Please try again.")
    if presentation_name not in copilot_action_names(state):
        logger.warning(
            "canonical_intent_unavailable trace_id=%s intent=%s reason=presentation_not_registered",
            current_trace_id(), intent_id,
        )
        return AIMessage(content="I couldn't display that verified banking information right now. Please refresh the assistant and try again.")

    failed_connector = _connector_error_for_turn(messages)
    if failed_connector:
        return _connector_failure_message(failed_connector)
    context = _selected_account_context(state)
    if plan.get("ignore_selected_account") is True:
        context = {**context, "selected_account_id": None, "selected_account_name": "All accounts"}
    candidates = _preflight_candidates(presentation, context)
    if not candidates:
        return _customer_safe_component_failure(presentation_name, invalid=True)

    for capability_id, _arguments in candidates:
        seen, envelope = _capability_result_for_turn(messages, capability_id)
        if seen:
            if envelope is None:
                return _customer_safe_component_failure(presentation_name, invalid=True)
            component = AIMessage(content="", tool_calls=[{
                "name": presentation_name,
                "args": {},
                "id": f"oraiflow-contract-render-{uuid.uuid4().hex}",
                "type": "tool_call",
            }])
            return _bind_verified_component_data(component, state, context_override=context)

    capability_id, arguments = candidates[0]
    capability = get_capability(capability_id)
    try:
        validate_capability_input(capability_id, arguments)
    except ContractViolation:
        logger.warning(
            "canonical_intent_invalid_plan trace_id=%s intent=%s capability=%s",
            current_trace_id(), intent_id, capability_id,
        )
        return _customer_safe_component_failure(presentation_name, invalid=True)
    logger.info(
        "canonical_intent_resolved trace_id=%s intent=%s capability=%s presentation=%s",
        current_trace_id(), intent_id, capability_id, presentation_name,
    )
    call_id = f"oraiflow-contract-read-{uuid.uuid4().hex}"
    message = AIMessage(content="", tool_calls=[{
        "name": capability["name"],
        "args": arguments,
        "id": call_id,
        "type": "tool_call",
    }])
    message.additional_kwargs["oraiflow_runtime_owned_read_ids"] = [call_id]
    return message


def _contact_search_candidates_for_turn(messages) -> tuple[bool, list[dict[str, Any]]]:
    """Return only candidates from a validated successful contact search this turn."""
    seen, envelope = _capability_result_for_turn(messages or [], "banking.contacts.search")
    if not seen or envelope is None:
        return False, []
    data = envelope.get("data")
    return True, data if isinstance(data, list) else []


def _validate_transfer_draft_has_unique_contact(
    response: AIMessage, messages, context: dict[str, Any] | None = None,
) -> AIMessage | None:
    """Require a source-account choice and a uniquely searched contact before drafting."""
    calls = getattr(response, "tool_calls", None) or []
    draft_call = next((call for call in calls if isinstance(call, dict) and call.get("name") == "prepare_transfer_draft"), None)
    if not draft_call:
        return None
    args = draft_call.get("args", {})
    context = context if isinstance(context, dict) else {}
    accounts = [item for item in context.get("accounts", []) if isinstance(item, dict)]
    source = next((item for item in accounts if item.get("account_id") == args.get("from_account_id")), None)
    currency = str(args.get("currency", "")).upper()
    if not source or str(source.get("currency", "")).upper() != currency:
        return AIMessage(content="Please choose an available source account in Accounts or Activity that uses the requested currency. I won't select an account on your behalf. No transfer draft has been opened.")

    selected_id = context.get("selected_account_id")
    latest_text = latest_human_text(messages)
    explicitly_named_source = any(
        item.get("account_name")
        and str(item["account_name"]).casefold() in latest_text.casefold()
        and item.get("account_id") == args.get("from_account_id")
        for item in accounts
    )
    if selected_id and selected_id != args.get("from_account_id") and not explicitly_named_source:
        return AIMessage(content="The proposed source account differs from the account currently selected in the workspace. Select the intended account or name it explicitly in your request, then try again. No transfer draft has been opened.")
    if not selected_id and not explicitly_named_source:
        eligible = [item for item in accounts if str(item.get("currency", "")).upper() == currency]
        if len(eligible) != 1 or eligible[0].get("account_id") != args.get("from_account_id"):
            return AIMessage(content="Which source account should I use? Please select one in Accounts or Activity, then repeat the transfer request. I won't default to the first account. No transfer draft has been opened.")

    seen, candidates = _contact_search_candidates_for_turn(messages)
    if not seen:
        return AIMessage(content="I need to verify the recipient against your saved Contacts before opening a transfer draft. No draft has been opened.")
    if not candidates:
        return AIMessage(content="I couldn't find an active saved contact matching that recipient. Please check Contacts. No transfer draft has been opened.")
    if len(candidates) > 1:
        pending = build_transfer_workflow({
            "slots": {"amount": args.get("amount", ""), "currency": args.get("currency", "")},
            "entities": {"beneficiary": args.get("beneficiary", "the requested name")},
        }, context)
        pending["source_account_id"] = str(args.get("from_account_id", ""))
        pending["contact_candidates"] = candidates
        pending["stage"] = "AWAITING_CONTACT"
        return _workflow_message(contact_choice_prompt(pending), pending)
    selected = candidates[0]
    if (args.get("beneficiary_contact_id") != selected.get("contact_id")
            or _normalise_contract_name(args.get("beneficiary", "")) != _normalise_contract_name(selected.get("full_name", ""))):
        return AIMessage(content="The recipient details did not match the uniquely verified saved contact. Please try again; no transfer draft was opened.")
    return None


def _normalise_contract_name(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _build_turn_assessment_tool() -> StructuredTool:
    """Create the sole model-facing, provider-native semantic output schema.

    The schema is the complete assessment at the top level; it deliberately
    does not require the model to JSON-encode a second object inside a string.
    This tool is intercepted by the runtime and is never executed.
    """
    def not_executable(**_arguments):
        raise RuntimeError("Turn assessments are intercepted by the Agent Runtime")

    return StructuredTool.from_function(
        func=not_executable,
        name="oraiflow_assess_turn",
        description=(
            "Return the complete structured semantic interpretation for this customer turn: "
            "registered intent IDs, requested outcomes, grounded slots/entities, exact evidence, "
            "nullable confidence values and any focused clarification. Do not select capabilities, "
            "tool names, API routes or executable arguments."
        ),
        args_schema=AssessmentToolArguments,
        infer_schema=False,
    )




def _is_structured_output_compatibility_error(exc: Exception) -> bool:
    """Identify provider schema-adapter incompatibility without retrying outages.

    Network errors, provider timeouts and service failures must propagate to the
    normal terminal-outcome handler. Only errors that indicate the selected
    structured-output mechanism is unsupported may trigger the bounded adapter
    fallback.
    """
    if isinstance(exc, NotImplementedError):
        return True

    name = type(exc).__name__.casefold()
    message = str(exc).casefold()
    compatibility_markers = (
        "response_format", "json_schema", "structured output", "structured_output",
        "tool_choice", "unsupported schema", "schema is not supported",
        "function calling is not supported", "tool use is not supported",
        "does not support structured", "not supported by this model",
    )
    has_compatibility_marker = any(marker in message for marker in compatibility_markers)
    if not has_compatibility_marker:
        return False

    return isinstance(exc, (TypeError, ValueError, AttributeError)) or name in {
        "badrequesterror", "unsupportedvalueerror", "invalidrequesterror"
    }


def _assessment_from_model_response(response: Any):
    """Extract a Pydantic/direct tool/JSON response through the same strict parser."""
    parsed = response
    if isinstance(response, dict) and "parsed" in response:
        parsed = response.get("parsed")
        if parsed is None:
            parsed = response.get("raw")
    if isinstance(parsed, TurnInterpretationAssessment):
        return parse_assessment_payload(parsed.model_dump(mode="json"))
    if isinstance(parsed, dict) and "tasks" in parsed:
        return parse_assessment_payload(parsed)

    calls = getattr(parsed, "tool_calls", None) or []
    assessment_calls = [
        call for call in calls
        if isinstance(call, dict) and call.get("name") == "oraiflow_assess_turn"
    ]
    if len(assessment_calls) == 1 and len(calls) == 1:
        return parse_assessment_payload(assessment_calls[0].get("args", {}))

    content = getattr(parsed, "content", "")
    if isinstance(content, list):
        content = message_content_to_text(content)
    if isinstance(content, str) and content.strip().startswith("{"):
        return parse_assessment_payload(content.strip())
    return None, {"code": "ASSESSMENT_OUTPUT_MISSING", "detail_type": type(parsed).__name__}




def _build_customer_response_tool() -> StructuredTool:
    """Create a runtime-only response envelope; this tool is never executed."""
    def not_executable(**_arguments):
        raise RuntimeError("Customer response envelopes are intercepted by Agent Runtime")

    return StructuredTool.from_function(
        func=not_executable,
        name=CUSTOMER_RESPONSE_TOOL_NAME,
        description=response_tool_description(),
        args_schema=CustomerResponseEnvelope,
        infer_schema=False,
    )


def _is_standalone_saved_contact_lookup(
    messages, discovered_intent_candidates: list[dict[str, Any]] | None = None,
) -> bool:
    """Recognize a standalone contact search without stealing transfer context.

    Transfer preparation owns its prerequisite contacts lookup and every
    clarification reply (including ``latter``). Intent discovery alone is not
    sufficient evidence to render that capability as a standalone customer task.
    """
    return is_standalone_saved_contact_lookup(
        list(messages or []), discovered_intent_candidates,
    )


def _customer_response_from_protocol(
    response: AIMessage,
    messages,
    *,
    discovered_intent_candidates: list[dict[str, Any]] | None = None,
) -> AIMessage | None:
    """Intercept and validate the model's non-action response envelope."""
    calls = getattr(response, "tool_calls", None) or []
    response_calls = [call for call in calls if isinstance(call, dict) and call.get("name") == CUSTOMER_RESPONSE_TOOL_NAME]
    if not response_calls:
        return None
    if len(response_calls) != 1 or len(calls) != 1:
        logger.warning(
            "customer_response_policy_rejected trace_id=%s reason=MIXED_RESPONSE_AND_ACTION",
            current_trace_id(),
        )
        message = AIMessage(content=OUT_OF_SCOPE_MESSAGE)
        return message
    raw_args = response_calls[0].get("args", {})
    turn_outcome = derive_turn_outcome(
        list(messages or []),
        fallback_turn_id=current_trace_id(),
    )
    successful_capabilities = set(turn_outcome.successful_capabilities)
    partial_or_empty_capabilities = set(turn_outcome.partial_or_empty_capabilities)
    failed_capabilities = set(turn_outcome.failed_capabilities)
    successful_frontend_actions = set(turn_outcome.successful_frontend_actions)
    failed_frontend_actions = set(turn_outcome.failed_frontend_actions)
    task_outcomes = [item.model_dump(mode="json") for item in turn_outcome.task_outcomes]
    transaction_summary = None
    contact_lookup_summary = None
    if (
        "banking.contacts.search" in (successful_capabilities | partial_or_empty_capabilities)
        and _is_standalone_saved_contact_lookup(messages, discovered_intent_candidates)
    ):
        contact_lookup_summary = verified_saved_contact_lookup_summary(
            list(messages or []), trace_id=current_trace_id(),
        )
    if "banking.transactions.analyze" in (
        successful_capabilities | partial_or_empty_capabilities
    ):
        transaction_summary = verified_transaction_analysis_summary(
            list(messages or []), trace_id=current_trace_id(),
        )
        if transaction_summary is None:
            logger.warning(
                "transaction_analysis_summary_fallback_unavailable trace_id=%s reason=INVALID_VERIFIED_DATA",
                current_trace_id(),
            )
    streamed_content = getattr(response, "content", "")
    if isinstance(streamed_content, list):
        streamed_content = message_content_to_text(streamed_content)
    result = finalize_response_envelope(
        raw_args,
        current_user_text=latest_human_text(messages or []),
        registered_intents=set(get_registry().get("intents", {}).keys()),
        successful_capabilities=successful_capabilities,
        partial_or_empty_capabilities=partial_or_empty_capabilities,
        failed_capabilities=failed_capabilities,
        successful_frontend_actions=successful_frontend_actions,
        failed_frontend_actions=failed_frontend_actions,
        task_outcomes=task_outcomes,
        streamed_customer_text=str(streamed_content or ""),
        discovered_intent_candidates=discovered_intent_candidates or [],
    )
    if result is None:
        logger.warning(
            "customer_response_policy_rejected trace_id=%s reason=INVALID_RESPONSE_ENVELOPE",
            current_trace_id(),
        )
        fallback = response_protocol_fallback(
            str(streamed_content or ""),
            current_user_text=latest_human_text(messages or []),
            successful_capabilities=successful_capabilities,
            partial_or_empty_capabilities=partial_or_empty_capabilities,
            failed_capabilities=failed_capabilities,
            successful_frontend_actions=successful_frontend_actions,
            failed_frontend_actions=failed_frontend_actions,
            discovered_intent_candidates=discovered_intent_candidates or [],
        )
        if contact_lookup_summary:
            fallback = contact_lookup_summary
        elif transaction_summary and is_generic_scope_response(fallback):
            fallback = transaction_summary
        metadata = dict(getattr(response, "additional_kwargs", {}) or {})
        metadata["oraiflow_turn_outcome"] = turn_outcome.model_dump(mode="json")
        # Preserve the streamed assistant message's identity while replacing
        # its private response-tool call with validated customer prose. AG-UI
        # can then finalize the same message rather than presenting a second
        # response that appears to overwrite the useful streamed answer.
        message = replace_assistant_message_content(
            response, content=fallback, additional_kwargs=metadata,
        )
    else:
        classification, customer_text = result
        if contact_lookup_summary and not failed_capabilities:
            customer_text = contact_lookup_summary
        elif (
            transaction_summary
            and classification == "IN_SCOPE"
            and is_generic_scope_response(customer_text)
        ):
            customer_text = transaction_summary
        logger.info(
            "customer_response_policy_decision trace_id=%s classification=%s",
            current_trace_id(), classification,
        )
        metadata = dict(getattr(response, "additional_kwargs", {}) or {})
        metadata["oraiflow_turn_outcome"] = turn_outcome.model_dump(mode="json")
        message = replace_assistant_message_content(
            response, content=customer_text, additional_kwargs=metadata,
        )
    return message


def _pending_navigation_message(state: ORAIState) -> AIMessage | None:
    """Continue a typed navigation clarification or confirmation."""
    messages = state.get("messages", [])
    current_text = latest_human_text(messages)
    pending = latest_pending_task(messages, domain="navigation")
    if pending and pending.stage == "AWAITING_DESTINATION":
        if current_text.strip().casefold() in {"cancel", "never mind", "nevermind", "no thanks"}:
            message = AIMessage(content="No problem. I won't open a workspace.")
            attach_navigation_task(message, "CANCELLED", {"destination_query": pending.state.get("destination_query", "")})
            return message
        workspace = (
            resolve_workspace_target(current_text)
            or resolve_workspace_request(current_text, messages=messages)
            or resolve_workspace_request(f"open {current_text}", messages=messages)
        )
        if workspace:
            # The customer is answering the exact pending destination question.
            # Resolve and execute without asking for a redundant confirmation.
            return _workspace_navigation_step(f"open {workspace}", state, continuation=True)
        # An unmatched short reply may answer a different semantic task (for
        # example an employer name or a transaction period). Do not trap it in
        # a stale navigation clarification. The LLM receives the conversation
        # excerpt and can resolve it against the active customer intent.
        return None

    decision = resolve_pending_navigation_confirmation(messages, current_text, copilot_action_names(state))
    if not decision:
        return None
    workspace = str(decision.get("workspace", ""))
    navigation_epoch = int(decision.get("navigation_epoch", 0) or 0)
    if decision["kind"] == "EXECUTE":
        arguments = {
            "workspace": workspace,
            "action_id": str(decision.get("action_id") or f"nav-{uuid.uuid4().hex}"),
            "navigation_epoch": navigation_epoch,
        }
        try:
            validate_capability_input("frontend.workspace.navigate", arguments)
        except ContractViolation:
            decision = {**decision, "kind": "UNAVAILABLE"}
        else:
            message = AIMessage(content="", tool_calls=[{
                "name": str(decision["action_name"]),
                "args": arguments,
                "id": str(decision["action_id"]),
                "type": "tool_call",
            }])
            attach_navigation_task(message, "NAVIGATION_REQUESTED", {"workspace": workspace, "action_id": arguments["action_id"]})
            return message
    if decision["kind"] == "CANCELLED":
        message = AIMessage(content=f"No problem. I won't open {workspace}.")
        attach_navigation_task(message, "CANCELLED", {"workspace": workspace})
        return message
    message = AIMessage(content=f"I found {workspace}, but the navigation action is not available in this view. You can open it from the sidebar.")
    attach_navigation_task(message, "UNAVAILABLE", {"workspace": workspace})
    return message


def _explicit_navigation_request(text: str) -> bool:
    """Guard UI navigation against answering a location question with an action."""
    value = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    if re.search(r"\bshow me\s+(?:where|how|why)\b", value):
        return False
    # The registered resource-view resolver accepts direct commands such as
    # "show my contacts" and "list saved beneficiaries". Treat those as
    # explicit navigation rather than asking for a redundant confirmation.
    if is_explicit_workspace_view_request(text):
        return True
    return bool(re.search(
        r"\b(?:open|go to|let(?:'s|s) go to|navigate to|switch to|take me to|take me there|bring me to|show me)\b",
        value,
    ))


def _sanitize_customer_response(response: AIMessage) -> AIMessage:
    """Remove internal registry identifiers from ordinary assistant prose."""
    if getattr(response, "tool_calls", None):
        return response
    content = getattr(response, "content", "")
    if not isinstance(content, str) or not content:
        return response
    cleaned = sanitize_customer_text(content)
    if cleaned != content:
        # Pydantic's model_copy preserves response metadata, IDs and usage data;
        # rebuilding a new AIMessage here would silently discard them.
        response = response.model_copy(update={"content": cleaned})
    return response


def _workspace_navigation_step(
    text: str, state: ORAIState, *, continuation: bool = False,
) -> AIMessage | None:
    """Route an explicit workspace objective using the registered page map."""
    workspace = resolve_workspace_request(text, messages=state.get("messages", []))
    if not workspace:
        if not is_incomplete_navigation_request(text):
            return None
        pages = get_registry().get("application_policy", {}).get("navigation", {}).get("pages", [])
        available = ", ".join(str(item) for item in pages)
        message = AIMessage(content=f"Which ORAIflow workspace would you like me to open? Available workspaces: {available}.")
        attach_navigation_task(message, "AWAITING_DESTINATION", {"destination_query": str(text or "")})
        return message
    action_name = str(get_registry().get("capabilities", {}).get("frontend.workspace.navigate", {}).get("name", ""))
    if not action_name or action_name not in copilot_action_names(state):
        return AIMessage(content=f"The {workspace} workspace is not available in this view. You can open it from the sidebar.")
    messages = state.get("messages", [])
    if navigation_action_already_emitted(messages, workspace):
        logger.info("workspace_navigation_replay_suppressed trace_id=%s workspace=%s", current_trace_id(), workspace)
        return AIMessage(content="", additional_kwargs={"oraiflow_navigation_replay_suppressed": True})
    thread_id = str(state.get("thread_id") or state.get("threadId") or state.get("conversation_id") or "")
    action_id = navigation_action_id(messages, workspace, thread_id)
    context = _selected_account_context(state)
    navigation_epoch = int(context.get("navigation_epoch", 0) or 0)
    arguments = {"workspace": workspace, "action_id": action_id, "navigation_epoch": navigation_epoch}
    try:
        validate_capability_input("frontend.workspace.navigate", arguments)
    except ContractViolation:
        logger.warning("workspace_navigation_rejected trace_id=%s reason=INPUT_CONTRACT", current_trace_id())
        return AIMessage(content="I couldn't validate that workspace destination. Please choose a workspace from the sidebar.")
    if not _explicit_navigation_request(text) and not continuation:
        message = AIMessage(content=f"I found **{workspace}**. Would you like me to open that workspace?")
        attach_navigation_task(message, "AWAITING_CONFIRMATION", {
            "workspace": workspace, "action_id": action_id, "navigation_epoch": navigation_epoch,
            "action": "frontend.workspace.navigate",
        })
        return message
    message = AIMessage(content="", tool_calls=[{
        "name": action_name, "args": arguments, "id": action_id, "type": "tool_call",
    }])
    attach_navigation_task(message, "NAVIGATION_REQUESTED", {
        "workspace": workspace, "action_id": action_id, "navigation_epoch": navigation_epoch,
    })
    return message


def _frontend_action_acknowledgement(state: ORAIState) -> AIMessage | None:
    """Acknowledge one browser action receipt without asking the model again."""
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    if message_type(last) != "tool":
        return None
    tool_name = str(message_value(last, "name", "") or "")
    registered = capability_by_name(tool_name, kind="frontend")
    if not registered:
        return None
    capability_id = registered[0]
    if capability_id == "frontend.transfer.prepare_draft":
        # Normally _handle_transfer_workflow() acknowledges this receipt using
        # its persisted workflow state. This verified fallback covers a browser
        # ToolMessage rehydrated without that metadata; it prevents the raw
        # successful draft response from falling through to the generic scope
        # response protocol.
        context = _selected_account_context(state)
        transfer_result = transfer_draft_receipt_message(
            messages, source_accounts=context.get("accounts", [])
        )
        if transfer_result is None:
            return None
        reply, receipt = transfer_result
        workflow = latest_pending_workflow(messages)
        if workflow and receipt.get("status") == "draft_opened":
            workflow = apply_frontend_draft_result(workflow, {
                "status": "draft_opened",
                "workflow_id": receipt.get("workflow_id"),
                "action_id": receipt.get("action_id"),
            })
        message = (
            _frontend_action_ack_message(reply, receipt, workflow)
            if receipt.get("status") == "draft_opened"
            else AIMessage(content=reply, additional_kwargs={"oraiflow_frontend_action_receipt": receipt})
        )
        if workflow and workflow.get("stage") not in {"CANCELLED", "CONTACT_NOT_FOUND", "CONTACT_SEARCH_FAILED", "DRAFT_REJECTED", "DISMISSED", "DRAFT_OPENED", "COMPLETED"}:
            with_workflow(message, workflow)
        logger.info(
            "transfer_draft_receipt_acknowledged trace_id=%s status=%s workflow_state_present=%s",
            current_trace_id(), receipt.get("status", ""), bool(workflow),
        )
        return message
    try:
        envelope = parse_result_envelope(capability_id, str(message_value(last, "content", "")))
    except (TypeError, ValueError, ContractViolation):
        logger.warning("frontend_action_ack_rejected trace_id=%s capability=%s reason=INVALID_RECEIPT", current_trace_id(), capability_id)
        return AIMessage(content="I couldn't verify that the application action completed. Please check the current screen.")

    outcome = str(envelope.get("outcome", ""))
    data = envelope.get("data") if outcome == "SUCCESS" and isinstance(envelope.get("data"), dict) else {}
    status = str(data.get("status", ""))
    expected_status = str(get_capability(capability_id).get("result_semantics", {}).get("success_status", ""))
    if outcome == "SUCCESS" and (not expected_status or status != expected_status):
        reply = "I couldn't verify that the application completed the requested action. Please check the current screen before trying again."
    elif outcome == "REJECTED":
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        reply = str(error.get("message") or "The application rejected that action. Please try again.")
    elif outcome in {"UNAVAILABLE", "INVALID_RESPONSE", "INVALID_INPUT", "EMPTY", "PARTIAL"} or outcome != "SUCCESS":
        reply = "I couldn't complete that application action just now. Please try again."
    elif capability_id == "frontend.workspace.navigate" and status == expected_status:
        destination = str(data.get("workspace") or "the requested")
        reply = f"I've opened the {destination} workspace for you."
    elif capability_id == "frontend.account.select" and status == expected_status:
        reply = "Done — Activity is now showing the selected account."
    elif capability_id == "frontend.account.clear_selection" and status == expected_status:
        reply = "Done — Activity now shows transactions across all accounts."
    else:
        reply = "The application confirmed the action, but I couldn't identify the resulting view. Please check the screen."

    receipt = {
        "capability_id": capability_id,
        "tool_call_id": str(message_value(last, "tool_call_id", "") or ""),
        "status": status or outcome,
    }
    if outcome == "SUCCESS" and status.lower() in {"opened", "selected", "cleared"}:
        message = _frontend_action_ack_message(reply, receipt, None)
    else:
        message = AIMessage(content=reply, additional_kwargs={"oraiflow_frontend_action_receipt": receipt})
    logger.info("frontend_action_acknowledged trace_id=%s capability=%s outcome=%s status=%s", current_trace_id(), capability_id, outcome, status)
    return message


def _customer_assessment_message(decision: dict[str, Any], assessment: InterpretationAssessment | None) -> AIMessage:
    code = str(decision.get("code", "ASSESSMENT_REJECTED"))
    logger.warning(
        "interpretation_gate trace_id=%s decision=%s code=%s dimensions=%s issues=%s transport=%s detail_type=%s",
        current_trace_id(), decision.get("decision", "REJECT"), code,
        ",".join(decision.get("dimensions", []) or decision.get("fields", [])),
        ",".join(decision.get("issues", []) or []),
        str(decision.get("transport", ""))[:80],
        str(decision.get("detail", ""))[:80],
    )
    if decision.get("decision") == "ABSTAIN":
        return AIMessage(content="I’m not confident I understand the banking request. Could you describe what you’d like to do?")
    if assessment and any(task.intent_id == "TRANSACTION_ANALYSIS" for task in assessment.tasks):
        # A model-authored clarification can echo the OCCF candidate description.
        # Keep transaction-analysis failure copy runtime-owned in both the
        # empty-task and populated-task validation paths.
        return AIMessage(content=safe_customer_clarification(
            assessment.clarification_question,
            [{"intent_id": "TRANSACTION_ANALYSIS"}],
        ))
    if assessment and assessment.clarification_question:
        return AIMessage(content=assessment.clarification_question)
    if code == "CONFIDENCE_BELOW_THRESHOLD":
        dimensions = set(decision.get("dimensions", []))
        if "entity" in dimensions:
            return AIMessage(content="I’m not certain which person, account or transaction you mean. Could you clarify that detail?")
        if "slot" in dimensions:
            return AIMessage(content="I understand what you want to do, but I’m missing or uncertain about one of the details. Could you clarify it?")
    fields = {str(item).casefold().replace("slot:", "").replace("entity:", "") for item in decision.get("fields", [])}
    if code == "VALUE_EVIDENCE_MISSING":
        financial_fields = fields & {"amount", "currency", "beneficiary", "recipient"}
        if financial_fields == {"currency"}:
            return AIMessage(content="I understood the amount and recipient. Which currency should I use? No transfer draft was opened.")
        if financial_fields == {"amount"}:
            return AIMessage(content="I found the recipient, but I still need the transfer amount. How much would you like to send? No transfer draft was opened.")
        if financial_fields in ({"beneficiary"}, {"recipient"}):
            return AIMessage(content="I understood the amount, but I still need the recipient. Who would you like to send it to? No transfer draft was opened.")
        if financial_fields:
            missing = []
            if "amount" in financial_fields:
                missing.append("amount")
            if "currency" in financial_fields:
                missing.append("currency")
            if financial_fields & {"beneficiary", "recipient"}:
                missing.append("recipient")
            if len(missing) == 1:
                question = f"Please provide the missing {missing[0]}."
            elif len(missing) == 2:
                question = f"Please provide the missing {missing[0]} and {missing[1]}."
            else:
                question = "Please provide the missing amount, currency and recipient."
            return AIMessage(content=f"I understood part of your transfer request. {question} No transfer draft was opened.")
        if fields & {"workspace", "workspace_name", "navigation_target"}:
            return AIMessage(content="I can help navigate, but I couldn't identify the destination from the conversation. Which workspace should I open?")
        return AIMessage(content="I understood what you're referring to, but I couldn't match that detail to the available information. Could you clarify which item you mean?")
    if code in {"EVIDENCE_NOT_IN_SOURCE", "CUSTOMER_INSTRUCTION_EVIDENCE_MISSING"}:
        return AIMessage(content="I couldn't verify the context needed for that action. Could you tell me what you'd like me to do, in your own words?")
    if code == "INTENT_CAPABILITY_MISMATCH":
        return AIMessage(content="I couldn't identify a safe operation for that request. Could you describe the outcome you're looking for?")
    if code == "UNKNOWN_INTENT" or code == "UNKNOWN_CAPABILITY":
        return AIMessage(content="I couldn't match that request to an available banking or workspace action. Could you clarify what you'd like to accomplish?")
    if code == "CAPABILITY_ASSESSMENT_MISMATCH":
        return AIMessage(content="I need to double-check which operation is appropriate before proceeding. Could you describe the request once more?")
    if code == "MODEL_REQUESTED_CLARIFICATION":
        return AIMessage(content="Could you clarify what you’d like me to do?")
    if code == "ASSESSMENT_SCHEMA_INVALID":
        return AIMessage(content="I understood that you want me to take an action, but some interpretation details were incomplete. Nothing was executed. Could you clarify the specific detail you want me to use?")
    if code == "CONFIDENCE_MISSING":
        return AIMessage(content="I need to check my understanding before proceeding. Could you clarify the intended outcome?")
    return AIMessage(content="I couldn't validate the action safely. Nothing was executed. Could you clarify what you'd like me to do?")


def _validate_proposed_tool_calls(
    response: AIMessage, messages=None, context: dict[str, Any] | None = None,
) -> AIMessage:
    """Revalidate runtime-constructed calls through the established OCCF/security gates.

    Kept as the common final validator for canonical, interpreted and workflow
    continuations; the interpretation model no longer proposes executable calls.
    """
    transfer_guard = _validate_transfer_draft_has_unique_contact(response, messages or [], context)
    if transfer_guard is not None:
        return transfer_guard
    calls = getattr(response, "tool_calls", None) or []
    if not calls:
        return response
    messages = messages or []
    context = context if isinstance(context, dict) else {}
    current_text = latest_human_text(messages)
    sources = assessment_sources(messages, current_text, context)
    workflow = None
    for call in calls:
        if not isinstance(call, dict):
            return AIMessage(content="I couldn’t safely process that application action. Please rephrase your request.")
        name = call.get("name", "")
        raw_args = call.get("args", {})
        if not isinstance(raw_args, dict):
            return AIMessage(content="I couldn’t safely process that application action because its parameters were invalid.")
        if name == "prepare_transfer_draft":
            # Draft opening is a runtime-owned continuation after candidate and
            # account validation, never a model-authorized operation.
            return AIMessage(content="I need to verify the saved recipient and source account before opening a transfer draft. No draft has been opened.")
        registered = capability_by_name(name)
        if registered:
            capability_id, capability = registered
            if capability.get("authorization", {}).get("agent_invocable") is not True:
                return AIMessage(content="I couldn’t safely process that application action because it is not available to the assistant.")
            if capability.get("kind") == "mcp":
                pending_workflow = latest_pending_workflow(messages)
                inferred_intent = unique_intent_for_capability(capability_id)
                if (capability_id == "banking.accounts.list" and pending_workflow
                        and pending_workflow.get("workflow_type") == "TRANSFER_PREPARATION"
                        and pending_workflow.get("stage") in {"AWAITING_SOURCE_ACCOUNT", "RESOLVING_SOURCE_ACCOUNT"}):
                    inferred_intent = "TRANSFER_PREPARATION"
                raw_assessment, transport_info = assessment_from_tool_arguments(
                    raw_args, expected_capability_id=capability_id,
                    inferred_intent_id=inferred_intent,
                )
                contract_args = strip_assessment_tool_arguments(raw_args)
                if isinstance(raw_assessment, dict):
                    # The registered tool call is itself the selected capability.
                    # The search query is its typed representation of the
                    # beneficiary wording; copy it into the assessment only so
                    # the common evidence gate can validate the same value.
                    if capability_id == "banking.contacts.search":
                        proposed_name = str(contract_args.get("name_query", "")).strip()
                        is_standalone_lookup = raw_assessment.get("intent_id") == "SAVED_CONTACT_LOOKUP"
                        entity_field = "contact_name" if is_standalone_lookup else "beneficiary"
                        entities = raw_assessment.setdefault("entities", {})
                        if not entities.get(entity_field) and proposed_name and proposed_name.casefold() in current_text.casefold():
                            entities[entity_field] = proposed_name
                            evidence = raw_assessment.setdefault("evidence", [])
                            if isinstance(evidence, list):
                                evidence.append({"field": entity_field, "source": "current_turn", "quote": proposed_name})
                        # Recover amount/currency only for a transfer intent and
                        # only when those values are explicitly present in this
                        # same customer turn. Standalone contact lookup never
                        # inherits transfer fields or opens a transfer workflow.
                        if not is_standalone_lookup:
                            recovered_fields = reconcile_explicit_transfer_slots(
                                raw_assessment,
                                current_customer_text=current_text,
                                proposed_recipient_query=proposed_name,
                                context=context,
                            )
                            if recovered_fields:
                                logger.info(
                                    "transfer_explicit_slots_reconciled trace_id=%s fields=%s source=current_turn",
                                    current_trace_id(), ",".join(recovered_fields),
                                )
                assessment, decision = validate_interpretation_assessment(
                    raw_assessment,
                    expected_capability_id=capability_id,
                    sources=sources,
                    applicable_confidence_dimensions={"intent", "slot", "entity"},
                )
                if decision.get("decision") != "PASS":
                    decision.setdefault("transport", transport_info.get("code"))
                    return _customer_assessment_message(decision, assessment)
                try:
                    validate_capability_input(capability_id, contract_args)
                except ContractViolation:
                    logger.warning(
                        "capability_input_rejected trace_id=%s capability=%s source=agent_proposal",
                        current_trace_id(), capability_id,
                    )
                    return AIMessage(content="I couldn’t safely process that request because some required information was missing or invalid.")
                if (capability_id == "banking.accounts.list"
                        and pending_workflow
                        and pending_workflow.get("stage") == "AWAITING_SOURCE_ACCOUNT"
                        and assessment.intent_id == "TRANSFER_PREPARATION"):
                    preference = assessment.slots.get("source_account_preference")
                    if preference not in {"highest_balance", "lowest_balance"}:
                        return AIMessage(content="I understand you want me to choose the source account by a preference, but I couldn’t validate which comparison you meant. Please say highest or lowest balance.")
                    workflow = dict(pending_workflow)
                    workflow["source_account_preference"] = preference
                    workflow["account_resolution_reason"] = (
                        "NEEDS_VERIFIED_BALANCE_MAX" if preference == "highest_balance"
                        else "NEEDS_VERIFIED_BALANCE_MIN"
                    )
                    workflow["stage"] = "RESOLVING_SOURCE_ACCOUNT"
                if capability_id == "banking.contacts.search":
                    query = str(contract_args.get("name_query", "")).strip()
                    if assessment.intent_id == "SAVED_CONTACT_LOOKUP":
                        contact_name = assessment.entities.get("contact_name") or assessment.slots.get("contact_name")
                        if not contact_name or _normalise_contract_name(query) != _normalise_contract_name(contact_name):
                            return AIMessage(content="I couldn’t match the contact search to the name in your request. Please provide the saved contact’s name again.")
                        # A saved-contact lookup is a standalone read-only query.
                        # It intentionally does not construct TRANSFER_PREPARATION
                        # state or require amount/currency/source-account fields.
                    else:
                        beneficiary = (
                            assessment.entities.get("beneficiary")
                            or assessment.entities.get("recipient")
                            or assessment.slots.get("beneficiary")
                            or assessment.slots.get("recipient")
                        )
                        if not beneficiary or _normalise_contract_name(query) != _normalise_contract_name(beneficiary):
                            return AIMessage(content="I couldn’t match the recipient search to the name in your request. Please provide the saved contact’s name again.")
                        if assessment.intent_id != "TRANSFER_PREPARATION":
                            return AIMessage(content="I couldn’t validate the purpose of that recipient search. Please restate the transfer request.")
                        workflow = build_transfer_workflow(assessment.model_dump(), context)
                        if not workflow.get("beneficiary_query") or not workflow.get("amount"):
                            return AIMessage(content="Please tell me the amount and the saved recipient you want to transfer to.")
                logger.info(
                    "interpretation_assessment_validated trace_id=%s intent=%s capability=%s mode=%s deferred=%s missing=%s scores=%s evidence_anchor=%s",
                    current_trace_id(), assessment.intent_id, capability_id,
                    decision.get("confidence_mode", "observe"), decision.get("deferred_dimensions", []),
                    decision.get("missing_dimensions", []), decision.get("scores", {}),
                    decision.get("evidence_anchor", {}),
                )
                continue
            inferred_intent = unique_intent_for_capability(capability_id)
            raw_assessment, transport_info = assessment_from_tool_arguments(
                raw_args, expected_capability_id=capability_id,
                inferred_intent_id=inferred_intent,
            )
            contract_args = strip_assessment_tool_arguments(raw_args)
            if isinstance(raw_assessment, dict):
                if capability_id == "frontend.workspace.navigate":
                    if not (raw_assessment.get("entities") or {}).get("workspace") and contract_args.get("workspace"):
                        raw_assessment.setdefault("entities", {})["workspace"] = str(contract_args["workspace"])
                elif capability_id == "frontend.account.select":
                    account_id = str(contract_args.get("account_id", ""))
                    account_record = next((item for item in context.get("accounts", [])
                                           if isinstance(item, dict) and str(item.get("account_id", "")) == account_id), None)
                    if account_record:
                        entities = raw_assessment.setdefault("entities", {})
                        entities.setdefault("account_id", account_id)
                        if account_record.get("account_name"):
                            entities.setdefault("account_name", str(account_record["account_name"]))
            assessment, decision = validate_interpretation_assessment(
                raw_assessment,
                expected_capability_id=capability_id,
                sources=sources,
                applicable_confidence_dimensions={"intent", "slot", "entity"},
            )
            if decision.get("decision") != "PASS":
                decision.setdefault("transport", transport_info.get("code"))
                return _customer_assessment_message(decision, assessment)
            if capability_id == "frontend.workspace.navigate":
                # The identifier is runtime-owned. Never rely on a model to invent
                # an idempotency key for a UI side effect.
                contract_args["action_id"] = f"nav-{uuid.uuid4().hex}"
                contract_args["navigation_epoch"] = max(0, int(context.get("navigation_epoch", 0) or 0))
            try:
                validate_capability_input(capability_id, contract_args)
            except ContractViolation:
                return AIMessage(content="I couldn’t safely process that application action because its parameters were invalid.")
            if capability_id == "frontend.workspace.navigate":
                allowed_pages = set(get_registry().get("application_policy", {}).get("navigation", {}).get("pages", []))
                workspace = str(contract_args.get("workspace", ""))
                assessed_workspace = str(
                    assessment.entities.get("workspace")
                    or assessment.entities.get("workspace_name")
                    or assessment.slots.get("workspace")
                    or assessment.slots.get("workspace_name")
                    or ""
                )
                if workspace not in allowed_pages or assessed_workspace != workspace:
                    return AIMessage(content="I can only open a workspace when its destination matches an available workspace. Which workspace would you like?")
                if not _explicit_navigation_request(current_text):
                    message = AIMessage(content=f"I found **{workspace}**. Would you like me to open that workspace?")
                    attach_navigation_task(message, "AWAITING_CONFIRMATION", {
                        "workspace": workspace,
                        "action_id": contract_args["action_id"],
                        "navigation_epoch": contract_args["navigation_epoch"],
                        "action": "frontend.workspace.navigate",
                    })
                    return message
            elif capability_id == "frontend.account.select":
                account_id = str(contract_args.get("account_id", ""))
                visible_accounts = context.get("accounts", [])
                if not any(isinstance(item, dict) and str(item.get("account_id", "")) == account_id for item in visible_accounts):
                    return AIMessage(content="I couldn't match that account to an account available in your current workspace. Please choose one of your listed accounts.")
            # CopilotKit receives only its declared browser arguments. The
            # assessment is an Agent Runtime contract, not a frontend input.
            call["args"] = contract_args
            logger.info(
                "interpretation_assessment_validated trace_id=%s intent=%s capability=%s mode=%s missing=%s scores=%s evidence_anchor=%s",
                current_trace_id(), assessment.intent_id, capability_id,
                decision.get("confidence_mode", "observe"), decision.get("missing_dimensions", []),
                decision.get("scores", {}), decision.get("evidence_anchor", {}),
            )
            continue
        if name in get_registry().get("presentations", {}):
            # Model-provided props are discarded and rebound to verified connector data.
            continue
        return AIMessage(content="I couldn’t safely process that application action because it isn’t a supported capability.")
    if workflow is not None:
        with_workflow(response, workflow)
    return response


def _workflow_message(
    content: str,
    workflow: dict[str, Any],
    *,
    additional_kwargs: dict[str, Any] | None = None,
    action_ack: bool = False,
    action_id: str | None = None,
) -> AIMessage:
    message_kwargs: dict[str, Any] = {"additional_kwargs": dict(additional_kwargs or {})}
    if action_ack:
        stable_action_id = str(action_id or workflow.get("action_id") or workflow.get("workflow_id") or uuid.uuid4().hex)
        message_kwargs.update({
            "name": ACTION_ACK_NAME,
            "id": f"{ACTION_ACK_ID_PREFIX}{stable_action_id}",
        })
    message = AIMessage(content=content, **message_kwargs)
    with_workflow(message, workflow)
    return message


def _frontend_action_ack_message(
    content: str, receipt: dict[str, Any], workflow: dict[str, Any] | None = None,
) -> AIMessage:
    """Create a durable, recognisable final response for one verified UI action."""
    action_id = str(
        receipt.get("action_id") or receipt.get("tool_call_id")
        or receipt.get("workflow_id") or uuid.uuid4().hex
    )
    message = AIMessage(
        content=content,
        name=ACTION_ACK_NAME,
        id=f"{ACTION_ACK_ID_PREFIX}{action_id}",
        additional_kwargs={"oraiflow_frontend_action_receipt": dict(receipt)},
    )
    if workflow is not None:
        with_workflow(message, workflow)
    return message


def _verified_tool_data(messages, capability_id: str) -> tuple[bool, list[dict[str, Any]] | None]:
    seen, envelope = _capability_result_for_turn(messages or [], capability_id)
    if not seen:
        return False, None
    if envelope is None:
        return True, None
    data = envelope.get("data")
    if not isinstance(data, list):
        return True, None
    return True, [item for item in data if isinstance(item, dict)]


def _verified_tool_data_diagnostic(
    messages, capability_id: str,
) -> tuple[bool, list[dict[str, Any]] | None, str | None, str | None, str | None]:
    """Runtime wrapper retaining the current trace identifier."""
    return verified_list_result_diagnostic(
        list(messages or []), capability_id, trace_id=current_trace_id(),
    )


def _internal_read_tool_call(tool_name: str, workflow: dict[str, Any]) -> AIMessage:
    call_id = f"oraiflow-workflow-read-{uuid.uuid4().hex}"
    # The frontend already renders live tool activity for this read. Keep the
    # assistant conversation reserved for clarifications and the one terminal
    # draft acknowledgement, rather than emitting duplicate progress prose.
    message = AIMessage(content="", tool_calls=[{
        "name": tool_name,
        "args": {},
        "id": call_id,
        "type": "tool_call",
    }])
    message.additional_kwargs["oraiflow_runtime_owned_read_ids"] = [call_id]
    with_workflow(message, workflow)
    return message


def _academy_personal_data_read_step(state: ORAIState) -> AIMessage | None:
    """Fetch a registered personal-data capability before personalized Academy guidance.

    Academy catalogue metadata maps a topic to its required intent. The read is
    runtime-owned, read-only, and still passes the common OCCF input/result
    validation path. It is performed at most once per customer turn.
    """
    messages = list(state.get("messages", []))
    current_text = latest_human_text(messages)
    intent_id = academy_personal_snapshot_intent(current_text)
    if not intent_id:
        return None
    intent = get_registry().get("intents", {}).get(intent_id, {})
    capability_id = intent.get("execution_plan", {}).get("capability")
    if not capability_id:
        logger.error("academy_personal_read_missing_plan trace_id=%s intent=%s", current_trace_id(), intent_id)
        return AIMessage(content="I couldn't retrieve the current financial information needed for that explanation. Please try again.")
    seen, result = _capability_result_for_turn(messages, capability_id)
    if seen:
        if intent_id == "LOAN_INFORMATION":
            if result is None:
                return AIMessage(content="I couldn't retrieve a complete, verified loan schedule just now. Please try again or open Loans to review the available record.")
            outcome = str(result.get("outcome", ""))
            data = result.get("data")
            if outcome == "EMPTY" or not data:
                return AIMessage(content="I couldn't find an illustrative loan record in your ORAIflow profile. I can still explain principal, interest and APR generally, but I won't invent loan terms or instalment dates.")
            if outcome != "SUCCESS" or not isinstance(data, dict):
                return AIMessage(content="The loan information was incomplete, so I won't present it as a complete repayment schedule. Please review Loans or try again.")
            return AIMessage(content=render_loan_schedule_explanation(data))
        if intent_id == "FINANCIAL_HEALTH_EXPLANATION":
            if result is None:
                return AIMessage(content="I couldn't validate the current Financial Health snapshot, so I won't guess at your ratios. Please try again or open Overview.")
            outcome = str(result.get("outcome", ""))
            data = result.get("data")
            if outcome not in {"SUCCESS", "PARTIAL"} or not isinstance(data, dict):
                return AIMessage(content="I couldn't retrieve a complete Financial Health snapshot. I can explain the general concepts, but I won't invent your personal ratios.")
            return AIMessage(content=render_financial_health_explanation(data, current_text))
        return None
    capability = get_capability(capability_id)
    if capability.get("kind") != "mcp" or capability.get("authorization", {}).get("agent_invocable") is not True:
        logger.error("academy_personal_read_not_authorized trace_id=%s intent=%s capability=%s", current_trace_id(), intent_id, capability_id)
        return AIMessage(content="I couldn't retrieve the current financial information needed for that explanation. Please try again.")
    try:
        validate_capability_input(capability_id, {})
    except ContractViolation:
        logger.exception("academy_personal_read_invalid_plan trace_id=%s capability=%s", current_trace_id(), capability_id)
        return AIMessage(content="I couldn't retrieve the current financial information needed for that explanation. Please try again.")
    call_id = f"oraiflow-academy-read-{uuid.uuid4().hex}"
    progress = (
        "Checking your recorded loan information so I can explain the repayment details..."
        if intent_id == "LOAN_INFORMATION"
        else "Retrieving your current recorded Financial Health information..."
    )
    message = AIMessage(content=progress, tool_calls=[{
        "name": capability["name"],
        "args": {},
        "id": call_id,
        "type": "tool_call",
    }])
    message.additional_kwargs["oraiflow_runtime_owned_read_ids"] = [call_id]
    message.additional_kwargs["oraiflow_academy_intent_id"] = intent_id
    logger.info("academy_personal_read_scheduled trace_id=%s intent=%s capability=%s", current_trace_id(), intent_id, capability_id)
    return message


def _prepare_transfer_draft_action(
    workflow: dict[str, Any], context: dict[str, Any], actions: set[str],
    *, verified_accounts: list[dict[str, Any]] | None = None,
) -> AIMessage:
    contact = workflow.get("contact") or {}
    contact_id = str(contact.get("contact_id", ""))
    contact_context = next((item for item in context.get("contacts", [])
                            if item.get("contact_id") == contact_id and item.get("status") == "ACTIVE"), None)
    account_id = str(workflow.get("source_account_id", ""))
    source_context = next((item for item in (verified_accounts or [])
                           if item.get("account_id") == account_id), None)
    currency = str(workflow.get("currency", "")).upper()
    if "prepare_transfer_draft" not in actions:
        workflow = dict(workflow)
        workflow["stage"] = "DRAFT_REJECTED"
        return _workflow_message("The transfer form isn’t available in this conversation. No draft was opened and no payment was submitted.", workflow)
    if not contact_context or _normalise_contract_name(contact_context.get("full_name")) != _normalise_contract_name(contact.get("full_name")):
        workflow = dict(workflow)
        workflow["stage"] = "DRAFT_REJECTED"
        return _workflow_message("I couldn’t match the saved recipient to the current Contacts list. Please refresh Contacts and try again. No draft was opened.", workflow)
    if (not source_context
            or str(source_context.get("currency", "")).upper() != currency
            or str(source_context.get("status", "ACTIVE")).upper() != "ACTIVE"):
        workflow = dict(workflow)
        workflow["stage"] = "DRAFT_REJECTED"
        return _workflow_message("I couldn’t validate the source account for this currency. No draft was opened.", workflow)
    if str(contact.get("currency", "")).upper() != currency:
        workflow = dict(workflow)
        workflow["stage"] = "DRAFT_REJECTED"
        return _workflow_message("The saved recipient’s account currency does not match the transfer currency. No draft was opened.", workflow)
    marked = mark_draft_requested(workflow)
    arguments = {
        "beneficiary": str(contact.get("full_name", "")),
        "beneficiary_contact_id": contact_id,
        "amount": str(workflow.get("amount", "")),
        "currency": currency,
        "from_account_id": account_id,
        "workflow_id": str(marked.get("workflow_id", "")),
        "action_id": str(marked.get("action_id", "")),
    }
    try:
        validate_capability_input("frontend.transfer.prepare_draft", arguments)
    except ContractViolation:
        marked["stage"] = "DRAFT_REJECTED"
        logger.warning("transfer_draft_action_rejected trace_id=%s reason=INPUT_CONTRACT", current_trace_id())
        return _workflow_message("I couldn’t safely prepare this transfer draft. No draft was opened.", marked)
    message = AIMessage(
        content="",
        tool_calls=[{
        "name": "prepare_transfer_draft",
        "args": arguments,
        "id": str(marked["action_id"]),
        "type": "tool_call",
    }])
    with_workflow(message, marked)
    return message


def _start_explicit_transfer_workflow(state: ORAIState, context: dict[str, Any]) -> AIMessage | None:
    """Start an explicit transfer through the one canonical typed workflow.

    The narrow amount/recipient grammar only extracts source-anchored slots. It
    does not resolve a beneficiary, choose an account, or authorize execution;
    those steps remain inside the existing authenticated workflow.
    """
    messages = list(state.get("messages", []))
    if not messages or message_type(messages[-1]) != "human":
        return None
    text = latest_human_text(messages)
    workflow = build_explicit_transfer_workflow(text, context)
    if not workflow:
        return None
    capability_id = "banking.contacts.search"
    capability = get_capability(capability_id)
    search_arguments = {"name_query": str(workflow.get("beneficiary_query", "")).strip()}
    try:
        validate_capability_input(capability_id, search_arguments)
    except ContractViolation:
        logger.warning("transfer_start_rejected trace_id=%s reason=CONTACT_QUERY_CONTRACT", current_trace_id())
        workflow["stage"] = "CONTACT_SEARCH_FAILED"
        workflow["failure_code"] = "INVALID_CONTACT_QUERY"
        return _workflow_message(
            "I understood the transfer amount, but couldn't validate the recipient search request. No draft was opened.",
            workflow,
        )
    call_id = f"oraiflow-transfer-contact-search-{uuid.uuid4().hex}"
    message = AIMessage(content="", tool_calls=[{
        "name": capability["name"],
        "args": search_arguments,
        "id": call_id,
        "type": "tool_call",
    }])
    message.additional_kwargs["oraiflow_runtime_owned_read_ids"] = [call_id]
    with_workflow(message, workflow)
    logger.info(
        "transfer_workflow_started trace_id=%s workflow_id=%s amount=%s currency=%s beneficiary_query_present=%s source_preference=%s",
        current_trace_id(), workflow.get("workflow_id"), workflow.get("amount"),
        workflow.get("currency") or "missing", bool(workflow.get("beneficiary_query")),
        workflow.get("source_account_preference") or "none",
    )
    return message


def _handle_transfer_workflow(state: ORAIState, context: dict[str, Any]) -> AIMessage | None:
    """Advance the bounded transfer workflow around verified MCP/UI results."""
    messages = state.get("messages", [])
    workflow = latest_pending_workflow(messages)
    if not workflow:
        return None
    stage = str(workflow.get("stage", ""))
    logger.info("transfer_workflow_resumed trace_id=%s workflow_type=%s stage=%s last_message_type=%s",
                current_trace_id(), workflow.get("workflow_type", ""), stage,
                getattr(messages[-1], "type", "") if messages else "none")
    terminal = {"CANCELLED", "CONTACT_NOT_FOUND", "CONTACT_SEARCH_FAILED", "DRAFT_REJECTED", "DISMISSED", "DRAFT_OPENED", "COMPLETED"}
    if stage in terminal:
        return None

    workflow = apply_frontend_dismissal(workflow, context.get("transfer_draft_state"))
    stage = str(workflow.get("stage", ""))
    last = messages[-1] if messages else None
    last_type = message_type(last)
    last_name = str(message_value(last, "name", "") or "")

    # Acknowledge the browser result exactly once. Never feed draft_opened back
    # into the language model as a reason to issue the same frontend action.
    if last_type == "tool" and last_name == "prepare_transfer_draft" and stage == "DRAFT_REQUESTED":
        raw = message_value(last, "content", "")
        try:
            envelope = parse_result_envelope("frontend.transfer.prepare_draft", raw)
            if envelope.get("outcome") == "SUCCESS" and isinstance(envelope.get("data"), dict):
                result = envelope["data"]
            else:
                result = {"status": "rejected"}
        except (TypeError, ValueError, ContractViolation):
            result = {"status": "rejected"}
        workflow = apply_frontend_draft_result(workflow, result)
        if workflow.get("stage") == "DRAFT_OPENED":
            verified_ack = transfer_draft_receipt_message(
                messages, source_accounts=context.get("accounts", [])
            )
            if verified_ack and verified_ack[1].get("status") == "draft_opened":
                acknowledgement = verified_ack[0]
                receipt = verified_ack[1]
            else:
                acknowledgement = (
                    "Your editable transfer draft is open. You can review or close it; "
                    "nothing has been previewed, approved or submitted."
                )
                # The ToolMessage and workflow/action IDs were validated above.
                # Mark the successful receipt so an AG-UI re-entry for this same
                # user turn cannot overwrite the acknowledgement with generic help.
                receipt = {
                    "capability_id": "frontend.transfer.prepare_draft",
                    "workflow_id": str(workflow.get("workflow_id", "")),
                    "action_id": str(workflow.get("action_id", "")),
                    "status": "draft_opened",
                }
            return _workflow_message(
                acknowledgement, workflow,
                additional_kwargs={"oraiflow_frontend_action_receipt": receipt},
                action_ack=True,
                action_id=str(receipt.get("action_id") or workflow.get("action_id") or ""),
            )
        return _workflow_message("I couldn’t open the transfer draft. No payment was submitted. Please check the recipient and source account and try again.", workflow)

    # If the customer has closed a draft, do not reopen it or interpret a later
    # acknowledgement as permission to submit anything.
    # A dismissal is terminal for this action, but it must not consume the
    # customer's next message. Let normal LLM conversation continue.
    # An open draft is an idempotency fact, not a conversational lock. Let the
    # agent handle unrelated reads/questions while the browser keeps the same
    # draft open. A repeated draft action remains blocked by its action receipt.

    # Continue after the authenticated Contacts search.
    if last_type == "tool" and last_name == "search_contacts" and stage == "CONTACT_SEARCH_PENDING":
        seen, candidates, diagnostic, result_outcome, error_code = _verified_tool_data_diagnostic(
            messages, "banking.contacts.search",
        )
        if not seen or candidates is None:
            failure_reason = diagnostic or "INVALID_CONTACT_RESULT"
            workflow["stage"] = "CONTACT_SEARCH_FAILED"
            workflow["failure_code"] = f"CONTACT_RESULT_{failure_reason}"
            logger.warning(
                "transfer_contact_search_not_verified trace_id=%s reason=%s outcome=%s error_code=%s",
                current_trace_id(), failure_reason, result_outcome or "unknown", error_code or "unknown",
            )
            if result_outcome == "UNAVAILABLE":
                customer_message = (
                    "The saved-contacts service is temporarily unavailable. I haven't selected a recipient "
                    "or opened a transfer draft. Please try again shortly."
                )
            elif result_outcome == "REJECTED" and error_code == "CAPABILITY_NOT_PERMITTED":
                customer_message = (
                    "I couldn't access your saved contacts with the current permissions. No recipient was "
                    "selected and no transfer draft was opened."
                )
            elif failure_reason in {"INVALID_RESULT_ENVELOPE", "INVALID_DATA_SHAPE", "INVALID_DATA_RECORD", "INVALID_DATA_SCHEMA", "INVALID_EVIDENCE", "TRUST_CLASS_MISMATCH"} or result_outcome == "INVALID_RESPONSE":
                customer_message = (
                    "The saved-contacts service returned information I couldn't verify. No recipient was "
                    "selected and no transfer draft was opened. Please try the search again."
                )
            else:
                customer_message = (
                    "I couldn't confirm that the saved-contact search completed. No recipient was selected "
                    "and no transfer draft was opened. Please try the search again."
                )
            return _workflow_message(customer_message, workflow)
        status, workflow = resolve_search_result(workflow, candidates, context)
        if status == "CONTACT_NOT_FOUND":
            return _workflow_message(f"I couldn’t find an active saved contact matching **{workflow.get('beneficiary_query', 'that name')}**. Check Contacts for the exact saved name. No draft was opened.", workflow)
        if status == "AWAITING_CONTACT":
            return _workflow_message(contact_choice_prompt(workflow), workflow)
        if status == "AWAITING_CURRENCY":
            return _workflow_message(currency_choice_prompt(workflow), workflow)
        if status == "CURRENCY_MISMATCH":
            return _workflow_message("The selected currency does not match the saved recipient account currency. Please use the recipient's supported currency or choose another saved recipient. No draft was opened.", workflow)
        if status == "AWAITING_SOURCE_ACCOUNT":
            return _workflow_message(account_choice_prompt(workflow), workflow)
        if status == "NEEDS_VERIFIED_BALANCE":
            return _internal_read_tool_call("list_customer_accounts", workflow)
        if status in {"READY_FOR_DRAFT", "VERIFY_SOURCE_ACCOUNT"}:
            workflow["stage"] = "VERIFYING_SOURCE_ACCOUNT"
            return _internal_read_tool_call("list_customer_accounts", workflow)
        return _workflow_message("I need one more detail before preparing the transfer draft. No draft was opened.", workflow)

    # Resolve comparative preferences or verify a named/ordinal source choice
    # using a fresh authenticated MCP account read before opening a draft.
    if last_type == "tool" and last_name == "list_customer_accounts" and stage in {"RESOLVING_SOURCE_ACCOUNT", "VERIFYING_SOURCE_ACCOUNT"}:
        seen, verified_accounts = _verified_tool_data(messages, "banking.accounts.list")
        if not seen or verified_accounts is None:
            workflow["stage"] = "AWAITING_SOURCE_ACCOUNT"
            return _workflow_message("I couldn’t verify the source account just now. Please choose an account by name; no draft was opened.", workflow)
        if stage == "RESOLVING_SOURCE_ACCOUNT":
            status, workflow = apply_verified_account_ranking(workflow, context, verified_accounts)
            if status == "READY_FOR_DRAFT":
                return _prepare_transfer_draft_action(workflow, context, copilot_action_names(state), verified_accounts=verified_accounts)
            if workflow.get("account_resolution_reason") == "BALANCE_RANKING_TIED":
                return _workflow_message("Two or more eligible accounts have the same balance, so there isn’t a unique highest-balance account. Please choose the source account by name.", workflow)
            return _workflow_message(account_choice_prompt(workflow), workflow)
        status, workflow = apply_verified_source_account_selection(workflow, verified_accounts)
        if status != "READY_FOR_DRAFT":
            return _workflow_message("That account is no longer available for this transfer. Please choose one of the listed accounts again.", workflow)
        return _prepare_transfer_draft_action(workflow, context, copilot_action_names(state), verified_accounts=verified_accounts)

    # Clarification replies are resolved only against the bounded trusted
    # candidates already presented to the customer.
    latest_text = latest_human_text(messages)
    latest_user_index = max((i for i, item in enumerate(messages) if message_type(item) == "human"), default=-1)
    if latest_user_index >= 0 and latest_user_index == len(messages) - 1:
        if stage == "AWAITING_CONTACT":
            status, updated = resolve_contact_reply(workflow, latest_text, context)
        elif stage == "AWAITING_CURRENCY":
            status, updated = resolve_currency_reply(workflow, latest_text, context)
        elif stage == "AWAITING_SOURCE_ACCOUNT":
            status, updated = resolve_source_account_reply(workflow, latest_text, context)
        else:
            return None
        if status == "UNRELATED":
            logger.info("transfer_followup_unmatched trace_id=%s stage=%s", current_trace_id(), stage)
            return None
        if status == "CANCELLED":
            return _workflow_message("Understood. I cancelled this transfer preparation. No draft was opened and no payment was submitted.", updated)
        if status == "AWAITING_CONTACT":
            return _workflow_message(contact_choice_prompt(updated), updated)
        if status == "AWAITING_CURRENCY":
            return _workflow_message(currency_choice_prompt(updated), updated)
        if status == "CURRENCY_MISMATCH":
            expected = str(updated.get("proposed_currency") or "the saved recipient currency")
            return _workflow_message(f"That recipient is saved in {expected}. I can only prepare the transfer in the saved recipient's supported currency. No draft was opened.", updated)
        if status == "AWAITING_SOURCE_ACCOUNT":
            return _workflow_message(account_choice_prompt(updated), updated)
        if status == "NEEDS_VERIFIED_BALANCE":
            updated["stage"] = "RESOLVING_SOURCE_ACCOUNT"
            return _internal_read_tool_call("list_customer_accounts", updated)
        if status in {"READY_FOR_DRAFT", "VERIFY_SOURCE_ACCOUNT"}:
            updated["stage"] = "VERIFYING_SOURCE_ACCOUNT"
            return _internal_read_tool_call("list_customer_accounts", updated)
    return None


def _current_turn_task_plan(messages: list[Any] | tuple[Any, ...]) -> dict[str, Any] | None:
    """Return the latest runtime-owned task plan from the active customer turn."""
    items = list(messages or [])
    start = max((i for i, item in enumerate(items) if message_type(item) == "human"), default=-1)
    for message in reversed(items[start + 1:]):
        if message_type(message) != "ai":
            continue
        plan = message_metadata(message).get("oraiflow_task_plan")
        if isinstance(plan, dict) and isinstance(plan.get("tasks"), list):
            return plan
    return None


def _attach_task_plan(
    message: AIMessage,
    task_plan: dict[str, Any],
    call_to_tasks: dict[str, list[str]] | None = None,
) -> AIMessage:
    """Attach a traceable plan to the existing message without changing its ID."""
    metadata = copy.deepcopy(getattr(message, "additional_kwargs", {}) or {})
    # Validate internal task metadata against the shared typed contract before
    # persisting it into message/checkpoint state.
    validated_plan = TurnTaskPlan.model_validate(task_plan)
    metadata["oraiflow_task_plan"] = validated_plan.model_dump(mode="json")
    if call_to_tasks:
        metadata["oraiflow_task_call_map"] = copy.deepcopy(call_to_tasks)
    return message.model_copy(update={"additional_kwargs": metadata})


def _conversation_excerpt(messages: list[Any] | tuple[Any, ...]) -> str:
    """Build a bounded role-labelled conversation excerpt with stable message IDs."""
    lines: list[str] = []
    items = list(messages or [])
    latest_user_index = max(
        (index for index, item in enumerate(items) if message_type(item) == "human"),
        default=-1,
    )
    prior_messages = items[max(0, latest_user_index - 13):latest_user_index] if latest_user_index >= 0 else items[-14:]
    for message in prior_messages:
        role = message_type(message)
        if role not in {"human", "ai"}:
            continue
        content = message_value(message, "content", "")
        if isinstance(content, list):
            content = message_content_to_text(content)
        content = str(content or "").strip()
        if not content or message_value(message, "tool_calls", None):
            continue
        message_id = str(message_value(message, "id", "") or "")
        lines.append(f"{role.upper()} id={message_id}: {content[:1200]}")
    return "\n".join(lines)[-8000:]


def _validated_turn_results(messages: list[Any] | tuple[Any, ...]) -> list[dict[str, Any]]:
    """Collect only result envelopes validated against their registered capability."""
    items = list(messages or [])
    start = max((i for i, item in enumerate(items) if message_type(item) == "human"), default=-1)
    results: list[dict[str, Any]] = []
    for message in items[start + 1:]:
        if message_type(message) != "tool":
            continue
        tool_name = str(message_value(message, "name", "") or "")
        registered = capability_by_name(tool_name, kind="mcp") or capability_by_name(tool_name, kind="frontend")
        if not registered:
            continue
        capability_id = registered[0]
        raw = message_value(message, "content", "")
        try:
            envelope = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
            validated = validate_result_envelope(capability_id, envelope)
        except (TypeError, ValueError, ContractViolation):
            results.append({"capability_id": capability_id, "outcome": "INVALID", "data": None})
            continue
        results.append({
            "capability_id": capability_id,
            "outcome": validated.get("outcome"),
            "data": validated.get("data"),
            "error": validated.get("error"),
            "trace_id": validated.get("trace_id"),
        })
    return results


def _is_plausible_pending_semantic_reply(text: str, intent_id: str) -> bool:
    """Accept answer-like fragments, not fresh questions or independent tasks.

    Pending slots are useful for short replies such as ``November``, ``EUR``,
    ``Lucas`` or ``Everyday``. A complete question such as ``what was my recent
    salary?`` is a new task and must be interpreted on its own, even when a
    previous transfer/analysis clarification remains in the message history.
    """
    value = " ".join(str(text or "").strip().casefold().split())
    if not value:
        return False
    # Explicit discourse markers commonly introduce a direct answer to a
    # clarification (e.g. "what about November?"), not an independent task.
    continuation_prefixes = (
        "what about ", "how about ", "i meant ", "i mean ",
        "rather ", "instead ", "for ", "in ", "during ", "since ", "from ",
    )
    explicit_continuation = value.startswith(continuation_prefixes)
    if not explicit_continuation:
        if value.endswith("?"):
            return False
        if re.match(
            r"^(?:what|how|why|when|where|who|which|whose|did|does|do|is|are|was|were|can|could|would|should|show|list|find|open|compare|calculate|tell me)\b",
            value,
        ):
            return False
        # Long declarative sentences are not slot answers. Allow short phrases
        # because customer contact/account names can contain several words.
        if len(value.split()) > 8:
            return False
    # A domain-incompatible answer should not keep a task alive. This guard is
    # deliberately conservative: it only excludes unmistakable fresh banking
    # questions/actions; the model still validates the actual slot evidence.
    if intent_id == "TRANSFER_PREPARATION" and re.search(
        r"\b(?:salary|payroll|income|spending|spent|coffee|transactions|recent activity|balance|contacts)\b", value,
    ) and not explicit_continuation:
        return False
    if intent_id in {"TRANSACTION_ANALYSIS", "SAVED_CONTACT_LOOKUP"} and re.search(
        r"\b(?:send|transfer|pay|make a payment|open contacts|navigate to)\b", value,
    ) and not explicit_continuation:
        return False
    return True


def _pending_semantic_tasks(
    messages: list[Any] | tuple[Any, ...], current_text: str | None = None,
) -> list[dict[str, Any]]:
    """Return only a still-relevant unresolved task for an answer-like reply.

    A newer independent question/action supersedes an older clarification. This
    prevents a pending transfer from hijacking a salary or spending question.
    """
    items = list(messages or [])
    latest_user_index = max(
        (index for index, item in enumerate(items) if message_type(item) == "human"),
        default=-1,
    )
    if latest_user_index < 0:
        return []
    previous_assistant = next(
        (item for item in reversed(items[:latest_user_index]) if message_type(item) == "ai"),
        None,
    )
    if previous_assistant is None:
        return []
    metadata = message_metadata(previous_assistant)
    plan = metadata.get("oraiflow_task_plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
        return []

    pending = []
    for record in plan["tasks"]:
        if not isinstance(record, dict) or record.get("status_hint") != "CLARIFICATION_REQUIRED":
            continue
        intent_id = str(record.get("intent_id") or "")
        if intent_id not in {
            "TRANSFER_PREPARATION", "SAVED_CONTACT_LOOKUP", "TRANSACTION_ANALYSIS",
        }:
            continue
        if current_text is not None and not _is_plausible_pending_semantic_reply(current_text, intent_id):
            continue
        pending.append({
            "task_id": str(record.get("task_id") or ""),
            "intent_id": str(record.get("intent_id")),
            "requested_outcome": str(
                record.get("requested_outcome")
                or ("INFORMATION" if record.get("intent_id") == "TRANSACTION_ANALYSIS" else "ACTION")
            ),
            "reason_code": str(record.get("reason_code") or "FOLLOW_UP_PENDING"),
            "missing_fields": [str(value) for value in (record.get("missing_fields") or [])[:10]],
            "slots": {str(k): str(v) for k, v in (record.get("semantic_slots") or {}).items()},
            "entities": {str(k): str(v) for k, v in (record.get("semantic_entities") or {}).items()},
            "evidence": [
                {str(k): str(v) for k, v in item.items() if k in {"field", "source", "quote"}}
                for item in (record.get("semantic_evidence") or [])[:20]
                if isinstance(item, dict)
            ],
            "capability_id": record.get("capability_id"),
            "kind": record.get("kind"),
            "tool_name": record.get("tool_name"),
            "response_mode": record.get("response_mode"),
        })
    return pending[:2]


def _task_plan_records(
    plans: list[dict[str, Any]], *, model_deadline_epoch: float | None = None,
) -> dict[str, Any]:
    """Create a versioned serializable plan used for task/outcome correlation."""
    records = []
    for plan in plans:
        capability_id = plan.get("capability_id")
        capability_ids = [str(capability_id)] if capability_id else []
        execution_plan = plan.get("execution_plan", {}) if isinstance(plan.get("execution_plan"), dict) else {}
        capability_ids.extend(str(value) for value in (execution_plan.get("supporting_capabilities") or []) if value)
        presentation_id = plan.get("presentation_id")
        if presentation_id:
            presentation = get_registry().get("presentations", {}).get(presentation_id, {})
            capability_ids.extend(
                str(value) for value in presentation.get("binding", {}).get("source_capabilities", []) if value
            )
        capability_ids = list(dict.fromkeys(
            value for value in capability_ids if value in get_registry().get("capabilities", {})
        ))
        records.append({
            "task_id": str(plan.get("task_id", "")),
            "intent_id": str(plan.get("intent_id", "UNKNOWN")),
            "status_hint": str(plan.get("status", "PLANNED")),
            "reason_code": plan.get("reason_code"),
            "missing_fields": list(plan.get("missing_fields", []) or []),
            "requested_outcome": plan.get("requested_outcome"),
            "semantic_slots": {str(k): str(v) for k, v in (plan.get("semantic_slots") or {}).items()},
            "semantic_entities": {str(k): str(v) for k, v in (plan.get("semantic_entities") or {}).items()},
            "semantic_evidence": [
                {str(k): str(v) for k, v in item.items() if k in {"field", "source", "quote"}}
                for item in (plan.get("semantic_evidence") or [])[:40]
                if isinstance(item, dict)
            ],
            "capability_ids": capability_ids,
            "capability_id": str(capability_id) if capability_id else None,
            "kind": plan.get("kind"),
            "tool_name": plan.get("tool_name"),
            "arguments": copy.deepcopy(plan.get("arguments", {})),
            "presentation_id": plan.get("presentation_id"),
            "presentation_name": get_registry().get("presentations", {}).get(plan.get("presentation_id"), {}).get("name") if plan.get("presentation_id") else None,
            "response_mode": plan.get("response_mode"),
            "tool_call_ids": list(plan.get("tool_call_ids", []) or []),
        })
    return {
        "schema_version": "1.0.0",
        "model_deadline_epoch": model_deadline_epoch,
        "tasks": records,
    }


def _pending_semantic_task_plan(
    pending_tasks: list[dict[str, Any]], *, model_deadline_epoch: float | None = None,
) -> dict[str, Any]:
    """Rebuild a strict continuation plan when interpretation cannot resolve a reply.

    A model timeout or a task-less clarification must not erase a previously
    acknowledged, still-pending read-only task. Rebuild only the validated
    semantic summary; never restore executable arguments or tool-call IDs.
    """
    plans = []
    for item in pending_tasks[:2]:
        if not isinstance(item, dict) or item.get("intent_id") not in {
            "TRANSACTION_ANALYSIS", "SAVED_CONTACT_LOOKUP", "TRANSFER_PREPARATION",
        }:
            continue
        plans.append({
            "task_id": str(item.get("task_id") or f"pending-{uuid.uuid4().hex}"),
            "intent_id": str(item.get("intent_id")),
            "status": "CLARIFICATION_REQUIRED",
            "reason_code": str(item.get("reason_code") or "FOLLOW_UP_NOT_RESOLVED"),
            "missing_fields": list(item.get("missing_fields") or [])[:10],
            "requested_outcome": str(item.get("requested_outcome") or "INFORMATION"),
            "semantic_slots": dict(item.get("slots") or {}),
            "semantic_entities": dict(item.get("entities") or {}),
            "semantic_evidence": list(item.get("evidence") or [])[:20],
            "capability_id": item.get("capability_id"),
            "kind": item.get("kind"),
            "tool_name": item.get("tool_name"),
            "response_mode": item.get("response_mode"),
        })
    return _task_plan_records(plans, model_deadline_epoch=model_deadline_epoch)


def _pending_semantic_clarification(
    pending_tasks: list[dict[str, Any]], clarification: str | None = None,
) -> str:
    """Return a customer-facing fallback without discarding the active task."""
    supplied = str(clarification or "").strip()
    if supplied:
        return supplied[:500]
    first = next((item for item in pending_tasks if isinstance(item, dict)), {})
    intent = str(first.get("intent_id") or "")
    fields = [str(value) for value in (first.get("missing_fields") or [])]
    if intent == "TRANSACTION_ANALYSIS":
        labels = {
            "period": "time period",
            "flow": "whether you mean spending, income or both",
            "measure": "total, average or transaction count",
            "group_by": "how to group the results",
            "analysis_mode": "the type of analysis",
            "merchant_query": "the merchant or description",
            "category_query": "the spending category",
        }
        if fields:
            requested = [labels.get(field, field.replace("_", " ")) for field in fields[:3]]
            return "Your transaction-analysis request is still active. I couldn't validate that follow-up. Please clarify " + " and ".join(requested) + "."
        return "Your transaction-analysis request is still active, but I couldn't validate that follow-up. Could you clarify the detail you meant?"
    if intent == "SAVED_CONTACT_LOOKUP":
        return "Your saved-contact lookup is still active, but I couldn't validate that follow-up. Which contact did you mean?"
    if intent == "TRANSFER_PREPARATION":
        return "Your transfer preparation is still active, but I couldn't validate that follow-up. Please clarify the missing detail. No transfer draft was opened."
    return "Your earlier request is still active, but I couldn't validate that follow-up. Could you clarify the missing detail?"


def _task_record_for_plan(task_plan: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    return next((item for item in task_plan.get("tasks", []) if item.get("task_id") == task_id), None)


def _response_composition_prompt(
    *, current_text: str, task_plan: dict[str, Any], messages: list[Any] | tuple[Any, ...],
) -> str:
    """Bound the response model to validated execution receipts and task outcomes."""
    outcome = derive_turn_outcome(list(messages or []), fallback_turn_id=current_trace_id())
    # The response model receives the minimum semantic task context it needs.
    # Never send internal tool names, capability IDs, account IDs or runtime
    # arguments back into the natural-language composition prompt.
    public_task_plan = {
        "tasks": [
            {
                "intent_id": str(item.get("intent_id", "")),
                "status": str(item.get("status_hint", "PLANNED")),
                "response_mode": str(item.get("response_mode", "")),
                "missing_fields": list(item.get("missing_fields", []) or []),
            }
            for item in task_plan.get("tasks", []) if isinstance(item, dict)
        ]
    }
    payload = {
        "task_outcomes": [item.model_dump(mode="json") for item in outcome.task_outcomes],
        "validated_results": _validated_turn_results(messages),
    }
    return (
        "You are ORAIflow's customer-response composer. The runtime has already interpreted the request, "
        "selected registered capabilities and executed them. You have no tools or execution authority. "
        "Return exactly one oraiflow_customer_response tool call.\n\n"
        "Use only the validated result envelopes and task outcomes supplied below for customer-specific facts. "
        "Do not invent balances, amounts, dates, account details, loan terms, results or actions. Distinguish "
        "SUCCESS, EMPTY, PARTIAL, UNAVAILABLE and FAILED honestly. If several tasks exist, preserve each "
        "task's separate result; do not hide a successful task because another needs clarification. "
        "If the result supports a useful direct answer, answer it naturally and concisely. If data is absent, "
        "state what could not be verified and ask one targeted follow-up. Never claim a transfer was submitted, "
        "approved or paid merely because a draft was prepared. Never expose internal IDs, schemas or traces.\n\n"
        "For transaction-analysis results, report amounts separately for each currency. Interpret sum_minor and average_minor using that currency's minor units; never combine currencies or perform an unregistered FX conversion. The flow field determines whether amounts are incoming credits, positive-magnitude outflows, or signed net movement. A merchant group is based on the recorded transaction description, not a verified normalized merchant identity. Records categorized as TRANSFER are excluded unless include_transfers is true; do not claim all own-account transfers are identified. In recurring_inflows mode, potentially_recurring is only a screening signal based on repeated observations; it does not prove salary, employment or payer identity. If the history is insufficient, say so. If truncated is true, describe the result as partial. A successful query with transaction_count=0 means no matching records were found for the stated filter and period.\n\n"
        "For a successful read use classification IN_SCOPE. For an unresolved requirement use CLARIFICATION. "
        "For a general maintained financial-literacy explanation use EDUCATIONAL_GUIDANCE. Use OUT_OF_SCOPE "
        "only if the runtime task outcomes and registry context show no supported way to help.\n\n"
        "CURRENT CUSTOMER REQUEST:\n" + str(current_text or "")[:3000]
        + "\n\nRUNTIME TASK PLAN (descriptive; not new execution authority):\n"
        + json.dumps(public_task_plan, ensure_ascii=False, separators=(",", ":"))[:3000]
        + "\n\nVALIDATED OUTCOMES AND RESULTS:\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:16000]
    )


def build_graph(checkpointer=None):
    # Compile and validate the human policy + OCCF registry once for the graph
    # lifetime. Every semantic turn receives this same immutable decision map;
    # no per-turn capability discovery call or prompt-time registry rebuild is
    # required. Editing the JSON takes effect after the Agent Runtime restarts.
    """Build the Agent Runtime graph with the supplied durable checkpointer."""
    decision_map = build_agent_decision_map()
    policy_metadata = decision_map.get("policy", {})
    logger.info(
        "decision_routing_policy_loaded policy_id=%s policy_revision=%s registry_version=%s map_chars=%d",
        policy_metadata.get("policy_id", ""), policy_metadata.get("revision", ""),
        policy_metadata.get("registry_version", ""),
        len(json.dumps(decision_map, ensure_ascii=False, separators=(",", ":"))),
    )
    provider = os.getenv("LLM_PROVIDER", "demo").lower()
    if provider == "openai":
        if os.getenv("LLM_EXTERNAL_DATA_APPROVED", "false").lower() != "true":
            raise RuntimeError("Set LLM_EXTERNAL_DATA_APPROVED=true only after approving the provider's data handling for this environment")
        from langchain_openai import ChatOpenAI
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        model = ChatOpenAI(
            model=os.getenv("LLM_MODEL", "gpt-4.1-mini"),
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL") or None,
            temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
            timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "90")),
            max_retries=int(os.getenv("LLM_MAX_RETRIES", "1")),
        )
    elif provider == "ollama":
        from langchain_ollama import ChatOllama
        ollama_timeout = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", os.getenv("LLM_TIMEOUT_SECONDS", "60")))
        model = ChatOllama(
            model=os.getenv("LLM_MODEL", "qwen2.5:7b"),
            base_url=os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434"),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
            num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "8192")),
            keep_alive=os.getenv("OLLAMA_KEEP_ALIVE", "5m"),
            client_kwargs={"timeout": ollama_timeout},
            async_client_kwargs={"timeout": ollama_timeout},
        )
    elif provider == "anthropic":
        if os.getenv("LLM_EXTERNAL_DATA_APPROVED", "false").lower() != "true":
            raise RuntimeError("Set LLM_EXTERNAL_DATA_APPROVED=true only after approving the provider's data handling for this environment")
        from langchain_anthropic import ChatAnthropic
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
        model = ChatAnthropic(
            model=os.getenv("LLM_MODEL", "claude-3-5-haiku-latest"),
            api_key=api_key,
            temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
            timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "90")),
            max_retries=int(os.getenv("LLM_MAX_RETRIES", "1")),
        )
    else:
        raise RuntimeError(f"No hosted model graph for provider: {provider}")

    # This runtime-only response schema is immutable for the lifetime of the
    # graph. Build it once rather than rebuilding its Pydantic tool schema on
    # every conversational turn.
    customer_response_tool = _build_customer_response_tool()
    assessment_tool = _build_turn_assessment_tool()

    def route(state: ORAIState) -> str:
        messages = state.get("messages", [])
        # A verified browser action already has its one authoritative customer
        # acknowledgement. Stop graph re-entry here as well as at call_model;
        # this prevents a post-action SOCIAL fallback from adding another bubble.
        if has_successful_frontend_ack_after_latest_user(messages):
            return END
        last = messages[-1]
        # Backend MCP calls always take precedence. call_model strips frontend
        # actions from a mixed response and schedules any required display-data
        # reads before the browser is allowed to render a GenUI component.
        backend_tool_names = {banking_tool.name for banking_tool in TOOLS}
        calls = getattr(last, "tool_calls", None) or []
        return "tools" if any(
            isinstance(call, dict) and call.get("name") in backend_tool_names
            for call in calls
        ) else END

    async def compose_grounded_response(
        state: ORAIState,
        task_plan: dict[str, Any],
        turn_context: AgentTurnContext,
        discovered_candidates: list[dict[str, Any]],
    ) -> AIMessage:
        """Compose prose from validated results with no executable tools exposed."""
        current_text = latest_human_text(state.get("messages", []))
        prompt = _response_composition_prompt(
            current_text=current_text,
            task_plan=task_plan,
            messages=state.get("messages", []),
        )
        started = time.perf_counter()
        configured_response_timeout = max(0.5, float(os.getenv("LLM_RESPONSE_TIMEOUT_SECONDS", "25")))
        deadline_epoch = task_plan.get("model_deadline_epoch")
        remaining_budget = (
            float(deadline_epoch) - time.time()
            if isinstance(deadline_epoch, (int, float)) else configured_response_timeout
        )
        if remaining_budget <= 0.5:
            logger.warning("agent_model_turn_budget_exhausted trace_id=%s phase=response", current_trace_id())
            return AIMessage(content=(
                "I retrieved the available ORAIflow information, but the response time budget for this turn was reached. "
                "The verified records have not been changed. Please ask me to continue with the explanation."
            ), additional_kwargs={"oraiflow_terminal_error": {"code": "MODEL_TURN_BUDGET_EXHAUSTED", "retryable": True}})
        response_timeout = min(configured_response_timeout, remaining_budget)
        try:
            response_model = model.bind_tools([customer_response_tool])
            # Keep the internal response-envelope tool call buffered until it
            # has been validated. Streaming unvalidated tool-call chunks through
            # LangGraph/AG-UI can expose internal payloads or leave the UI with
            # an incomplete final message. Customer-visible progress is handled
            # by the AG-UI transport keepalive; validated response delivery is
            # a single terminal message.
            response = await asyncio.wait_for(
                invoke_internal_model(
                    response_model,
                    [SystemMessage(content=prompt), HumanMessage(content=current_text[:3000])],
                    phase="response-composition",
                ),
                timeout=response_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "agent_response_composition_timeout trace_id=%s provider=%s timeout_s=%s",
                current_trace_id(), provider, response_timeout,
            )
            return AIMessage(content="I retrieved the available ORAIflow information, but the explanation took too long to complete. Please try again; the verified records have not been changed.")
        except Exception:
            logger.exception("agent_response_composition_failed trace_id=%s provider=%s", current_trace_id(), provider)
            return AIMessage(content="I retrieved the available ORAIflow information, but couldn't prepare a reliable explanation just now. Please review any verified information shown above or try again.")
        finally:
            logger.info(
                "agent_response_composition_finished trace_id=%s provider=%s duration_ms=%d",
                current_trace_id(), provider, round((time.perf_counter() - started) * 1000),
            )

        if response is None:
            return AIMessage(content="I retrieved the available ORAIflow information, but couldn't prepare a response. Please try again.")
        customer_response = _customer_response_from_protocol(
            response if isinstance(response, AIMessage) else AIMessage(content=str(getattr(response, "content", "")), tool_calls=getattr(response, "tool_calls", []) or []),
            state.get("messages", []),
            discovered_intent_candidates=discovered_candidates,
        )
        if customer_response is not None:
            return _sanitize_customer_response(customer_response)

        # Some providers return ordinary text despite the required response
        # tool. Route that text through the same versioned envelope validator.
        text_content = getattr(response, "content", "")
        if isinstance(text_content, list):
            text_content = message_content_to_text(text_content)
        text_content = str(text_content or "").strip()
        turn_outcome = derive_turn_outcome(
            list(state.get("messages", [])), fallback_turn_id=turn_context.turn_id,
        )
        classification = "IN_SCOPE" if (
            turn_outcome.successful_capabilities
            or turn_outcome.partial_or_empty_capabilities
            or turn_outcome.successful_frontend_actions
            or any(item.status in {"SUCCEEDED", "PARTIAL", "EMPTY"} for item in turn_outcome.task_outcomes)
        ) else "CLARIFICATION"
        response_args = {
            "classification": classification,
            "resolution": "RESOLVED" if classification == "IN_SCOPE" else "NEEDS_CLARIFICATION",
            "message": text_content or "I checked the available records, but I couldn't prepare a complete explanation. Could you clarify what you'd like to understand?",
        }
        synthetic = AIMessage(tool_calls=[{
            "name": CUSTOMER_RESPONSE_TOOL_NAME,
            "args": response_args,
            "id": f"oraiflow-response-{uuid.uuid4().hex}",
            "type": "tool_call",
        }])
        validated = _customer_response_from_protocol(
            synthetic,
            state.get("messages", []),
            discovered_intent_candidates=discovered_candidates,
        )
        return _sanitize_customer_response(validated or AIMessage(content="I checked the available records, but couldn't validate the response. Please try again."))

    async def call_model(state: ORAIState) -> dict[str, Any]:
        turn_started_at = time.perf_counter()
        model_turn_budget = max(5.0, float(os.getenv("LLM_TURN_MODEL_BUDGET_SECONDS", "70")))
        model_deadline_epoch = time.time() + model_turn_budget
        current_text = latest_human_text(state.get("messages", []))
        turn_context, discovered_candidates = _build_agent_turn_context(state, current_text)
        turn_context_prepared_ms = round((time.perf_counter() - turn_started_at) * 1000)
        context = _selected_account_context(state)

        def emit(message: AIMessage) -> dict[str, Any]:
            return {"messages": [_attach_agent_turn_contract(message, turn_context, state.get("messages", []))]}

        logger.info(
            "agent_turn_contract_prepared trace_id=%s turn_id=%s registry=%s workspace=%s mcp=%d frontend=%d presentations=%d candidates=%d duration_ms=%d",
            current_trace_id(), turn_context.turn_id, turn_context.registry_version,
            turn_context.workspace_id or "unknown", len(turn_context.available_mcp_capability_ids),
            len(turn_context.available_frontend_capability_ids), len(turn_context.available_presentation_ids),
            len(turn_context.intent_candidates), turn_context_prepared_ms,
        )

        # CopilotKit may re-enter the graph after a frontend action has already
        # returned its receipt. Finish without another model call or duplicate ack.
        if has_successful_frontend_ack_after_latest_user(state.get("messages", [])):
            logger.info("frontend_action_final_response_replay_suppressed trace_id=%s", current_trace_id())
            return {}

        if is_instruction_override_attempt(current_text):
            message = AIMessage(content=INSTRUCTION_OVERRIDE_MESSAGE)
            logger.warning("instruction_override_blocked trace_id=%s", current_trace_id())
            return emit(message)

        # A new explicit transfer supersedes an older pending transfer. After
        # that admission check, resolve an active bounded transfer clarification
        # before general navigation/semantic interpretation so replies such as
        # "former" and "latter" are interpreted against the displayed contacts.
        transfer_start = _start_explicit_transfer_workflow(state, context)
        if transfer_start is not None:
            return emit(transfer_start)

        transfer_step = _handle_transfer_workflow(state, context)
        if transfer_step is not None:
            return emit(transfer_step)

        navigation_step = _pending_navigation_message(state)
        if navigation_step is not None:
            return emit(navigation_step)

        # A browser receipt is already an authoritative frontend outcome. Acknowledge
        # it before task-plan continuation so a model response cannot replace the
        # useful action confirmation or add an unnecessary inference round-trip.
        frontend_ack = _frontend_action_acknowledgement(state)
        if frontend_ack is not None:
            return emit(frontend_ack)

        # Resume a runtime-owned interpreted plan after its backend result has
        # returned. Do not reinterpret the same customer message or issue the
        # same capability twice.
        existing_task_plan = _current_turn_task_plan(state.get("messages", []))
        if existing_task_plan:
            # Browser tool results do not always preserve custom metadata. Use
            # the originating assistant call ID as the stable correlation key,
            # with explicit ToolMessage metadata taking precedence.
            correlated_call_tasks: dict[str, list[str]] = {}
            for prior in state.get("messages", []):
                if message_type(prior) != "ai":
                    continue
                prior_metadata = message_metadata(prior)
                mapping = prior_metadata.get("oraiflow_task_call_map", {})
                if isinstance(mapping, dict):
                    for call_id, task_ids in mapping.items():
                        values = task_ids if isinstance(task_ids, list) else [task_ids]
                        correlated_call_tasks[str(call_id)] = [str(value) for value in values if value]
                for planned_record in (prior_metadata.get("oraiflow_task_plan", {}) or {}).get("tasks", []):
                    if not isinstance(planned_record, dict):
                        continue
                    for call_id in planned_record.get("tool_call_ids", []) or []:
                        correlated_call_tasks.setdefault(str(call_id), []).append(str(planned_record.get("task_id", "")))
            def _message_task_ids(item: Any) -> list[str]:
                item_metadata = message_metadata(item)
                explicit_ids = item_metadata.get("oraiflow_task_ids")
                if not isinstance(explicit_ids, list):
                    explicit_id = item_metadata.get("oraiflow_task_id")
                    explicit_ids = [explicit_id] if explicit_id else []
                if explicit_ids:
                    return [str(value) for value in explicit_ids if value]
                return correlated_call_tasks.get(str(message_value(item, "tool_call_id", "")), [])

            presentation_task_names = {
                str(task.get("task_id", "")): str(task.get("presentation_name", ""))
                for task in existing_task_plan.get("tasks", [])
                if task.get("presentation_id")
            }
            presentation_receipts = set()
            for item in state.get("messages", []):
                if message_type(item) != "tool":
                    continue
                task_ids = _message_task_ids(item)
                for task_id in task_ids:
                    if presentation_task_names.get(task_id) == str(message_value(item, "name", "")):
                        presentation_receipts.add(task_id)
            presentation_tasks = [
                item for item in existing_task_plan.get("tasks", [])
                if item.get("status_hint") == "PLANNED" and item.get("presentation_id")
                and str(item.get("task_id", "")) not in presentation_receipts
            ]
            for task_record in presentation_tasks:
                rendered = canonical_intent_step(str(task_record.get("intent_id", "")), state)
                if rendered is not None:
                    call_map: dict[str, list[str]] = {}
                    for call in getattr(rendered, "tool_calls", None) or []:
                        if isinstance(call, dict) and call.get("id"):
                            call_map[str(call["id"])] = [str(task_record.get("task_id", ""))]
                            task_record.setdefault("tool_call_ids", []).append(str(call["id"]))
                    return emit(_attach_task_plan(
                        rendered, existing_task_plan, call_map if call_map else None,
                    ))

            # A task plan is resumed only after all planned non-presentation
            # calls have a correlated ToolMessage. Missing receipts become an
            # explicit terminal error rather than another model-selection loop.
            active_records = [
                item for item in existing_task_plan.get("tasks", [])
                if item.get("status_hint") == "PLANNED" and not item.get("presentation_id")
            ]
            tool_messages = [
                item for item in state.get("messages", [])
                if message_type(item) == "tool"
            ]
            completed_task_ids = {
                str(task_id)
                for item in tool_messages
                for task_id in _message_task_ids(item)
            }
            planned_presentation_ids = {
                str(item.get("task_id", "")) for item in existing_task_plan.get("tasks", [])
                if item.get("status_hint") == "PLANNED" and item.get("presentation_id")
            }
            if planned_presentation_ids and planned_presentation_ids.issubset(presentation_receipts):
                logger.info("agent_presentation_task_complete trace_id=%s task_count=%d", current_trace_id(), len(planned_presentation_ids))
                final_message = AIMessage(content="I've displayed the verified information above.")
                return emit(_attach_task_plan(final_message, existing_task_plan))

            backend_awaiting = [
                str(item.get("task_id", "")) for item in active_records
                if item.get("kind") == "mcp"
                and str(item.get("task_id", "")) not in completed_task_ids
            ]
            if not backend_awaiting:
                # Loan schedules have a deterministic domain explanation; do
                # not spend another model round-trip paraphrasing a fixed record.
                for task_record in active_records:
                    if task_record.get("intent_id") == "LOAN_INFORMATION":
                        deterministic = canonical_intent_step("LOAN_INFORMATION", state)
                        if deterministic is not None:
                            return emit(_attach_task_plan(deterministic, existing_task_plan))
                pending_frontend = [
                    item for item in active_records
                    if item.get("kind") == "frontend"
                    and str(item.get("task_id", "")) not in completed_task_ids
                    and item.get("tool_name")
                ]
                if pending_frontend:
                    frontend_calls = []
                    call_map = {}
                    for item in pending_frontend:
                        call_id = f"oraiflow-planned-ui-{uuid.uuid4().hex}"
                        frontend_calls.append({
                            "name": str(item["tool_name"]),
                            "args": dict(item.get("arguments", {})),
                            "id": call_id,
                            "type": "tool_call",
                        })
                        call_map[call_id] = [str(item.get("task_id", ""))]
                        item.setdefault("tool_call_ids", []).append(call_id)
                    return emit(_attach_task_plan(
                        AIMessage(content="", tool_calls=frontend_calls),
                        existing_task_plan, call_map,
                    ))
                composed = await compose_grounded_response(
                    state, existing_task_plan, turn_context, discovered_candidates,
                )
                # The last assistant message is what the next user turn reads
                # when looking for a pending semantic task. Preserve the
                # originating plan on the customer-facing composition result,
                # not only on the internal tool-call message.
                return emit(_attach_task_plan(composed, existing_task_plan))

        # The configured decision policy owns the priority of pre-assessment
        # fast routes. Each handler is an existing resource/read/workflow owner;
        # the policy does not duplicate its capability schema or business logic.
        route_handlers = {
            "academy_personal_read": lambda: _academy_personal_data_read_step(state),
            "workspace_navigation": lambda: _workspace_navigation_step(current_text, state),
            "workspace_knowledge": lambda: workspace_help_for_request(current_text),
            "academy_education": lambda: academy_direct_guidance_for_request(current_text),
            "connector_failure": lambda: (
                _connector_failure_message(name)
                if (name := _connector_error_for_turn(state.get("messages", []))) else None
            ),
            "canonical_read": lambda: (
                canonical_intent_step(intent, state)
                if (intent := classify_canonical_read_intent(current_text)) else None
            ),
            "assistant_capabilities": lambda: assistant_capability_help_for_request(current_text),
            "simple_social": lambda: SOCIAL_REPLY_MESSAGE if is_simple_social_turn(current_text) else None,
        }
        for configured_route in pre_assessment_routes():
            handler = route_handlers.get(configured_route.handler)
            if handler is None:
                logger.error(
                    "decision_route_handler_missing trace_id=%s route=%s handler=%s",
                    current_trace_id(), configured_route.id, configured_route.handler,
                )
                continue
            route_started = time.perf_counter()
            route_result = handler()
            if route_result is None:
                continue
            if isinstance(route_result, str):
                route_message = AIMessage(content=route_result)
            else:
                route_message = route_result
            policy_identity = decision_policy_identity()
            logger.info(
                "decision_route_selected trace_id=%s policy_id=%s policy_revision=%s route=%s handler=%s duration_ms=%d",
                current_trace_id(), policy_identity["policy_id"], policy_identity["revision"],
                configured_route.id, configured_route.handler,
                round((time.perf_counter() - route_started) * 1000),
            )
            return emit(route_message)

        copilot_state = state.get("copilotkit") or {}
        registry = get_registry()
        runtime_context = _selected_account_context(state)
        # Only descriptive account/workspace metadata is shown to the model.
        # Account IDs, balances and authorization material remain runtime-owned.
        interpreter_context = {
            "workspace": runtime_context.get("workspace"),
            "selected_account_name": runtime_context.get("selected_account_name"),
            "accounts": [
                {
                    "account_name": item.get("account_name"),
                    "currency": item.get("currency"),
                }
                for item in runtime_context.get("accounts", [])
                if isinstance(item, dict)
            ][:20],
            "saved_contacts": rank_saved_contacts_for_context(
                runtime_context.get("contacts", []), current_text, limit=20,
            ),
            "available_workspaces": list(registry.get("application_policy", {}).get("navigation", {}).get("pages", [])),
        }
        pending_semantic_tasks = _pending_semantic_tasks(state.get("messages", []), current_text)
        if pending_semantic_tasks:
            interpreter_context["pending_tasks"] = pending_semantic_tasks
        source_context = assessment_sources(
            list(state.get("messages", [])), current_text, runtime_context,
        )
        conversation_excerpt = _conversation_excerpt(state.get("messages", []))
        # Include only source-validated prior capability results, not raw tool
        # output or arbitrary tool instructions.
        if source_context.get("capability_result"):
            conversation_excerpt += "\nVALIDATED PRIOR CAPABILITY RESULTS:\n" + source_context["capability_result"][-4000:]
        candidate_hints = []
        for item in discovered_candidates[:5]:
            candidate_intent = registry.get("intents", {}).get(item.get("intent_id"), {})
            candidate_plan = candidate_intent.get("execution_plan", {}) if isinstance(candidate_intent, dict) else {}
            candidate_discovery = candidate_intent.get("semantic_discovery", {}) if isinstance(candidate_intent, dict) else {}
            candidate_hints.append({
                "intent_id": item.get("intent_id"),
                "description": str(item.get("description", ""))[:360],
                "match_kind": item.get("match_kind"),
                "matched_phrase": item.get("matched_phrase"),
                "matched_typo_token": item.get("matched_typo_token"),
                "customer_language_examples": [str(value)[:120] for value in (candidate_discovery.get("phrases") or [])[:3]],
                "customer_language_concepts": [str(value)[:80] for value in (candidate_discovery.get("keywords") or [])[:4]],
                "semantic_inputs": {
                    str(name): {
                        "description": str(spec.get("description", ""))[:180],
                        "allowed_values": [str(value) for value in (spec.get("allowed_values") or [])[:8]],
                        "default": spec.get("default"),
                        "default_policy": spec.get("default_policy"),
                    }
                    for name, spec in (candidate_plan.get("semantic_inputs") or {}).items()
                    if isinstance(spec, dict)
                },
                "outcome_semantics": str(candidate_plan.get("outcome_semantics", ""))[:360],
                "score": item.get("score"),
            })
        for pending in pending_semantic_tasks:
            if not any(item.get("intent_id") == pending.get("intent_id") for item in candidate_hints):
                pending_intent = registry.get("intents", {}).get(pending.get("intent_id"), {})
                candidate_hints.insert(0, {
                    "intent_id": pending.get("intent_id"),
                    "description": str(pending_intent.get("description", ""))[:240],
                    "match_kind": "pending_task_continuation",
                    "matched_phrase": None,
                    "score": 1200,
                })
        assessment_prompt = build_assessment_prompt(
            intent_candidates=candidate_hints,
            catalogue=task_prompt_catalogue(registry),
            conversation_excerpt=conversation_excerpt,
            application_context=interpreter_context,
            decision_map=decision_map,
        )
        logger.info(
            "agent_interpretation_prompt_profile trace_id=%s prompt_chars=%d decision_map_chars=%d application_context_chars=%d conversation_chars=%d saved_contacts=%d pending_tasks=%d",
            current_trace_id(),
            len(assessment_prompt),
            len(json.dumps(decision_map, ensure_ascii=False, separators=(",", ":"))),
            len(json.dumps(interpreter_context, ensure_ascii=False, separators=(",", ":"))),
            len(conversation_excerpt),
            len(interpreter_context.get("saved_contacts", [])),
            len(pending_semantic_tasks),
        )
        interpretation_timeout = min(
            max(2.0, float(os.getenv("LLM_INTERPRETATION_TIMEOUT_SECONDS", "50"))),
            max(0.5, model_deadline_epoch - time.time()),
        )
        if interpretation_timeout <= 0.5:
            message = AIMessage(content="I couldn't begin interpreting this request within the available response time. Please try again.")
            message.additional_kwargs["oraiflow_terminal_error"] = {"code": "MODEL_TURN_BUDGET_EXHAUSTED", "retryable": True}
            return emit(message)
        interpretation_started = time.perf_counter()
        assessment, assessment_status = None, {"code": "ASSESSMENT_NOT_ATTEMPTED"}
        try:
            # Prefer provider-native Pydantic/JSON-schema output. The intercepted
            # forced-tool channel is a compatibility fallback, not the primary
            # semantic contract. Neither route exposes executable capabilities.
            assessment_model = None
            assessment_binding_mode = "unavailable"
            try:
                assessment_model, assessment_binding_mode = bind_structured_assessment_model(model)
            except (TypeError, ValueError, NotImplementedError, AttributeError):
                assessment_model, assessment_binding_mode = bind_required_assessment_model(model, assessment_tool, provider)

            try:
                assessment_response = await asyncio.wait_for(
                    invoke_internal_model(
                        assessment_model,
                        [
                            SystemMessage(content=assessment_prompt),
                            HumanMessage(content=current_text[:4000]),
                        ],
                        phase="turn-assessment",
                    ),
                    timeout=interpretation_timeout,
                )
            except asyncio.TimeoutError:
                raise
            except Exception as exc:
                # Retry only adapter/schema incompatibility via the forced
                # assessment-tool channel. Provider outages, auth failures and
                # timeouts are not retried here, avoiding latency amplification.
                if not assessment_binding_mode.startswith("native_") or not _is_structured_output_compatibility_error(exc):
                    raise
                remaining = max(0.0, model_deadline_epoch - time.time())
                if remaining <= 0.5:
                    raise
                logger.warning(
                    "agent_structured_adapter_fallback trace_id=%s provider=%s binding=%s error_type=%s fallback=required_tool",
                    current_trace_id(), provider, assessment_binding_mode, type(exc).__name__,
                )
                assessment_model, fallback_mode = bind_required_assessment_model(model, assessment_tool, provider)
                assessment_binding_mode = f"{assessment_binding_mode}+{fallback_mode}"
                assessment_response = await asyncio.wait_for(
                    invoke_internal_model(
                        assessment_model,
                        [
                            SystemMessage(content=assessment_prompt),
                            HumanMessage(content=current_text[:4000]),
                        ],
                        phase="turn-assessment-adapter-fallback",
                    ),
                    timeout=min(interpretation_timeout, remaining),
                )
            assessment, assessment_status = _assessment_from_model_response(assessment_response)
            if assessment is not None:
                assessment, evidence_repair_info = repair_registered_semantic_evidence(
                    assessment, current_text, registry,
                )
                if evidence_repair_info.get("repaired_fields") or evidence_repair_info.get("defaulted_fields"):
                    logger.info(
                        "agent_registered_evidence_repair trace_id=%s repaired_fields=%s defaulted_fields=%s",
                        current_trace_id(),
                        ",".join(evidence_repair_info.get("repaired_fields", [])),
                        ",".join(evidence_repair_info.get("defaulted_fields", [])),
                    )
                missing_evidence = missing_task_field_evidence(assessment)
                if missing_evidence:
                    assessment = None
                    assessment_status = {
                        "code": "ASSESSMENT_FIELD_EVIDENCE_MISSING",
                        "missing": missing_evidence,
                    }

            # One bounded cross-adapter recovery is allowed. If native structured
            # output did not validate, retry through the provider's required
            # assessment tool (or vice versa). Both adapters feed the same strict
            # schema; neither may expose executable capabilities.
            if assessment is None:
                remaining = max(0.0, model_deadline_epoch - time.time())
                repair_timeout = min(
                    max(1.0, float(os.getenv("LLM_ASSESSMENT_REPAIR_TIMEOUT_SECONDS", "8"))),
                    remaining,
                )
                if repair_timeout > 0.5:
                    evidence_repair = ""
                    if assessment_status.get("code") == "ASSESSMENT_FIELD_EVIDENCE_MISSING":
                        missing_fields = [
                            f"task[{item.get('task_index')}].{field}"
                            for item in assessment_status.get("missing", [])
                            if isinstance(item, dict)
                            for field in item.get("fields", [])
                        ]
                        evidence_repair = (
                            " FIELD-EVIDENCE REPAIR: the previous structured output proposed these fields "
                            f"without field-specific evidence: {', '.join(missing_fields)}. "
                            "Preserve the original intent IDs and every explicitly supported slot/entity value exactly. "
                            "Add one evidence item per retained field, with a short exact quotation copied "
                            "verbatim from the current customer turn or supplied customer conversation. "
                            "Do not invent or paraphrase quotations, do not cite assistant text, and do not "
                            "replace an explicit customer value with a registered default. If a proposed value "
                            "was only a default and the customer did not specify it, remove that field so the "
                            "runtime can apply the OCCF default."
                        )
                    repair_prompt = (
                        assessment_prompt
                        + "\n\nSTRUCTURED OUTPUT RECOVERY: the previous output did not validate. "
                        "Return one complete assessment using the provided structured schema. "
                        "Do not answer the customer in prose. Preserve every supported task and every explicit slot/entity. "
                        "Use only registered intent IDs. Do not add capability IDs, tool names or executable arguments."
                        + evidence_repair
                    )
                    if assessment_binding_mode.startswith("native_"):
                        recovery_model, recovery_mode = bind_required_assessment_model(model, assessment_tool, provider)
                    else:
                        try:
                            recovery_model, recovery_mode = bind_structured_assessment_model(model)
                        except (TypeError, ValueError, NotImplementedError, AttributeError):
                            recovery_model, recovery_mode = assessment_model, assessment_binding_mode
                    assessment_response = await asyncio.wait_for(
                        invoke_internal_model(
                            recovery_model,
                            [
                                SystemMessage(content=repair_prompt),
                                HumanMessage(content=current_text[:4000]),
                            ],
                            phase="turn-assessment-recovery",
                        ),
                        timeout=repair_timeout,
                    )
                    assessment, assessment_status = _assessment_from_model_response(assessment_response)
                    if assessment is not None:
                        assessment, evidence_repair_info = repair_registered_semantic_evidence(
                            assessment, current_text, registry,
                        )
                        if evidence_repair_info.get("repaired_fields") or evidence_repair_info.get("defaulted_fields"):
                            logger.info(
                                "agent_registered_evidence_repair trace_id=%s repaired_fields=%s defaulted_fields=%s",
                                current_trace_id(),
                                ",".join(evidence_repair_info.get("repaired_fields", [])),
                                ",".join(evidence_repair_info.get("defaulted_fields", [])),
                            )
                        missing_evidence = missing_task_field_evidence(assessment)
                        if missing_evidence:
                            assessment = None
                            assessment_status = {
                                "code": "ASSESSMENT_FIELD_EVIDENCE_MISSING",
                                "missing": missing_evidence,
                            }
                    assessment_binding_mode = f"{assessment_binding_mode}+{recovery_mode}"
        except asyncio.TimeoutError:
            logger.warning(
                "agent_interpretation_timeout trace_id=%s provider=%s timeout_s=%s",
                current_trace_id(), provider, interpretation_timeout,
            )
        except Exception:
            logger.exception(
                "agent_interpretation_failed trace_id=%s provider=%s elapsed_ms=%d",
                current_trace_id(), provider, round((time.perf_counter() - interpretation_started) * 1000),
            )
        finally:
            logger.info(
                "agent_interpretation_finished trace_id=%s provider=%s binding=%s duration_ms=%d prompt_chars=%d intent_count=%d candidate_count=%d assessment_status=%s",
                current_trace_id(), provider, locals().get("assessment_binding_mode", "unbound"),
                round((time.perf_counter() - interpretation_started) * 1000),
                len(assessment_prompt), len(registry.get("intents", {})), len(candidate_hints),
                assessment_status.get("code", "unknown"),
            )

        if assessment is None:
            # If the model proposed values but failed to supply field-specific
            # customer quotations, do not route around that failure with a
            # defaulted read fallback: an explicit period/filter could be lost.
            if assessment_status.get("code") != "ASSESSMENT_FIELD_EVIDENCE_MISSING":
                assessment = safe_canonical_read_assessment(
                    current_text, discovered_candidates, registry,
                )
                fallback_kind = "canonical_read"
            else:
                fallback_kind = "field_evidence_repair_failed"
            # Explicit transfer recovery is independently grounded from exact
            # amount/beneficiary/currency spans; unlike a defaulted read
            # fallback, it cannot discard an explicit semantic filter.
            if assessment is None:
                assessment = safe_explicit_transfer_assessment(current_text, runtime_context)
                if assessment is not None:
                    fallback_kind = "explicit_transfer_slot_recovery"
            if assessment is not None:
                logger.warning(
                    "agent_interpretation_safe_fallback trace_id=%s intents=%s kind=%s reason=%s",
                    current_trace_id(),
                    ",".join(task.intent_id for task in assessment.tasks),
                    fallback_kind,
                    assessment_status.get("code", "unknown"),
                )
            else:
                # A short follow-up may arrive while the provider is slow or
                # returns malformed structured output. Keep an already-open
                # read-only task alive instead of resetting the conversation
                # to a generic new-request prompt.
                if pending_semantic_tasks:
                    message = AIMessage(content=_pending_semantic_clarification(pending_semantic_tasks))
                    message.additional_kwargs["oraiflow_terminal_error"] = {
                        "code": assessment_status.get("code", "ASSESSMENT_INVALID"),
                        "retryable": True,
                    }
                    return emit(_attach_task_plan(
                        message,
                        _pending_semantic_task_plan(
                            pending_semantic_tasks,
                            model_deadline_epoch=model_deadline_epoch,
                        ),
                    ))
                # Never silently discard a clear registered request. Ask a
                # useful, task-aware question rather than the generic
                # "rephrase" response. Consequential actions are not executed
                # by this fallback.
                transfer = next((item for item in discovered_candidates if item.get("intent_id") == "TRANSFER_PREPARATION"), None)
                if transfer:
                    message = AIMessage(content=(
                        "I understand you want to prepare a transfer. I couldn't validate the structured details just now, "
                        "so I have not searched contacts or opened a draft. Please send the amount, currency and saved recipient together, "
                        "for example: ‘Send 50 EUR to John’."
                    ))
                    message.additional_kwargs["oraiflow_terminal_error"] = {"code": assessment_status.get("code", "ASSESSMENT_INVALID"), "retryable": True}
                    return emit(message)
                candidate = discovered_candidates[0] if discovered_candidates else None
                if candidate and candidate.get("intent_id") == "TRANSACTION_ANALYSIS":
                    missing = {
                        field
                        for item in assessment_status.get("missing", []) if isinstance(item, dict)
                        for field in item.get("fields", [])
                    }
                    if "merchant_query" in missing or "category_query" in missing:
                        message_text = (
                            "I can analyze your booked transactions, but I couldn't verify the merchant or category filter. "
                            "Please repeat the merchant or category name exactly as it appears in your request. "
                            "I haven't retrieved any transaction data yet."
                        )
                    else:
                        message_text = (
                            "I can analyze your booked transactions, but I couldn't safely validate the requested measure or time range. "
                            "Please specify the time range or whether you want a total, average or transaction count. "
                            "I haven't retrieved any transaction data yet."
                        )
                    message = AIMessage(content=message_text)
                elif candidate:
                    # Registry descriptions are internal routing metadata, not
                    # customer-facing copy. Never expose them as a clarification.
                    message = AIMessage(content=(
                        "I found a supported ORAIflow task, but couldn't validate the details of your request. "
                        "I haven't retrieved or changed anything. Could you clarify what result you're looking for?"
                    ))
                else:
                    message = AIMessage(content=(
                        "I can help with ORAIflow workspaces, accounts, activity, loans, Financial Health, Academy topics and supported transfer preparation. "
                        "I couldn't confidently identify which of those you mean. What would you like to understand or do?"
                    ))
                message.additional_kwargs["oraiflow_terminal_error"] = {"code": assessment_status.get("code", "ASSESSMENT_INVALID"), "retryable": True}
                return emit(message)

        if assessment.resolution == "SOCIAL":
            if discovered_candidates:
                return emit(AIMessage(content=(
                    "I may be able to help with that through ORAIflow, but I couldn't confidently identify the task. "
                    "Could you tell me what information or outcome you're looking for?"
                )))
            return emit(AIMessage(content=SOCIAL_REPLY_MESSAGE))
        if assessment.resolution == "OUT_OF_SCOPE":
            if discovered_candidates:
                return emit(AIMessage(content=(
                    "I may be able to help with that through a registered ORAIflow workspace or capability, "
                    "but I couldn't confidently match the request. Could you clarify what you'd like to see or do?"
                )))
            return emit(AIMessage(content=OUT_OF_SCOPE_MESSAGE))
        if assessment.resolution == "NEEDS_CLARIFICATION" and not assessment.tasks:
            recovered = safe_canonical_read_assessment(
                current_text, discovered_candidates, registry,
            )
            if recovered is not None:
                logger.warning(
                    "agent_empty_clarification_contract_recovered trace_id=%s intents=%s",
                    current_trace_id(), ",".join(task.intent_id for task in recovered.tasks),
                )
                assessment = recovered
            else:
                question = safe_customer_clarification(
                    assessment.clarification_message, discovered_candidates,
                )
                if pending_semantic_tasks:
                    message = AIMessage(content=_pending_semantic_clarification(
                        pending_semantic_tasks, question,
                    ))
                    return emit(_attach_task_plan(
                        message,
                        _pending_semantic_task_plan(
                            pending_semantic_tasks,
                            model_deadline_epoch=model_deadline_epoch,
                        ),
                    ))
                return emit(AIMessage(content=question[:500]))
        if not assessment.tasks:
            question = safe_customer_clarification(
                assessment.clarification_message, discovered_candidates,
            )
            if pending_semantic_tasks:
                message = AIMessage(content=_pending_semantic_clarification(
                    pending_semantic_tasks, question,
                ))
                return emit(_attach_task_plan(
                    message,
                    _pending_semantic_task_plan(
                        pending_semantic_tasks,
                        model_deadline_epoch=model_deadline_epoch,
                    ),
                ))
            return emit(AIMessage(content=question[:500]))

        planned_tasks: list[dict[str, Any]] = []
        for interpreted_task in assessment.tasks:
            planned = plan_interpreted_task(
                interpreted_task,
                sources=source_context,
                messages=list(state.get("messages", [])),
                context=runtime_context,
                registry=registry,
            )
            planned["requested_outcome"] = interpreted_task.requested_outcome
            planned["semantic_slots"] = dict(interpreted_task.slots)
            planned["semantic_entities"] = dict(interpreted_task.entities)
            planned["semantic_evidence"] = [item.model_dump(mode="json") for item in interpreted_task.evidence]
            planned_tasks.append(planned)
        policy_identity = decision_policy_identity()
        logger.info(
            "decision_task_plan_created trace_id=%s policy_id=%s policy_revision=%s tasks=%s",
            current_trace_id(), policy_identity["policy_id"], policy_identity["revision"],
            json.dumps([
                {
                    "task_id": item.get("task_id"),
                    "intent_id": item.get("intent_id"),
                    "task_class": item.get("task_class"),
                    "dispatch_handler": item.get("dispatch_handler"),
                    "status": item.get("status"),
                    "reason_code": item.get("reason_code"),
                    "missing_fields": list(item.get("missing_fields", []) or []),
                    "semantic_slot_fields": sorted((item.get("semantic_slots") or {}).keys()),
                    "semantic_entity_fields": sorted((item.get("semantic_entities") or {}).keys()),
                    "evidence_fields": sorted({
                        str(evidence.get("field"))
                        for evidence in (item.get("semantic_evidence") or [])
                        if isinstance(evidence, dict) and evidence.get("field")
                    }),
                }
                for item in planned_tasks
            ], separators=(",", ":")),
        )
        clarification_messages = [
            str(item.get("clarification_question", "")).strip()
            for item in planned_tasks
            if item.get("status") in {"CLARIFICATION_REQUIRED", "REJECTED"}
            and str(item.get("clarification_question", "")).strip()
        ]
        backend_or_frontend_calls: list[dict[str, Any]] = []
        call_to_tasks: dict[str, list[str]] = {}
        runtime_owned_ids: list[str] = []
        direct_messages: list[str] = []

        for planned in planned_tasks:
            dispatch_handler = str(planned.get("dispatch_handler", ""))
            task_class = str(planned.get("task_class", ""))
            if planned.get("status") == "RESOURCE_READY":
                if dispatch_handler != "registered_resource" or task_class != "resource":
                    planned["status"] = "REJECTED"
                    planned["reason_code"] = "TASK_DISPATCH_OWNER_MISMATCH"
                    clarification_messages.append("I found the information request, but its registered resource route could not be validated.")
                    continue
                resource_text = render_registered_resource_response(
                    str(planned.get("resource_id", "")),
                    planned.get("resource_references", {}),
                    current_text,
                )
                if resource_text:
                    direct_messages.append(resource_text)
                    planned["status"] = "RESOURCE_SERVED"
                else:
                    planned["status"] = "UNAVAILABLE"
                    planned["reason_code"] = "REGISTERED_RESOURCE_EMPTY"
                    clarification_messages.append(
                        "I found the relevant ORAIflow information area, but its maintained explanation is not available right now. Which part would you like me to explain?"
                    )
                continue
            if planned.get("status") != "PLANNED":
                continue

            # The human policy selects an approved task-class owner. The strict
            # policy model enforces this class/handler relationship; this runtime
            # guard prevents a stale or malformed task plan from crossing owners.
            allowed_dispatch = {
                "mcp_read": "occf_read",
                "presentation": "registered_presentation",
                "workflow": "registered_workflow",
                "frontend_action": "registered_frontend_action",
            }
            if allowed_dispatch.get(task_class) != dispatch_handler:
                planned["status"] = "REJECTED"
                planned["reason_code"] = "TASK_DISPATCH_OWNER_MISMATCH"
                clarification_messages.append("I understood the request, but its registered execution route could not be validated. No action was executed.")
                continue

            task_id = str(planned.get("task_id", ""))
            if dispatch_handler == "registered_presentation":
                if not planned.get("presentation_id"):
                    planned["status"] = "REJECTED"
                    planned["reason_code"] = "PRESENTATION_CONTRACT_MISSING"
                    clarification_messages.append("The requested information view has no registered presentation in this version.")
                    continue
                canonical = canonical_intent_step(str(planned.get("intent_id", "")), state)
                if canonical is None:
                    continue
                canonical_calls = getattr(canonical, "tool_calls", None) or []
                if not canonical_calls:
                    # A presentation task is not complete merely because the
                    # canonical renderer returned explanatory text. Keep the
                    # text, but record that no browser presentation receipt was
                    # scheduled so the task cannot remain falsely PLANNED.
                    planned["status"] = "UNAVAILABLE"
                    planned["reason_code"] = "PRESENTATION_NOT_SCHEDULED"
                    clarification_messages.append(str(getattr(canonical, "content", "") or "The verified information could not be displayed in this view."))
                    continue
                for call in canonical_calls:
                    if not isinstance(call, dict) or not call.get("id"):
                        continue
                    call_id = str(call["id"])
                    call_to_tasks[call_id] = [task_id]
                    planned.setdefault("tool_call_ids", []).append(call_id)
                    if call.get("name") in {item.name for item in TOOLS}:
                        runtime_owned_ids.append(call_id)
                    backend_or_frontend_calls.append(call)
                continue

            tool_name = str(planned.get("tool_name", ""))
            capability_id = str(planned.get("capability_id", ""))
            if not tool_name or not capability_id:
                planned["status"] = "REJECTED"
                planned["reason_code"] = "REGISTERED_TOOL_MAPPING_MISSING"
                clarification_messages.append("I found a relevant ORAIflow topic, but its registered execution route is not available in this application version.")
                continue
            call_id = f"oraiflow-planned-{uuid.uuid4().hex}"
            arguments = dict(planned.get("arguments", {}))
            arguments.update(assessment_tool_arguments(planned))
            planned.setdefault("tool_call_ids", []).append(call_id)
            call_to_tasks[call_id] = [task_id]
            backend_or_frontend_calls.append({
                "name": tool_name,
                "args": arguments,
                "id": call_id,
                "type": "tool_call",
            })

        task_plan = _task_plan_records(
            planned_tasks, model_deadline_epoch=model_deadline_epoch,
        )

        # Validate the runtime-created calls through the established OCCF and
        # transfer/frontend guards. The model no longer supplies tool names or
        # executable arguments at this boundary.
        if backend_or_frontend_calls:
            proposed = AIMessage(content="", tool_calls=backend_or_frontend_calls)
            validated_calls = _validate_proposed_tool_calls(
                proposed, state.get("messages", []), runtime_context,
            )
            final_calls = getattr(validated_calls, "tool_calls", None) or []
            if not final_calls:
                rejected_task_ids = {
                    str(task_id) for task_ids in call_to_tasks.values() for task_id in task_ids
                }
                for planned in planned_tasks:
                    if str(planned.get("task_id", "")) in rejected_task_ids and planned.get("status") == "PLANNED":
                        planned["status"] = "REJECTED"
                        planned["reason_code"] = "OCCF_EXECUTION_VALIDATION_REJECTED"
                task_plan = _task_plan_records(
                    planned_tasks, model_deadline_epoch=model_deadline_epoch,
                )
                content = str(getattr(validated_calls, "content", "") or "").strip()
                planned_message = validated_calls if content else AIMessage(
                    content="I couldn't safely prepare that request using the registered ORAIflow contract. No operation was executed."
                )
                planned_message = _attach_task_plan(planned_message, task_plan, call_to_tasks)
                return emit(planned_message)
            else:
                accepted_call_ids = {
                    str(call.get("id", "")) for call in final_calls if isinstance(call, dict)
                }
                rejected_call_ids = set(call_to_tasks) - accepted_call_ids
                rejected_task_ids = {
                    str(task_id) for call_id in rejected_call_ids for task_id in call_to_tasks.get(call_id, [])
                }
                for planned in planned_tasks:
                    if str(planned.get("task_id", "")) in rejected_task_ids and planned.get("status") == "PLANNED":
                        planned["status"] = "REJECTED"
                        planned["reason_code"] = "OCCF_EXECUTION_VALIDATION_REJECTED"
                task_plan = _task_plan_records(
                    planned_tasks, model_deadline_epoch=model_deadline_epoch,
                )
                final_names = {str(call.get("name", "")) for call in final_calls if isinstance(call, dict)}
                backend_names = {item.name for item in TOOLS}
                # Mixed frontend and backend work is staged: reads execute
                # first, then the remaining UI task is emitted on re-entry.
                backend_only = [call for call in final_calls if isinstance(call, dict) and call.get("name") in backend_names]
                frontend_only = [call for call in final_calls if isinstance(call, dict) and call.get("name") not in backend_names]
                if backend_only:
                    next_metadata = copy.deepcopy(getattr(validated_calls, "additional_kwargs", {}) or {})
                    next_metadata["oraiflow_runtime_owned_read_ids"] = [str(call["id"]) for call in backend_only if str(call.get("id")) in runtime_owned_ids or str(call.get("id", "")).startswith("oraiflow-planned-")]
                    next_message = AIMessage(content="", tool_calls=backend_only, additional_kwargs=next_metadata)
                    next_message = _attach_task_plan(next_message, task_plan, call_to_tasks)
                    return emit(next_message)
                if frontend_only:
                    next_message = AIMessage(content="", tool_calls=frontend_only, additional_kwargs=copy.deepcopy(getattr(validated_calls, "additional_kwargs", {}) or {}))
                    return emit(_attach_task_plan(next_message, task_plan, call_to_tasks))

        # A presentation may already have been bound from a previously verified
        # source result, or the plan may need a targeted clarification.
        if direct_messages and not backend_or_frontend_calls:
            content_parts = [*direct_messages, *clarification_messages]
            message = AIMessage(content="\n\n".join(dict.fromkeys(item for item in content_parts if item))[:5000])
            message = _attach_task_plan(message, task_plan)
            message = attach_navigation_clarification_from_plan(message, planned_tasks, current_text)
            return emit(message)
        if planned_tasks and all(item.get("status") != "PLANNED" for item in planned_tasks):
            content = " ".join(dict.fromkeys(clarification_messages)) or "I couldn't safely complete that request. Could you clarify what you'd like me to do?"
            message = AIMessage(content=content[:1200])
            message = _attach_task_plan(message, task_plan)
            message = attach_navigation_clarification_from_plan(message, planned_tasks, current_text)
            return emit(message)
        if clarification_messages and not backend_or_frontend_calls:
            message = AIMessage(content=" ".join(dict.fromkeys(clarification_messages))[:1200])
            message = _attach_task_plan(message, task_plan)
            message = attach_navigation_clarification_from_plan(message, planned_tasks, current_text)
            return emit(message)

        # A plan with no executable call is not allowed to disappear silently.
        message = AIMessage(content="I understood the general request, but need one more detail before I can continue. What would you like me to focus on?")
        message.additional_kwargs["oraiflow_terminal_error"] = {"code": "NO_EXECUTABLE_TASK_PLAN", "retryable": True}
        return emit(_attach_task_plan(message, task_plan))

    async def execute_tools(state: ORAIState) -> dict[str, Any]:
        last = state["messages"][-1]
        calls = getattr(last, "tool_calls", None) or []
        metadata = getattr(last, "additional_kwargs", {}) or {}
        runtime_owned_ids = {
            str(item) for item in metadata.get("oraiflow_runtime_owned_read_ids", [])
            if isinstance(item, str)
        }
        task_call_map = metadata.get("oraiflow_task_call_map", {})
        task_call_map = task_call_map if isinstance(task_call_map, dict) else {}

        def correlate_result(tool_message: ToolMessage) -> ToolMessage:
            call_id = str(getattr(tool_message, "tool_call_id", "") or "")
            task_ids = task_call_map.get(call_id, [])
            if not isinstance(task_ids, list):
                task_ids = [task_ids] if task_ids else []
            existing = dict(getattr(tool_message, "additional_kwargs", {}) or {})
            if task_ids:
                existing["oraiflow_task_ids"] = [str(value) for value in task_ids if value]
                if len(existing["oraiflow_task_ids"]) == 1:
                    existing["oraiflow_task_id"] = existing["oraiflow_task_ids"][0]
            return tool_message.model_copy(update={"additional_kwargs": existing})
        internal_reads = [
            call for call in calls
            if isinstance(call, dict) and str(call.get("id", "")) in runtime_owned_ids
        ]
        model_calls = [
            call for call in calls
            if not (isinstance(call, dict) and str(call.get("id", "")) in runtime_owned_ids)
        ]
        results = []
        def unavailable_tool_result(call: dict[str, Any], *, code: str) -> ToolMessage:
            tool_name = str(call.get("name", ""))
            registered = capability_by_name(tool_name, kind="mcp")
            if registered:
                capability_id = registered[0]
                envelope = create_result_envelope(
                    capability_id,
                    "UNAVAILABLE",
                    error={"code": code, "retryable": True, "message": "The information source is temporarily unavailable."},
                    trace_id=current_trace_id(),
                )
                content = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
            else:
                # The OCCF planner should make this unreachable. Preserve the
                # tool-call lifecycle so LangGraph can terminate the task; the
                # task correlator will mark the unregistered receipt failed.
                content = json.dumps({"error": "UNREGISTERED_CAPABILITY", "retryable": False})
            return correlate_result(ToolMessage(
                content=content,
                name=tool_name or "unknown_tool",
                tool_call_id=str(call.get("id", "")),
            ))

        if model_calls:
            # Defensive compatibility path. The structured assessment path
            # should not emit executable calls; any validated call that reaches
            # ToolNode still remains protected by its OCCF input schema.
            model_message = AIMessage(
                content=getattr(last, "content", ""),
                tool_calls=model_calls,
            )
            model_state = dict(state)
            model_state["messages"] = [*state["messages"][:-1], model_message]
            try:
                regular_results = await ToolNode(TOOLS).ainvoke(model_state)
                results.extend(correlate_result(item) for item in regular_results.get("messages", []))
            except Exception:
                logger.exception("agent_tool_node_failed trace_id=%s call_count=%d", current_trace_id(), len(model_calls))
                results.extend(unavailable_tool_result(call, code="TOOL_NODE_UNAVAILABLE") for call in model_calls)

        for call in internal_reads:
            # These are canonical-intent, presentation-preflight or workflow
            # continuations created by the runtime, not LLM-proposed actions.
            # They still use the same OCCF/MCP adapter and canonical validation.
            tool_name = str(call.get("name", ""))
            registered = capability_by_name(tool_name, kind="mcp")
            if not registered:
                logger.error("agent_internal_capability_unregistered trace_id=%s tool=%s", current_trace_id(), tool_name)
                results.append(unavailable_tool_result(call, code="UNREGISTERED_CAPABILITY"))
                continue
            try:
                content = await _call_read_capability(tool_name, call.get("args") or {})
            except Exception:
                logger.exception("agent_internal_read_failed trace_id=%s capability=%s", current_trace_id(), registered[0])
                results.append(unavailable_tool_result(call, code="MCP_EXECUTION_EXCEPTION"))
                continue
            results.append(correlate_result(ToolMessage(
                content=content,
                name=tool_name,
                tool_call_id=str(call.get("id", "")),
            )))
        if internal_reads or model_calls:
            return {"messages": results}
        return await ToolNode(TOOLS).ainvoke(state)


    async def safe_call_model(state: ORAIState) -> dict[str, Any]:
        """Turn unexpected graph-node exceptions into an explicit terminal reply."""
        try:
            return await call_model(state)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("agent_runtime_unhandled_error trace_id=%s", current_trace_id())
            try:
                turn_context, _candidates = _build_agent_turn_context(
                    state, latest_human_text(state.get("messages", [])),
                )
                message = AIMessage(
                    content="I couldn't complete that request because an internal ORAIflow service encountered an error. No financial action was taken. Please try again.",
                    additional_kwargs={
                        "oraiflow_terminal_error": {"code": "AGENT_RUNTIME_ERROR", "retryable": True},
                    },
                )
                return {"messages": [_attach_agent_turn_contract(message, turn_context, state.get("messages", []))]}
            except Exception:
                logger.exception("agent_runtime_terminal_response_failed trace_id=%s", current_trace_id())
                return {"messages": [AIMessage(
                    content="I couldn't complete that request just now. No financial action was taken. Please try again.",
                    additional_kwargs={"oraiflow_terminal_error": {"code": "AGENT_RUNTIME_ERROR", "retryable": True}},
                )]}

    graph = StateGraph(ORAIState)
    graph.add_node("agent", safe_call_model)
    graph.add_node("tools", execute_tools)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer)


def to_langchain_messages(history: list[dict[str, str]], message: str):
    """Convert supported transcript messages into LangChain message objects."""
    result = []
    for item in history[-10:]:
        role = item.get("role", "")
        content = redact_for_model(str(item.get("content", ""))[:4000])
        if role == "user":
            result.append(HumanMessage(content=content))
        elif role == "assistant":
            result.append(AIMessage(content=content))
    result.append(HumanMessage(content=redact_for_model(message[:4000])))
    return result


async def run_llm_chat(message: str, history: list[dict[str, str]]) -> tuple[str, list[str]]:
    """Run a model-backed chat turn and return its final customer-facing response."""
    graph = build_graph()
    result = await graph.ainvoke(
        {"messages": to_langchain_messages(history, message)},
        {"recursion_limit": 10},
    )
    final = result["messages"][-1]
    content = final.content
    if isinstance(content, list):
        content = message_content_to_text(content)
    used = []
    for item in result["messages"]:
        for call in getattr(item, "tool_calls", []) or []:
            used.append(call.get("name", "MCP tool"))
    return str(content or "I could not produce a response."), sorted(set(used))


async def run_demo_chat(message: str) -> tuple[str, list[str]]:
    """Run the deterministic synthetic-data demo chat path without an external model."""
    lower = message.lower()
    if any(term in lower for term in ("balance", "account", "accounts", "money", "how much")):
        envelope = json.loads(await _call_read_capability("list_customer_accounts"))
        if envelope.get("outcome") not in {"SUCCESS", "EMPTY"}:
            return "I couldn't retrieve your account information just now. Please try again.", ["list_customer_accounts"]
        accounts = envelope["data"]
        lines = [
            f"• **{a['account_name']}** · {a['currency']} {int(a['balance_minor']) / 100:,.2f}"
            for a in accounts
        ]
        return "Here is your current account overview from the banking capability service:\n\n" + ("\n".join(lines) if lines else "No accounts were returned."), ["list_customer_accounts"]
    transfer_history_query = any(phrase in lower for phrase in (
        "did my", "has my", "was my", "was that transfer", "recent transfer",
        "completed transfer", "settled transfer", "transfer history",
        "payment status", "transfer status", "check the transfer",
        "was the transfer", "did the transfer", "go through", "went through",
    ))
    if transfer_history_query or any(term in lower for term in ("transaction", "spending", "spent", "activity", "recent", "history")):
        envelope = json.loads(await _call_read_capability("get_recent_transactions", {"limit": 6}))
        if envelope.get("outcome") not in {"SUCCESS", "EMPTY"}:
            return "I couldn't retrieve your recent transactions just now. Please try again.", ["get_recent_transactions"]
        rows = envelope["data"]
        lines = [
            f"• **{r['description']}** · {'+' if int(r['amount_minor']) >= 0 else '−'}{r['currency']} {abs(int(r['amount_minor'])) / 100:,.2f} · {r['status']}"
            for r in rows
        ]
        return "Here are your latest transactions:\n\n" + ("\n".join(lines) if lines else "No transactions found."), ["get_recent_transactions"]
    if any(term in lower for term in ("transfer", "send", "pay", "payment")):
        return (
            "I can help you prepare a transfer, but I cannot execute it from chat. "
            "Open **New transfer**, review the exact recipient and amount, then complete "
            "the clearly labelled demo approval step. The payment will be handled by a Temporal workflow.",
            [],
        )
    return (
        "Welcome to ORAIflow. I can look up your accounts, balances and recent transactions "
        "through the mandatory MCP banking interface. I can also guide you through a "
        "separately confirmed demo transfer. Try asking **“Show my balances”** or **“What are my recent transactions?”**",
        [],
    )
