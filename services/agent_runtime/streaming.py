"""Transport helpers for keeping long-running AG-UI streams responsive."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import uuid
from collections import defaultdict, deque
from contextlib import suppress
from typing import Any, AsyncIterable, AsyncIterator, Callable


async def iter_with_keepalives(
    source: AsyncIterable[Any],
    *,
    interval_seconds: float,
) -> AsyncIterator[Any | None]:
    """Yield source events and ``None`` heartbeat markers without cancelling reads.

    A timed-out wait does not cancel the outstanding ``__anext__`` operation.
    This is important for model/provider calls: transport keepalives must not
    interrupt the underlying inference. If the client disconnects, closing
    this iterator does cancel and close the source promptly.
    """
    interval = max(0.1, float(interval_seconds))
    iterator = source.__aiter__()
    pending: asyncio.Task[Any] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(iterator.__anext__())
            done, _ = await asyncio.wait(
                {pending},
                timeout=interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                yield None
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                pending = None
                break
            pending = None
            yield item
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            with suppress(BaseException):
                await pending
        close = getattr(iterator, "aclose", None)
        if callable(close):
            with suppress(Exception):
                await close()


def internal_model_run_config(phase: str) -> dict[str, Any]:
    """Return a child-run config that prevents private model output reaching AG-UI.

    LangGraph propagates its callback handlers into nested Runnable invocations
    unless a child invocation explicitly overrides them. Internal assessment
    JSON and response-envelope tool arguments are implementation details; only
    validated graph-node messages may be published to the customer stream.
    """
    safe_phase = "".join(ch for ch in str(phase).lower() if ch.isalnum() or ch in "-_")[:40]
    return {
        "callbacks": [],
        "tags": ["oraiflow-internal", f"oraiflow-phase:{safe_phase or 'model'}"],
    }


async def invoke_internal_model(model: Any, model_input: Any, *, phase: str) -> Any:
    """Invoke a private model phase without inheriting customer-stream callbacks."""
    return await model.ainvoke(
        model_input,
        config=internal_model_run_config(phase),
    )


def _event_value(event: Any, name: str, default: Any = None) -> Any:
    if isinstance(event, dict):
        return event.get(name, default)
    return getattr(event, name, default)


def _event_kind(event: Any) -> str:
    raw = _event_value(event, "type")
    return str(getattr(raw, "value", raw or type(event).__name__)).upper()


def _is_internal_assessment_text(text: str) -> bool:
    """Recognize leaked internal ORAIflow assessment JSON, including truncation."""
    value = str(text or "").lstrip()
    if not value.startswith("{"):
        return False
    markers = ('"schema_version"', '"resolution"', '"tasks"')
    if all(marker in value for marker in markers):
        return True
    # A partially streamed assessment may be cut off before its JSON closes.
    return '"schema_version"' in value and '"tasks"' in value and '"intent_id"' in value


def _event_delta(event: Any) -> str:
    delta = _event_value(event, "delta", "")
    return delta if isinstance(delta, str) else str(delta or "")


def _message_text(message: Any) -> str:
    """Extract plain text from an AG-UI/LangChain message snapshot safely."""
    content = _event_value(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        else:
            value = _event_value(part, "text", None)
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts).strip()


def _snapshot_assistant_candidate(
    event: Any,
    *,
    initial_message_ids: set[str],
    initial_message_count: int,
) -> tuple[str, str] | None:
    """Find the newest *new* assistant text message in a state snapshot."""
    messages = _event_value(event, "messages", [])
    if not isinstance(messages, (list, tuple)):
        return None
    candidates: list[tuple[str, str]] = []
    for index, message in enumerate(messages):
        role = str(
            _event_value(message, "role", "") or _event_value(message, "type", "")
        ).casefold()
        if role not in {"assistant", "ai"}:
            continue
        content = _message_text(message)
        if not content or _is_internal_assessment_text(content):
            continue
        raw_id = _event_value(message, "id", None) or _event_value(message, "message_id", None)
        if raw_id:
            message_id = str(raw_id)
            if message_id in initial_message_ids:
                continue
        else:
            # Some snapshot adapters omit message IDs. Only accept a message
            # beyond the request's initial message count; never replay an old
            # assistant message with a newly invented ID.
            if index < initial_message_count:
                continue
            message_id = f"snapshot-assistant-{uuid.uuid4().hex}"
        candidates.append((message_id, content))
    return candidates[-1] if candidates else None


def _normalise_stream_text(value: str) -> str:
    """Normalize transport-only whitespace for snapshot/delta deduplication."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _message_role(message: Any) -> str:
    raw = str(_event_value(message, "role", "") or _event_value(message, "type", "")).casefold()
    return {"human": "user", "ai": "assistant", "function": "tool"}.get(raw, raw)


def _message_signature(message: Any) -> tuple[str, str, str] | None:
    """Stable, content-based signature for matching checkpointer history to UI history."""
    role = _message_role(message)
    if role not in {"user", "assistant", "tool", "system", "developer"}:
        return None
    content = _normalise_stream_text(_message_text(message))
    tool_calls = (
        _event_value(message, "tool_calls", None) or _event_value(message, "toolCalls", None) or []
    )
    try:
        calls = json.dumps(
            tool_calls, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError):
        calls = str(tool_calls)
    if not content and not tool_calls:
        return None
    return role, content, calls


def _reconcile_snapshot_history_ids(
    event: Any,
    *,
    initial_messages: list[Any],
    initial_message_ids: set[str],
) -> tuple[Any, set[str], bool]:
    """Identify the current-turn boundary and align historical snapshot IDs.

    MESSAGES_SNAPSHOT is a complete transcript. We inspect it only as a source
    for snapshot-only reply recovery; it is never sent back to an already-live
    chat client. Historical ID alignment is needed solely to avoid mistaking a
    checkpoint's re-keyed old assistant message for a new reply.

    The boolean indicates whether the current user message was found in the
    snapshot. If the boundary cannot be established, snapshot recovery fails
    closed rather than replaying an ambiguous historical assistant message.
    """
    messages = _event_value(event, "messages", [])
    if not isinstance(messages, (list, tuple)):
        return event, set(), False
    if not initial_messages:
        return event, set(), True

    latest_user_index = next(
        (
            index
            for index in range(len(initial_messages) - 1, -1, -1)
            if _message_role(initial_messages[index]) == "user"
        ),
        None,
    )
    if latest_user_index is None:
        return event, set(), False
    latest_user = initial_messages[latest_user_index]
    latest_user_id = _event_value(latest_user, "id", None) or _event_value(
        latest_user, "message_id", None
    )
    latest_user_signature = _message_signature(latest_user)

    boundary = None
    if latest_user_id:
        boundary = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if str(
                    _event_value(messages[index], "id", None)
                    or _event_value(messages[index], "message_id", None)
                    or ""
                )
                == str(latest_user_id)
            ),
            None,
        )
    if boundary is None and latest_user_signature is not None:
        boundary = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if _message_signature(messages[index]) == latest_user_signature
            ),
            None,
        )
    if boundary is None:
        return event, set(), False

    prior_by_signature: dict[tuple[str, str, str], deque[str]] = defaultdict(deque)
    for item in initial_messages[:latest_user_index]:
        message_id = _event_value(item, "id", None) or _event_value(item, "message_id", None)
        signature = _message_signature(item)
        if message_id and signature is not None:
            prior_by_signature[signature].append(str(message_id))

    rewritten = list(messages)
    matched_ids: set[str] = set()
    changed = False
    for index, message in enumerate(messages[:boundary]):
        raw_id = _event_value(message, "id", None) or _event_value(message, "message_id", None)
        if raw_id and str(raw_id) in initial_message_ids:
            matched_ids.add(str(raw_id))
            continue
        signature = _message_signature(message)
        if signature is None or not prior_by_signature.get(signature):
            continue
        canonical_id = prior_by_signature[signature].popleft()
        rewritten[index] = _copy_message_with_id(message, canonical_id)
        matched_ids.add(canonical_id)
        changed = True

    if not changed:
        return event, matched_ids, True
    return _copy_event_with_messages(event, rewritten), matched_ids, True


def _copy_message_with_id(message: Any, message_id: str) -> Any:
    """Copy a snapshot message while preserving its representation."""
    if isinstance(message, dict):
        copied = dict(message)
        key = "id" if "id" in copied or "message_id" not in copied else "message_id"
        copied[key] = message_id
        return copied
    model_copy = getattr(message, "model_copy", None)
    if callable(model_copy):
        return model_copy(update={"id": message_id})
    try:
        from dataclasses import is_dataclass, replace

        if is_dataclass(message):
            return replace(message, id=message_id)
    except (TypeError, ValueError):
        pass
    copied = copy.copy(message)
    try:
        setattr(copied, "id", message_id)
    except (AttributeError, TypeError):
        return message
    return copied


def _copy_event_with_messages(event: Any, messages: list[Any]) -> Any:
    """Copy a snapshot event with reconciled message identities."""
    if isinstance(event, dict):
        copied = dict(event)
        copied["messages"] = messages
        return copied
    model_copy = getattr(event, "model_copy", None)
    if callable(model_copy):
        return model_copy(update={"messages": messages})
    try:
        from dataclasses import is_dataclass, replace

        if is_dataclass(event):
            return replace(event, messages=messages)
    except (TypeError, ValueError):
        pass
    copied = copy.copy(event)
    try:
        setattr(copied, "messages", messages)
    except (AttributeError, TypeError):
        return event
    return copied


def _snapshot_current_turn_messages(
    event: Any,
    initial_messages: list[Any],
) -> tuple[list[Any], bool]:
    """Return snapshot messages after the latest request user message.

    A complete transcript snapshot must never be forwarded as-is, but current-
    turn assistant tool calls can be recovered as ordinary AG-UI tool events.
    This boundary is established by message ID first and exact role/content
    signature second, matching the history reconciliation policy above.
    """
    messages = _event_value(event, "messages", [])
    if not isinstance(messages, (list, tuple)):
        return [], False
    if not initial_messages:
        return list(messages), True

    latest_user = next(
        (item for item in reversed(initial_messages) if _message_role(item) == "user"), None
    )
    if latest_user is None:
        return [], False

    latest_id = _event_value(latest_user, "id", None) or _event_value(
        latest_user, "message_id", None
    )
    boundary = None
    if latest_id:
        boundary = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if str(
                    _event_value(messages[index], "id", None)
                    or _event_value(messages[index], "message_id", None)
                    or ""
                )
                == str(latest_id)
            ),
            None,
        )
    if boundary is None:
        signature = _message_signature(latest_user)
        if signature is not None:
            boundary = next(
                (
                    index
                    for index in range(len(messages) - 1, -1, -1)
                    if _message_signature(messages[index]) == signature
                ),
                None,
            )
    if boundary is None:
        return [], False
    return list(messages[boundary + 1 :]), True


def _snapshot_tool_call_candidates(
    event: Any,
    *,
    initial_messages: list[Any],
    allowed_names: set[str],
    streamed_text_by_id: dict[str, list[str]] | None = None,
) -> list[dict[str, str]]:
    """Extract only registered current-turn assistant UI calls from a snapshot."""
    if not allowed_names:
        return []
    current_messages, boundary_valid = _snapshot_current_turn_messages(event, initial_messages)
    if not boundary_valid:
        return []

    recovered: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for position, message in enumerate(current_messages):
        if _message_role(message) != "assistant":
            continue
        message_id = str(
            _event_value(message, "id", None) or _event_value(message, "message_id", None) or ""
        )
        snapshot_text = _normalise_stream_text(_message_text(message))
        if snapshot_text and streamed_text_by_id:
            matching_stream_id = next(
                (
                    stream_id
                    for stream_id, parts in streamed_text_by_id.items()
                    if _normalise_stream_text("".join(parts)) == snapshot_text
                ),
                None,
            )
            if matching_stream_id:
                message_id = matching_stream_id
        calls = (
            _event_value(message, "tool_calls", None)
            or _event_value(message, "toolCalls", None)
            or []
        )
        additional = _event_value(message, "additional_kwargs", None) or {}
        if not calls and isinstance(additional, dict):
            calls = additional.get("tool_calls") or []
        if not isinstance(calls, (list, tuple)):
            continue
        for call in calls:
            function = _event_value(call, "function", None)
            name = str(
                _event_value(call, "name", None) or _event_value(function, "name", "") or ""
            ).strip()
            call_id = str(
                _event_value(call, "id", None) or _event_value(call, "tool_call_id", None) or ""
            ).strip()
            arguments = _event_value(call, "args", None)
            if arguments is None:
                arguments = _event_value(call, "arguments", None)
            if arguments is None:
                arguments = _event_value(function, "arguments", None)
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (TypeError, ValueError):
                    continue
            if (
                not call_id
                or not name
                or name not in allowed_names
                or not isinstance(arguments, dict)
            ):
                continue
            try:
                args_json = json.dumps(
                    arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            except (TypeError, ValueError):
                continue
            signature = (name, args_json)
            if signature in seen:
                continue
            seen.add(signature)
            recovered.append(
                {
                    "position": position,
                    "message_id": message_id,
                    "tool_call_id": call_id,
                    "tool_call_name": name,
                    "arguments_json": args_json,
                }
            )
    return recovered


def _snapshot_tool_result_candidates(
    event: Any,
    *,
    initial_messages: list[Any],
    allowed_names: set[str],
    known_tool_call_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Extract current-turn results for registered server-side render tools."""
    if not allowed_names:
        return []
    current_messages, boundary_valid = _snapshot_current_turn_messages(event, initial_messages)
    if not boundary_valid:
        return []

    recovered: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for position, message in enumerate(current_messages):
        if _message_role(message) != "tool":
            continue
        call_id = str(
            _event_value(message, "tool_call_id", None)
            or _event_value(message, "toolCallId", None)
            or ""
        ).strip()
        # LangChain ToolMessage may carry a name; the AG-UI ToolMessage wire
        # shape does not. In that case resolve the name from its assistant call.
        snapshot_name = str(_event_value(message, "name", None) or "").strip()
        known_name = str((known_tool_call_names or {}).get(call_id, "")).strip()
        if snapshot_name and known_name and snapshot_name != known_name:
            continue
        name = known_name or snapshot_name
        message_id = str(
            _event_value(message, "id", None) or _event_value(message, "message_id", None) or ""
        ).strip()
        content = _event_value(message, "content", "")
        if not name or name not in allowed_names or not call_id or not message_id:
            continue
        if not isinstance(content, (str, list)):
            try:
                content = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                continue
        signature = (call_id, message_id)
        if signature in seen:
            continue
        seen.add(signature)
        recovered.append(
            {
                "position": position,
                "tool_name": name,
                "tool_call_id": call_id,
                "message_id": message_id,
                "content": content,
            }
        )
    return recovered


def _has_customer_visible_text(
    text_by_id: dict[str, list[str]],
    anonymous_text: list[str],
) -> bool:
    """Ignore internal assessment JSON when deciding whether the UI got a reply."""
    values = ["".join(parts) for parts in text_by_id.values()]
    if anonymous_text:
        values.append("".join(anonymous_text))
    return any(
        _normalise_stream_text(value) and not _is_internal_assessment_text(value)
        for value in values
    )


async def synthesize_terminal_assistant_message(
    source: AsyncIterable[Any],
    *,
    initial_message_ids: set[str] | None = None,
    initial_message_count: int = 0,
    initial_messages: list[Any] | None = None,
    make_text_events: Callable[[str, str], list[Any]],
    make_missing_message_event: Callable[[], Any],
    make_tool_call_events: Callable[[str, str, str, str], list[Any]] | None = None,
    make_tool_result_events: Callable[[str, str, Any], list[Any]] | None = None,
    snapshot_tool_call_names: set[str] | None = None,
    snapshot_tool_result_names: set[str] | None = None,
    on_synthesized: Callable[[str], None] | None = None,
    on_tool_call_synthesized: Callable[[str], None] | None = None,
    on_tool_result_synthesized: Callable[[str], None] | None = None,
    on_history_reconciled: Callable[[int], None] | None = None,
    on_snapshot_suppressed: Callable[[int], None] | None = None,
    on_missing: Callable[[], None] | None = None,
) -> AsyncIterator[Any]:
    """Recover terminal assistant text omitted by snapshot-only AG-UI streams.

    MESSAGES_SNAPSHOT is a full conversation transcript, not a per-turn delta.
    It is consumed for recovery but never forwarded into an already-active
    CopilotKit chat. The request already contains the browser's history, and
    text/tool events carry current-turn output. Replaying a complete snapshot can
    reinsert historical messages or replace an in-flight message placeholder,
    especially when checkpoint IDs differ from browser message IDs. If the
    latest snapshot contains a new assistant text reply or registered UI tool
    call with no corresponding event lifecycle, synthesize only that current-turn
    lifecycle. Full transcript history remains suppressed. Ambiguous history
    boundaries fail closed instead of replaying old assistant responses.
    """
    prior_ids = set(initial_message_ids or set())
    request_messages = list(initial_messages or [])
    candidate: tuple[str, str] | None = None
    snapshot_tool_calls: list[dict[str, Any]] = []
    snapshot_tool_results: list[dict[str, Any]] = []
    pending_snapshot: Any | None = None
    snapshot_count = 0
    assistant_text_by_id: dict[str, list[str]] = {}
    anonymous_assistant_text: list[str] = []
    streamed_tool_call_ids: set[str] = set()
    streamed_tool_call_signatures: set[tuple[str, str]] = set()
    streamed_tool_names_by_id: dict[str, str] = {}
    streamed_tool_result_message_ids: set[str] = set()
    streamed_tool_result_call_ids: set[str] = set()
    open_tool_calls: dict[str, tuple[str, list[str]]] = {}
    visible_tool_call = False
    terminal_seen = False
    terminal_error = False

    def inspect_pending_snapshot() -> None:
        """Inspect the latest full transcript for a missing current-turn reply.

        Full MESSAGES_SNAPSHOT events are deliberately consumed, never emitted
        to CopilotKit during an active run. The client already supplied its
        transcript in RunAgentInput and owns that live message list; replaying a
        backend snapshot can re-add historical messages, especially when a
        checkpointer assigns different IDs.
        """
        nonlocal candidate, snapshot_tool_calls, snapshot_tool_results, pending_snapshot, prior_ids
        if pending_snapshot is None:
            return
        reconciled, matched_history_ids, boundary_valid = _reconcile_snapshot_history_ids(
            pending_snapshot,
            initial_messages=request_messages,
            initial_message_ids=prior_ids,
        )
        prior_ids.update(matched_history_ids)
        if matched_history_ids and on_history_reconciled:
            on_history_reconciled(len(matched_history_ids))
        if boundary_valid:
            candidate = _snapshot_assistant_candidate(
                reconciled,
                initial_message_ids=prior_ids,
                initial_message_count=max(0, int(initial_message_count)),
            )
            snapshot_tool_calls = _snapshot_tool_call_candidates(
                reconciled,
                initial_messages=request_messages,
                allowed_names=set(snapshot_tool_call_names or set()),
                streamed_text_by_id=assistant_text_by_id,
            )
            known_tool_call_names = dict(streamed_tool_names_by_id)
            known_tool_call_names.update(
                {call["tool_call_id"]: call["tool_call_name"] for call in snapshot_tool_calls}
            )
            snapshot_tool_results = _snapshot_tool_result_candidates(
                reconciled,
                initial_messages=request_messages,
                allowed_names=set(snapshot_tool_result_names or set()),
                known_tool_call_names=known_tool_call_names,
            )
        else:
            candidate = None
            snapshot_tool_calls = []
            snapshot_tool_results = []
        pending_snapshot = None

    def candidate_was_streamed() -> bool:
        candidate_text = _normalise_stream_text(candidate[1]) if candidate else ""
        if not candidate_text:
            return False
        return any(
            _normalise_stream_text("".join(parts)) == candidate_text
            for parts in assistant_text_by_id.values()
            if _normalise_stream_text("".join(parts))
        ) or (
            bool(anonymous_assistant_text)
            and _normalise_stream_text("".join(anonymous_assistant_text)) == candidate_text
        )

    async def emit_snapshot_tool_events():
        nonlocal visible_tool_call
        ordered_events = [(call["position"], "call", call) for call in snapshot_tool_calls] + [
            (result["position"], "result", result) for result in snapshot_tool_results
        ]
        for _position, event_kind, item in sorted(ordered_events, key=lambda entry: entry[0]):
            if event_kind == "call":
                if not make_tool_call_events:
                    continue
                call_id = item["tool_call_id"]
                signature = (item["tool_call_name"], item["arguments_json"])
                if call_id in streamed_tool_call_ids or signature in streamed_tool_call_signatures:
                    continue
                for generated in make_tool_call_events(
                    call_id,
                    item["tool_call_name"],
                    item["message_id"],
                    item["arguments_json"],
                ):
                    yield generated
                streamed_tool_call_ids.add(call_id)
                streamed_tool_call_signatures.add(signature)
                visible_tool_call = True
                if on_tool_call_synthesized:
                    on_tool_call_synthesized(item["tool_call_name"])
                continue

            if not make_tool_result_events:
                continue
            call_id = item["tool_call_id"]
            message_id = item["message_id"]
            if call_id not in streamed_tool_call_ids:
                continue
            if (
                message_id in streamed_tool_result_message_ids
                or call_id in streamed_tool_result_call_ids
            ):
                continue
            for generated in make_tool_result_events(call_id, message_id, item["content"]):
                yield generated
            streamed_tool_result_message_ids.add(message_id)
            streamed_tool_result_call_ids.add(call_id)
            visible_tool_call = True
            if on_tool_result_synthesized:
                on_tool_result_synthesized(item["tool_name"])

    async for event in source:
        kind = _event_kind(event)
        message_id = str(_event_value(event, "message_id", "") or "")
        role = str(_event_value(event, "role", "assistant") or "assistant").casefold()
        if "TEXT_MESSAGE_CONTENT" in kind and role in {"assistant", "ai"}:
            delta = _event_delta(event)
            if delta.strip():
                if message_id:
                    assistant_text_by_id.setdefault(message_id, []).append(delta)
                else:
                    anonymous_assistant_text.append(delta)
        elif "TOOL_CALL_START" in kind:
            visible_tool_call = True
            call_id = str(_event_value(event, "tool_call_id", "") or "")
            call_name = str(_event_value(event, "tool_call_name", "") or "")
            if call_id:
                streamed_tool_call_ids.add(call_id)
                streamed_tool_names_by_id[call_id] = call_name
                open_tool_calls[call_id] = (call_name, [])
        elif "TOOL_CALL_ARGS" in kind:
            call_id = str(_event_value(event, "tool_call_id", "") or "")
            if call_id and call_id in open_tool_calls:
                call_name, parts = open_tool_calls[call_id]
                parts.append(str(_event_value(event, "delta", "") or ""))
        elif "TOOL_CALL_END" in kind:
            call_id = str(_event_value(event, "tool_call_id", "") or "")
            if call_id and call_id in open_tool_calls:
                call_name, parts = open_tool_calls.pop(call_id)
                try:
                    parsed_args = json.loads("".join(parts) or "{}")
                    if isinstance(parsed_args, dict):
                        args_json = json.dumps(
                            parsed_args, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        )
                        streamed_tool_call_signatures.add((call_name, args_json))
                except (TypeError, ValueError):
                    pass
        elif "TOOL_CALL_CHUNK" in kind:
            visible_tool_call = True
            call_id = str(_event_value(event, "tool_call_id", "") or "")
            call_name = str(_event_value(event, "tool_call_name", "") or "")
            if call_id:
                streamed_tool_call_ids.add(call_id)
                streamed_tool_names_by_id[call_id] = call_name
            delta = str(_event_value(event, "delta", "") or "")
            try:
                parsed_args = json.loads(delta or "{}")
                if call_name and isinstance(parsed_args, dict):
                    args_json = json.dumps(
                        parsed_args, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    streamed_tool_call_signatures.add((call_name, args_json))
            except (TypeError, ValueError):
                pass
        elif "TOOL_CALL_RESULT" in kind:
            call_id = str(_event_value(event, "tool_call_id", "") or "")
            result_message_id = str(_event_value(event, "message_id", "") or "")
            if call_id:
                streamed_tool_result_call_ids.add(call_id)
            if result_message_id:
                streamed_tool_result_message_ids.add(result_message_id)
        elif "MESSAGES_SNAPSHOT" in kind:
            # It is a complete transcript, not a per-turn delta. The browser
            # already owns and sent its chat history; forwarding this snapshot
            # can replay old answers or reset an in-flight assistant placeholder.
            # Keep only the latest complete snapshot for recovery inspection.
            pending_snapshot = event
            snapshot_count += 1
            continue
        elif "RUN_ERROR" in kind:
            terminal_seen = True
            terminal_error = True
        elif "RUN_FINISHED" in kind:
            terminal_seen = True
            inspect_pending_snapshot()
            if not terminal_error:
                async for generated in emit_snapshot_tool_events():
                    yield generated
            if not terminal_error and candidate and not candidate_was_streamed():
                for generated in make_text_events(candidate[0], candidate[1]):
                    yield generated
                if on_synthesized:
                    on_synthesized(candidate[0])
            elif (
                not terminal_error
                and not _has_customer_visible_text(assistant_text_by_id, anonymous_assistant_text)
                and not visible_tool_call
            ):
                yield make_missing_message_event()
                if on_missing:
                    on_missing()
        yield event

    # Some adapters close without a terminal event. Flush snapshots only after
    # all text has been observed, preserving the same order-independent merge.
    if not terminal_seen:
        inspect_pending_snapshot()
        async for generated in emit_snapshot_tool_events():
            yield generated
        if candidate and not candidate_was_streamed():
            for generated in make_text_events(candidate[0], candidate[1]):
                yield generated
            if on_synthesized:
                on_synthesized(candidate[0])
        elif (
            not _has_customer_visible_text(assistant_text_by_id, anonymous_assistant_text)
            and not visible_tool_call
        ):
            yield make_missing_message_event()
            if on_missing:
                on_missing()
    elif pending_snapshot is not None:
        # Malformed events after RUN_FINISHED remain suppressed; do not replay
        # a whole transcript after the run boundary.
        pending_snapshot = None
    if snapshot_count and on_snapshot_suppressed:
        on_snapshot_suppressed(snapshot_count)


async def filter_internal_assistant_messages(source: AsyncIterable[Any]) -> AsyncIterator[Any]:
    """Suppress internal assessment JSON while preserving ordinary assistant streaming.

    Non-JSON prose is passed through immediately. Only assistant text messages
    beginning with an object are briefly buffered until their end marker, so a
    structured internal payload can be discarded as a whole rather than leaking
    a visible prefix and then being replaced by a later user-facing response.
    """
    buffers: dict[str, tuple[list[Any], list[str]]] = {}
    passthrough: set[str] = set()
    non_assistant_ids: set[str] = set()
    anonymous_buffer: tuple[list[Any], list[str]] | None = None

    async for event in source:
        kind = _event_kind(event)
        message_id = str(_event_value(event, "message_id", "") or "")
        role = str(_event_value(event, "role", "assistant") or "assistant").lower()

        if "TEXT_MESSAGE_START" in kind:
            if role != "assistant":
                if message_id:
                    non_assistant_ids.add(message_id)
                yield event
                continue
            if message_id:
                buffers[message_id] = ([event], [])
                passthrough.discard(message_id)
            else:
                anonymous_buffer = ([event], [])
            continue

        if "TEXT_MESSAGE_CONTENT" in kind and (role == "assistant" or not role):
            if message_id in non_assistant_ids:
                yield event
                continue
            delta = _event_delta(event)
            key = (
                message_id
                if message_id in buffers
                else ("__anonymous__" if anonymous_buffer is not None else "")
            )
            if key:
                events, parts = buffers[key] if key != "__anonymous__" else anonymous_buffer
                events.append(event)
                parts.append(delta)
                content = "".join(parts).lstrip()
                if content and not content.startswith("{"):
                    for buffered_event in events:
                        yield buffered_event
                    if key == "__anonymous__":
                        anonymous_buffer = None
                    else:
                        buffers.pop(key, None)
                        passthrough.add(key)
                # Object-like messages remain buffered until end so the complete
                # payload can be classified without exposing its first chunks.
                continue

            if delta.lstrip().startswith("{"):
                if message_id:
                    buffers[message_id] = ([event], [delta])
                else:
                    anonymous_buffer = ([event], [delta])
                continue
            yield event
            continue

        if "TEXT_MESSAGE_END" in kind:
            if message_id in non_assistant_ids or role != "assistant":
                yield event
                non_assistant_ids.discard(message_id)
                continue
            key = (
                message_id
                if message_id in buffers
                else ("__anonymous__" if anonymous_buffer is not None else "")
            )
            if key:
                events, parts = buffers[key] if key != "__anonymous__" else anonymous_buffer
                text = "".join(parts)
                if not _is_internal_assessment_text(text):
                    for buffered_event in events:
                        yield buffered_event
                    yield event
                if key == "__anonymous__":
                    anonymous_buffer = None
                else:
                    buffers.pop(key, None)
                    passthrough.discard(key)
                continue
            yield event
            passthrough.discard(message_id)
            continue

        # Before terminal events, flush any incomplete non-assessment message.
        # Internal-looking partial JSON is discarded rather than exposed.
        if "RUN_FINISHED" in kind or "RUN_ERROR" in kind:
            for key, (events, parts) in list(buffers.items()):
                if not _is_internal_assessment_text("".join(parts)):
                    for buffered_event in events:
                        yield buffered_event
                buffers.pop(key, None)
            if anonymous_buffer is not None:
                events, parts = anonymous_buffer
                if not _is_internal_assessment_text("".join(parts)):
                    for buffered_event in events:
                        yield buffered_event
                anonymous_buffer = None
        yield event

    # If the source closes without a terminal marker, do not leak an internal
    # assessment fragment. Preserve any ordinary buffered JSON message.
    for events, parts in buffers.values():
        if not _is_internal_assessment_text("".join(parts)):
            for buffered_event in events:
                yield buffered_event
    if anonymous_buffer is not None:
        events, parts = anonymous_buffer
        if not _is_internal_assessment_text("".join(parts)):
            for buffered_event in events:
                yield buffered_event


def _copy_event_with_delta(event: Any, delta: str) -> Any:
    """Copy an AG-UI event without changing its message identity or metadata."""
    if isinstance(event, dict):
        copied = dict(event)
        copied["delta"] = delta
        return copied
    model_copy = getattr(event, "model_copy", None)
    if callable(model_copy):
        return model_copy(update={"delta": delta})
    try:
        from dataclasses import is_dataclass, replace

        if is_dataclass(event):
            return replace(event, delta=delta)
    except (TypeError, ValueError):
        pass
    copied = copy.copy(event)
    setattr(copied, "delta", delta)
    return copied


def _progressive_chunks(text: str, target_chars: int) -> list[str]:
    """Split a completed response at whitespace boundaries without text loss."""
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    for token in re.findall(r"\S+\s*", text):
        if current and len(current) + len(token) > target_chars:
            chunks.append(current)
            current = token
        else:
            current += token
    if current:
        chunks.append(current)
    # Preserve unusual long unbroken strings (for example identifiers or URLs)
    # while avoiding one huge visual update.
    expanded: list[str] = []
    for chunk in chunks:
        if len(chunk) <= target_chars * 2:
            expanded.append(chunk)
        else:
            expanded.extend(
                chunk[index : index + target_chars] for index in range(0, len(chunk), target_chars)
            )
    return expanded


async def stream_text_events(
    source: AsyncIterable[Any],
    *,
    target_chars: int = 22,
    delay_seconds: float = 0.025,
) -> AsyncIterator[Any]:
    """Progressively emit long, completed assistant text as AG-UI text deltas.

    Provider-generated deltas already at or below ``target_chars`` pass through
    unchanged. Deterministic/scripted messages often arrive as one large delta;
    these are split into natural word-sized events while preserving the same
    message ID and surrounding start/end lifecycle. This is presentation
    streaming after the response has been produced, not model generation.
    """
    target = max(8, min(80, int(target_chars)))
    delay = max(0.0, min(0.25, float(delay_seconds)))
    iterator = source.__aiter__()
    try:
        async for event in iterator:
            kind = _event_kind(event)
            role = str(_event_value(event, "role", "assistant") or "assistant").lower()
            delta = _event_delta(event)
            if "TEXT_MESSAGE_CONTENT" not in kind or role != "assistant" or len(delta) <= target:
                yield event
                continue
            chunks = _progressive_chunks(delta, target)
            for index, chunk in enumerate(chunks):
                if index and delay:
                    await asyncio.sleep(delay)
                yield _copy_event_with_delta(event, chunk)
    finally:
        close = getattr(iterator, "aclose", None)
        if callable(close):
            with suppress(Exception):
                await close()
