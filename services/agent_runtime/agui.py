"""AG-UI transport adapter for streaming the Agent Runtime to the customer workspace."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import sys
import time
import uuid
import json
from typing import Any

from ag_ui.core import (
    EventType,
    RunErrorEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from ag_ui.core.types import RunAgentInput
from ag_ui.encoder import EventEncoder
from copilotkit import LangGraphAGUIAgent
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from copilotkit import CopilotKitState


from services.agent_runtime.action_response_guard import (
    has_successful_frontend_ack_after_latest_user,
)
from services.agent_runtime.agent import (
    _call_read_capability,
    _connector_failure_message,
    _frontend_action_ack_message,
    _build_agent_turn_context,
    _attach_agent_turn_contract,
    _selected_account_context,
    _pending_navigation_message,
    latest_human_text,
    build_graph,
    canonical_intent_step,
    _start_explicit_transfer_workflow,
    _handle_transfer_workflow,
    run_demo_chat,
    _workspace_navigation_step,
)
from shared.contracts.registry import capability_by_name, get_registry, parse_result_envelope
from shared.pending_task import message_type, message_value
from services.agent_runtime.intent_contracts import classify_canonical_read_intent
from services.agent_runtime.academy import academy_direct_guidance_for_request
from services.agent_runtime.workspace_knowledge import workspace_help_for_request
from services.agent_runtime.assistant_capabilities import assistant_capability_help_for_request
from services.agent_runtime.observability import current_trace_id, reset_trace_id, set_trace_id
from services.agent_runtime.streaming import (
    iter_with_keepalives,
    filter_internal_assistant_messages,
    synthesize_terminal_assistant_message,
    stream_text_events,
)

logger = logging.getLogger("oraiflow.agent_runtime.agui")


class ORAIState(CopilotKitState):
    """Typed graph state used by the local AG-UI demonstration adapter."""

    pass


def _make_snapshot_tool_result_events(tool_call_id: str, message_id: str, content: Any):
    """Encode a snapshot-only MCP result as its normal AG-UI tool message event."""
    if not isinstance(content, (str, list)):
        content = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    return [
        ToolCallResultEvent(
            type=EventType.TOOL_CALL_RESULT,
            message_id=message_id,
            tool_call_id=tool_call_id,
            content=content,
            role="tool",
        )
    ]


def _app_context(state):
    """Use the same validated, descriptive UI context as the model-backed graph."""
    return _selected_account_context(state)


def _snapshot_recovery_tool_names() -> set[str]:
    """Whitelist only registered CopilotKit presentation and frontend actions."""
    registry = get_registry()
    names = set((registry.get("presentations") or {}).keys())
    for contract in (registry.get("capabilities") or {}).values():
        if isinstance(contract, dict) and contract.get("kind") in {"frontend", "mcp"}:
            name = str(contract.get("name") or "").strip()
            if name:
                names.add(name)
    return names


def _snapshot_recovery_tool_result_names() -> set[str]:
    """Whitelist registered MCP tools whose result cards are rendered by useRenderTool."""
    registry = get_registry()
    return {
        str(contract.get("name")).strip()
        for contract in (registry.get("capabilities") or {}).values()
        if isinstance(contract, dict)
        and contract.get("kind") == "mcp"
        and str(contract.get("name") or "").strip()
    }


def _make_snapshot_tool_call_events(
    tool_call_id: str,
    tool_call_name: str,
    parent_message_id: str,
    arguments_json: str,
):
    """Encode a validated snapshot-only UI call as its normal AG-UI lifecycle."""
    return [
        ToolCallStartEvent(
            type=EventType.TOOL_CALL_START,
            tool_call_id=tool_call_id,
            tool_call_name=tool_call_name,
            **({"parent_message_id": parent_message_id} if parent_message_id else {}),
        ),
        ToolCallArgsEvent(
            type=EventType.TOOL_CALL_ARGS,
            tool_call_id=tool_call_id,
            delta=arguments_json,
        ),
        ToolCallEndEvent(
            type=EventType.TOOL_CALL_END,
            tool_call_id=tool_call_id,
        ),
    ]


def build_demo_agui_graph(checkpointer=None):
    """Build the deterministic demo graph exposed through the AG-UI adapter."""

    async def _respond(state: ORAIState):
        # The model-backed graph has this one-shot guard in both its router
        # and call_model path. Mirror it in demo mode: a browser draft receipt
        # is acknowledged once, and a later AG-UI re-entry must not fall
        # through to the generic demo assistant (which could emit account
        # selection/navigation after the draft is already open).
        messages = list(state.get("messages", []))
        if has_successful_frontend_ack_after_latest_user(messages):
            logger.info("demo_frontend_ack_replay_suppressed trace_id=%s", current_trace_id())
            return {"messages": []}
        messages = state.get("messages", [])
        last_message = messages[-1] if messages else None
        # CopilotKit runs frontend tools in the browser and starts a follow-up
        # AG-UI run containing the ToolMessage. Acknowledge that result rather
        # than issuing the same frontend tool call again.
        if last_message is not None and message_type(last_message) == "tool":
            import json

            raw_result = str(message_value(last_message, "content", ""))
            try:
                result = json.loads(raw_result)
            except (TypeError, ValueError):
                result = {}
            tool_name = message_value(last_message, "name", "")
            registered = capability_by_name(tool_name, kind="frontend")
            try:
                if not registered:
                    raise ValueError("Unregistered frontend tool result")
                envelope = parse_result_envelope(registered[0], raw_result)
            except Exception:
                logger.warning(
                    "frontend_result_contract_rejected trace_id=%s tool=%s",
                    current_trace_id(),
                    tool_name,
                )
                return {
                    "messages": [
                        AIMessage(
                            content="I couldn't verify that application action completed. Please check the current screen and try again if needed."
                        )
                    ]
                }
            outcome = envelope.get("outcome")
            result = envelope.get("data") if outcome == "SUCCESS" else {}
            status = result.get("status") if isinstance(result, dict) else None
            if outcome in {"UNAVAILABLE", "INVALID_RESPONSE", "INVALID_INPUT"}:
                reply = "I couldn't complete that application action just now. Please try again."
            elif outcome == "REJECTED":
                reply = str(
                    (envelope.get("error") or {}).get("message")
                    or "The application rejected that action."
                )
            elif status == "selected":
                reply = "Done — I opened Activity with that account selected. The Overview's Recent Activity remains global."
            elif status == "opened":
                reply = f"Done — I opened the {result.get('workspace', 'requested')} workspace."
            elif status == "draft_opened":
                reply = "The editable transfer draft is open. Nothing has been previewed, approved or submitted."
            elif status == "cleared":
                reply = "Done — the Activity view now includes all accounts."
            elif status == "rejected":
                reply = f"I couldn't complete that action: {result.get('reason', 'the application rejected it')}."
            else:
                reply = "The application action returned. What would you like to do next?"
            receipt = {
                "capability_id": registered[0],
                "tool_call_id": str(message_value(last_message, "tool_call_id", "")),
                "status": str(status or outcome or ""),
            }
            if outcome == "SUCCESS" and status in {"draft_opened", "opened", "selected", "cleared"}:
                return {"messages": [_frontend_action_ack_message(reply, receipt)]}
            return {"messages": [AIMessage(content=reply)]}
        latest = next((m for m in reversed(messages) if message_type(m) == "human"), None)
        if latest is None:
            return {
                "messages": [AIMessage(content="What would you like to explore in your accounts?")]
            }
        latest_text = str(message_value(latest, "content", ""))
        text = latest_text.lower()
        actions = (state.get("copilotkit") or {}).get("actions") or []
        action_names = {
            action.get("name", "") if isinstance(action, dict) else getattr(action, "name", "")
            for action in actions
        }
        context = _app_context(state)
        transfer_state = {"messages": list(messages), "copilotkit": state.get("copilotkit") or {}}
        transfer_step = _start_explicit_transfer_workflow(transfer_state, context)
        if transfer_step is None:
            transfer_step = _handle_transfer_workflow(transfer_state, context)
        if transfer_step is not None:
            # The demo provider uses the same typed transfer state machine as
            # the model-backed graph. Only runtime-owned, registered MCP reads
            # are executed here; frontend draft actions are returned to the
            # browser and remain editable/customer-controlled.
            for _ in range(3):
                calls = getattr(transfer_step, "tool_calls", None) or []
                if not calls:
                    break
                call = calls[0]
                tool_name = str(call.get("name", ""))
                call_id = str(call.get("id") or f"demo-{uuid.uuid4().hex}")
                if tool_name not in {"search_contacts", "list_customer_accounts"}:
                    break
                raw_result = await _call_read_capability(tool_name, call.get("args") or {})
                tool_result = ToolMessage(
                    name=tool_name,
                    tool_call_id=call_id,
                    content=raw_result,
                )
                transfer_state = {
                    **transfer_state,
                    "messages": [*transfer_state["messages"], transfer_step, tool_result],
                }
                transfer_step = _handle_transfer_workflow(transfer_state, context)
                if transfer_step is None:
                    break
            return {
                "messages": [
                    transfer_step
                    or AIMessage(
                        content="I couldn't safely continue the transfer preparation. No draft was opened."
                    )
                ]
            }

        # Confirmation and destination replies must resume in demo mode just
        # as they do in the model-backed graph. In particular, "ok" must
        # dispatch the pending registered navigation action rather than falling
        # through to the generic demo chatbot.
        navigation_continuation = _pending_navigation_message(state)
        if navigation_continuation is not None:
            return {"messages": [navigation_continuation]}

        # The demo/provider fallback consumes the same registry-backed help
        # catalogues as the model-backed graph. It must not route ordinary
        # workspace or Academy questions into the broad demo fallback.
        workspace_help = workspace_help_for_request(latest_text)
        if workspace_help:
            return {"messages": [AIMessage(content=workspace_help)]}
        academy_help = academy_direct_guidance_for_request(latest_text)
        if academy_help:
            return {"messages": [AIMessage(content=academy_help)]}
        capability_help = assistant_capability_help_for_request(latest_text)
        if capability_help:
            return {"messages": [AIMessage(content=capability_help)]}

        navigation_step = _workspace_navigation_step(latest_text, state)
        if navigation_step is not None:
            return {"messages": [navigation_step]}
        if "clear_account_selection" in action_names and any(
            phrase in text
            for phrase in (
                "clear account filter",
                "all account activity",
                "activity for all accounts",
                "show all transactions",
            )
        ):
            return {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "clear_account_selection",
                                "args": {},
                                "id": f"oraiflow-clear-filter-{uuid.uuid4().hex}",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            }
        selection_intent = any(
            phrase in text
            for phrase in (
                "select ",
                "switch to ",
                "open activity for ",
                "show activity for ",
                "show transactions for ",
                "activity on ",
            )
        )
        if "select_account" in action_names and selection_intent:
            import json

            account_result = parse_result_envelope(
                "banking.accounts.list", await _call_read_capability("list_customer_accounts")
            )
            accounts = (
                account_result.get("data", [])
                if account_result.get("outcome") in {"SUCCESS", "EMPTY"}
                else []
            )
            match = next((a for a in accounts if a.get("account_name", "").lower() in text), None)
            if match:
                return {
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "select_account",
                                    "args": {"account_id": match["account_id"]},
                                    "id": f"oraiflow-select-account-{uuid.uuid4().hex}",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    ]
                }
        context = _app_context(state)
        selected_name = context.get("selected_account_name") if isinstance(context, dict) else None
        query = latest_text
        if selected_name and selected_name != "All accounts":
            query = f"{query} (current selected account: {selected_name})"
        explicit_navigation = any(
            phrase in text
            for phrase in ("open ", "go to ", "navigate to ", "take me to ", "switch workspace to ")
        )
        # Resolve common human shorthand before any model-generated route can
        # reach the browser. The frontend independently validates the alias.

        # Deterministic intents use the exact same registry-driven execution
        # and presentation step as the model-backed graph. Demo mode simply
        # performs the MCP tool call itself, then re-enters the same binder.
        canonical_intent = classify_canonical_read_intent(latest_text)
        if not explicit_navigation and canonical_intent:
            canonical_state = {"messages": messages, "copilotkit": state.get("copilotkit") or {}}
            step = canonical_intent_step(canonical_intent, canonical_state)
            proposed_calls = getattr(step, "tool_calls", None) or []
            if proposed_calls:
                call = proposed_calls[0]
                mcp_capability = capability_by_name(call.get("name", ""), kind="mcp")
                if mcp_capability:
                    raw_result = await _call_read_capability(call["name"], call.get("args") or {})
                    tool_result = ToolMessage(
                        name=call["name"],
                        tool_call_id=call.get("id", f"demo-{uuid.uuid4().hex}"),
                        content=raw_result,
                    )
                    canonical_state = {
                        "messages": [*messages, tool_result],
                        "copilotkit": state.get("copilotkit") or {},
                    }
                    step = canonical_intent_step(canonical_intent, canonical_state)
            return {"messages": [step]}

        if explicit_navigation and "navigate_workspace" in action_names:
            destinations = {
                "overview": "Overview",
                "dashboard": "Overview",
                "home": "Overview",
                "accounts": "Accounts",
                "payments": "Payments",
                "cards": "Cards",
                "contacts": "Contacts",
                "contact": "Contacts",
                "beneficiaries": "Contacts",
                "payees": "Contacts",
                "activity": "Activity",
                "transactions": "Activity",
                "ai assistant": "AI assistant",
                "assistant": "AI assistant",
                "controls": "Controls & audit",
                "audit": "Controls & audit",
                "audit trail": "Controls & audit",
                "governance": "Controls & audit",
                "security": "Controls & audit",
                "controls audit": "Controls & audit",
            }
            destination = next(
                (label for term, label in destinations.items() if term in text), None
            )
            if destination:
                action_id = f"nav-{uuid.uuid4().hex}"
                return {
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "navigate_workspace",
                                    "args": {"workspace": destination, "action_id": action_id},
                                    "id": action_id,
                                    "type": "tool_call",
                                }
                            ],
                        )
                    ]
                }

        if (
            selected_name
            and selected_name != "All accounts"
            and any(term in query.lower() for term in ("balance", "how much", "money"))
        ):
            account_id = context.get("selected_account_id")
            if account_id:
                balance_result = parse_result_envelope(
                    "banking.accounts.balance",
                    await _call_read_capability("get_account_balance", {"account_id": account_id}),
                )
                if balance_result.get("outcome") not in {"SUCCESS", "EMPTY"}:
                    return {"messages": [_connector_failure_message("get_account_balance")]}
                balance = balance_result["data"]
                reply = f"{selected_name} has {balance['currency']} {int(balance['balance_minor']) / 100:,.2f} available."
            else:
                reply, _tools = await run_demo_chat(query)
        else:
            reply, _tools = await run_demo_chat(query)
        return {"messages": [AIMessage(content=reply)]}

    async def respond(state: ORAIState):
        try:
            result = await _respond(state)
        except Exception:
            # Never expose MCP/session details or internal orchestration text to
            # the customer. The exception is logged without arguments/data.
            logger.exception("demo_agent_request_failed trace_id=%s", current_trace_id())
            result = {
                "messages": [
                    AIMessage(
                        content="I couldn't retrieve the requested banking information just now. No financial action was taken. Please try again."
                    )
                ]
            }

        # Demo and model-backed providers share the exact same request and
        # response receipt contract. This wrapper runs once at the AG-UI graph
        # boundary, so acknowledgements, clarifications, tool requests and final
        # text all carry one validated per-turn context and stable message ID.
        messages = list(result.get("messages", [])) if isinstance(result, dict) else []
        if not messages:
            return result
        try:
            turn_context, _candidates = _build_agent_turn_context(
                state, latest_human_text(state.get("messages", []))
            )
            contracted = [
                (
                    _attach_agent_turn_contract(message, turn_context, state.get("messages", []))
                    if isinstance(message, AIMessage)
                    else message
                )
                for message in messages
            ]
            result["messages"] = contracted
            logger.info(
                "agent_turn_contract_prepared trace_id=%s turn_id=%s provider=demo workspace=%s mcp=%d frontend=%d presentations=%d candidates=%d",
                current_trace_id(),
                turn_context.turn_id,
                turn_context.workspace_id or "unknown",
                len(turn_context.available_mcp_capability_ids),
                len(turn_context.available_frontend_capability_ids),
                len(turn_context.available_presentation_ids),
                len(turn_context.intent_candidates),
            )
        except Exception:
            # Contract construction is fail-closed for model-visible tools but
            # must never lose the already-created customer response. Emit a
            # diagnostic and let the AG-UI stream retain its original message.
            logger.exception(
                "demo_agent_turn_contract_attachment_failed trace_id=%s", current_trace_id()
            )
        return result

    graph = StateGraph(ORAIState)
    graph.add_node("respond", respond)
    graph.add_edge(START, "respond")
    # Frontend tool calls are returned to CopilotKit. The browser runs the
    # matching useFrontendTool handler and CopilotKit starts the follow-up run.
    graph.add_edge("respond", END)
    return graph.compile(checkpointer=checkpointer)


async def start_agui_runtime(app):
    """Open a schema-scoped PostgreSQL checkpointer and compile the agent once."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    connection_string = os.getenv("AGENT_CHECKPOINT_DATABASE_URL", "").strip()
    if not connection_string:
        raise RuntimeError(
            "AGENT_CHECKPOINT_DATABASE_URL must point to the restricted oraiflow_agent schema"
        )
    checkpointer_context = AsyncPostgresSaver.from_conn_string(connection_string)
    checkpointer = await checkpointer_context.__aenter__()
    try:
        await checkpointer.setup()
        provider = os.getenv("LLM_PROVIDER", "demo").lower()
        graph = (
            build_demo_agui_graph(checkpointer) if provider == "demo" else build_graph(checkpointer)
        )
        app.state.agui_agent = LangGraphAGUIAgent(
            name="oraiflow_agent",
            description="ORAIflow read-only banking assistant. It can explain accounts and activity but cannot approve or execute financial actions.",
            graph=graph,
        )
        app.state.agui_checkpointer_context = checkpointer_context
    except BaseException:
        await checkpointer_context.__aexit__(*sys.exc_info())
        raise


async def stop_agui_runtime(app):
    """Stop AG-UI runtime resources and close their application-scoped clients."""
    context = getattr(app.state, "agui_checkpointer_context", None)
    if context is not None:
        await context.__aexit__(None, None, None)


def register_agui_routes(app):
    """Register the AG-UI streaming routes on the supplied FastAPI application."""
    token = os.getenv("AGENT_RUNTIME_INTERNAL_TOKEN", "local-agent-runtime-token-change-me")

    @app.post("/agui")
    async def agui_run(input_data: RunAgentInput, request: Request):
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {token}"):
            raise HTTPException(status_code=401, detail="Unauthorized AG-UI runtime caller")

        agent = getattr(request.app.state, "agui_agent", None)
        if agent is None:
            raise HTTPException(status_code=503, detail="ORAI AG-UI agent is not initialized")
        encoder = EventEncoder(accept=request.headers.get("accept"))
        trace_id = uuid.uuid4().hex[:12]

        async def events():
            trace_token = set_trace_id(trace_id)
            started_at = time.perf_counter()
            event_counts = {}
            first_event_ms = None
            first_message_ms = None
            first_tool_ms = None
            run_finished = False
            emitted = 0
            keepalive_count = 0
            logger.info("agui_run_started trace_id=%s", trace_id)
            try:
                # Keep the SSE connection alive while an interpreter or provider
                # is working. The helper leaves the pending __anext__ operation
                # running when a heartbeat interval elapses.
                keepalive_seconds = max(5.0, float(os.getenv("AGENT_AGUI_KEEPALIVE_SECONDS", "15")))
                # Defense in depth: nested internal model runs suppress inherited
                # callbacks; this transport filter also prevents an assessment
                # envelope from becoming a visible assistant message if a
                # provider/adapter accidentally emits it as text.
                initial_messages = list(getattr(input_data, "messages", []) or [])
                initial_message_ids = set()
                for item in initial_messages:
                    message_id = (
                        item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
                    )
                    if message_id:
                        initial_message_ids.add(str(message_id))

                def make_text_events(message_id: str, content: str):
                    return [
                        TextMessageStartEvent(
                            type=EventType.TEXT_MESSAGE_START,
                            message_id=message_id,
                            role="assistant",
                        ),
                        TextMessageContentEvent(
                            type=EventType.TEXT_MESSAGE_CONTENT,
                            message_id=message_id,
                            delta=content,
                        ),
                        TextMessageEndEvent(
                            type=EventType.TEXT_MESSAGE_END,
                            message_id=message_id,
                        ),
                    ]

                def make_missing_message_event():
                    return RunErrorEvent(
                        type=EventType.RUN_ERROR,
                        message="ORAIflow finished without a customer-visible response. Please try again. No financial action was taken.",
                        code="NO_CUSTOMER_MESSAGE",
                    )

                def log_snapshot_fallback(message_id: str):
                    logger.warning(
                        "agui_snapshot_message_fallback trace_id=%s message_id=%s",
                        trace_id,
                        message_id,
                    )

                def log_tool_call_fallback(tool_name: str):
                    logger.warning(
                        "agui_snapshot_tool_call_fallback trace_id=%s tool_name=%s",
                        trace_id,
                        tool_name,
                    )

                def log_tool_result_fallback(tool_name: str):
                    logger.warning(
                        "agui_snapshot_tool_result_fallback trace_id=%s tool_name=%s",
                        trace_id,
                        tool_name,
                    )

                def log_history_reconciled(count: int):
                    logger.info(
                        "agui_snapshot_history_reconciled trace_id=%s matched_message_count=%d",
                        trace_id,
                        count,
                    )

                def log_snapshot_suppressed(count: int):
                    logger.info(
                        "agui_transcript_snapshots_suppressed trace_id=%s snapshot_count=%d",
                        trace_id,
                        count,
                    )

                def log_missing_message():
                    logger.error("agui_customer_message_missing trace_id=%s", trace_id)

                recovered_events = synthesize_terminal_assistant_message(
                    agent.run(input_data),
                    initial_message_ids=initial_message_ids,
                    initial_message_count=len(initial_messages),
                    initial_messages=initial_messages,
                    make_text_events=make_text_events,
                    make_missing_message_event=make_missing_message_event,
                    make_tool_call_events=_make_snapshot_tool_call_events,
                    make_tool_result_events=_make_snapshot_tool_result_events,
                    snapshot_tool_call_names=_snapshot_recovery_tool_names(),
                    snapshot_tool_result_names=_snapshot_recovery_tool_result_names(),
                    on_synthesized=log_snapshot_fallback,
                    on_tool_call_synthesized=log_tool_call_fallback,
                    on_tool_result_synthesized=log_tool_result_fallback,
                    on_history_reconciled=log_history_reconciled,
                    on_snapshot_suppressed=log_snapshot_suppressed,
                    on_missing=log_missing_message,
                )
                customer_events = filter_internal_assistant_messages(recovered_events)
                # Many deterministic/resource handlers return one completed
                # assistant message. Preserve the registered routing and validation behavior,
                # but publish that text progressively through the same AG-UI
                # message lifecycle used by provider-generated deltas.
                scripted_chunk_chars = int(os.getenv("AGENT_SCRIPTED_STREAM_CHUNK_CHARS", "12"))
                scripted_chunk_delay_ms = float(os.getenv("AGENT_SCRIPTED_STREAM_DELAY_MS", "35"))
                customer_events = stream_text_events(
                    customer_events,
                    target_chars=scripted_chunk_chars,
                    delay_seconds=scripted_chunk_delay_ms / 1000.0,
                )
                async for event in iter_with_keepalives(
                    customer_events,
                    interval_seconds=keepalive_seconds,
                ):
                    if event is None:
                        keepalive_count += 1
                        # SSE comments are transport keepalives, not AG-UI
                        # application events; clients safely ignore them.
                        yield ": keep-alive\n\n"
                        continue
                    emitted += 1
                    now_ms = round((time.perf_counter() - started_at) * 1000)
                    raw_type = getattr(event, "type", None)
                    event_type = getattr(raw_type, "value", str(raw_type or type(event).__name__))
                    event_counts[event_type] = event_counts.get(event_type, 0) + 1
                    normalized_type = event_type.upper()
                    if first_event_ms is None:
                        first_event_ms = now_ms
                    if first_message_ms is None and "TEXT_MESSAGE_CONTENT" in normalized_type:
                        first_message_ms = now_ms
                    if first_tool_ms is None and "TOOL_CALL_START" in normalized_type:
                        first_tool_ms = now_ms
                    if "RUN_FINISHED" in normalized_type:
                        run_finished = True
                    yield encoder.encode(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "agui_run_failed trace_id=%s elapsed_ms=%d emitted_events=%d",
                    trace_id,
                    round((time.perf_counter() - started_at) * 1000),
                    emitted,
                )
                # A stream-level fault must be visible to the AG-UI client.
                # Do not expose provider, MCP, database or exception details.
                try:
                    yield encoder.encode(
                        RunErrorEvent(
                            type=EventType.RUN_ERROR,
                            message="ORAIflow couldn't complete this response. Please try again. No financial action was taken.",
                            code="AGENT_STREAM_ERROR",
                        )
                    )
                except Exception:
                    logger.exception("agui_run_error_event_encode_failed trace_id=%s", trace_id)
            finally:
                logger.info(
                    "agui_run_finished trace_id=%s elapsed_ms=%d emitted_events=%d first_event_ms=%s first_message_ms=%s first_tool_ms=%s run_finished_event=%s keepalive_count=%d event_counts=%s",
                    trace_id,
                    round((time.perf_counter() - started_at) * 1000),
                    emitted,
                    first_event_ms,
                    first_message_ms,
                    first_tool_ms,
                    run_finished,
                    keepalive_count,
                    event_counts,
                )
                reset_trace_id(trace_token)

        return StreamingResponse(
            events(),
            media_type=encoder.get_content_type(),
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-ORAIflow-Trace-ID": trace_id,
            },
        )
