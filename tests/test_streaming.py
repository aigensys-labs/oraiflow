import asyncio

import pytest

from services.agent_runtime.streaming import (
    iter_with_keepalives,
    synthesize_terminal_assistant_message,
)


@pytest.mark.asyncio
async def test_keepalive_does_not_cancel_pending_source_read():
    release = asyncio.Event()
    source_cancelled = False

    async def source():
        nonlocal source_cancelled
        try:
            await release.wait()
            yield "provider-event"
        except asyncio.CancelledError:
            source_cancelled = True
            raise

    iterator = iter_with_keepalives(source(), interval_seconds=0.01)
    first = await anext(iterator)
    assert first is None
    assert source_cancelled is False

    release.set()
    second = await anext(iterator)
    assert second == "provider-event"
    assert source_cancelled is False
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)


@pytest.mark.asyncio
async def test_keepalive_iterator_propagates_source_errors():
    async def source():
        raise RuntimeError("provider failure")
        yield  # pragma: no cover - make this an async generator

    iterator = iter_with_keepalives(source(), interval_seconds=0.01)
    with pytest.raises(RuntimeError, match="provider failure"):
        await anext(iterator)


@pytest.mark.asyncio
async def test_closing_keepalive_iterator_closes_source():
    source_closed = asyncio.Event()

    async def source():
        try:
            await asyncio.Event().wait()
            yield "unreachable"
        finally:
            source_closed.set()

    iterator = iter_with_keepalives(source(), interval_seconds=0.01)
    assert await anext(iterator) is None
    await iterator.aclose()
    assert source_closed.is_set()


def _agui_event(kind, **kwargs):
    from types import SimpleNamespace

    return SimpleNamespace(type=SimpleNamespace(value=kind), **kwargs)


async def _collect(source):
    return [item async for item in source]


def _fallback_text_events(message_id, content):
    return [
        _agui_event("TEXT_MESSAGE_START", message_id=message_id, role="assistant"),
        _agui_event("TEXT_MESSAGE_CONTENT", message_id=message_id, delta=content, role="assistant"),
        _agui_event("TEXT_MESSAGE_END", message_id=message_id, role="assistant"),
    ]


def _fallback_tool_call_events(tool_call_id, tool_call_name, parent_message_id, arguments_json):
    return [
        _agui_event(
            "TOOL_CALL_START",
            tool_call_id=tool_call_id,
            tool_call_name=tool_call_name,
            parent_message_id=parent_message_id,
        ),
        _agui_event("TOOL_CALL_ARGS", tool_call_id=tool_call_id, delta=arguments_json),
        _agui_event("TOOL_CALL_END", tool_call_id=tool_call_id),
    ]


def _fallback_tool_result_events(tool_call_id, message_id, content):
    return [
        _agui_event(
            "TOOL_CALL_RESULT",
            tool_call_id=tool_call_id,
            message_id=message_id,
            content=content,
            role="tool",
        )
    ]


def _missing_message_event():
    return _agui_event("RUN_ERROR", code="NO_CUSTOMER_MESSAGE")


@pytest.mark.asyncio
async def test_snapshot_only_assistant_reply_is_recovered_without_forwarding_transcript():
    reply = "Your coffee spending is EUR 4.85."
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "assistant-final-1", "role": "assistant", "content": reply},
        ],
    )
    finished = _agui_event("RUN_FINISHED")

    async def source():
        yield snapshot
        yield finished

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            make_text_events=_fallback_text_events,
            make_missing_message_event=_missing_message_event,
        )
    )

    assert [item.type.value for item in result] == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert result[1].delta == reply
    assert (
        result[0].message_id == result[1].message_id == result[2].message_id == "assistant-final-1"
    )


@pytest.mark.asyncio
async def test_suppressed_transcript_snapshot_count_is_reported_once():
    counts = []
    snapshot = _agui_event("MESSAGES_SNAPSHOT", messages=[])

    async def source():
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            make_text_events=_fallback_text_events,
            make_missing_message_event=_missing_message_event,
            on_snapshot_suppressed=counts.append,
        )
    )
    assert counts == [1]
    assert not any(item.type.value == "MESSAGES_SNAPSHOT" for item in result)


@pytest.mark.asyncio
async def test_transcript_snapshot_after_stream_is_consumed_even_when_ids_differ():
    reply = "I found Contacts. Would you like me to open that workspace?"
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "langgraph-message-id", "role": "assistant", "content": reply},
        ],
    )

    async def source():
        yield _agui_event("TEXT_MESSAGE_START", message_id="agui-message-id", role="assistant")
        yield _agui_event(
            "TEXT_MESSAGE_CONTENT", message_id="agui-message-id", role="assistant", delta=reply
        )
        yield _agui_event("TEXT_MESSAGE_END", message_id="agui-message-id", role="assistant")
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            make_text_events=_fallback_text_events,
            make_missing_message_event=_missing_message_event,
        )
    )
    assert [item.type.value for item in result] == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert [item.delta for item in result if item.type.value == "TEXT_MESSAGE_CONTENT"] == [reply]
    assert not any(item.type.value == "MESSAGES_SNAPSHOT" for item in result)


@pytest.mark.asyncio
async def test_snapshot_before_stream_is_consumed_and_does_not_duplicate_answer():
    reply = "Your coffee spending is EUR 4.85."
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "langgraph-message-id", "role": "assistant", "content": reply},
        ],
    )

    async def source():
        yield snapshot
        yield _agui_event("TEXT_MESSAGE_START", message_id="agui-message-id", role="assistant")
        yield _agui_event(
            "TEXT_MESSAGE_CONTENT", message_id="agui-message-id", role="assistant", delta=reply
        )
        yield _agui_event("TEXT_MESSAGE_END", message_id="agui-message-id", role="assistant")
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            make_text_events=_fallback_text_events,
            make_missing_message_event=_missing_message_event,
        )
    )
    assert [item.type.value for item in result] == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert len([item for item in result if item.type.value == "TEXT_MESSAGE_CONTENT"]) == 1
    assert not any(item.type.value == "MESSAGES_SNAPSHOT" for item in result)


@pytest.mark.asyncio
async def test_snapshot_with_rekeyed_history_only_does_not_replay_an_old_assistant_message():
    initial_messages = [
        {"id": "browser-user-1", "role": "user", "content": "Show my account balance."},
        {
            "id": "browser-assistant-1",
            "role": "assistant",
            "content": "Your balance is EUR 125.00.",
        },
        {"id": "browser-user-2", "role": "user", "content": "How much did I spend?"},
    ]
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "browser-user-1", "role": "user", "content": "Show my account balance."},
            {
                "id": "checkpoint-assistant-1",
                "role": "assistant",
                "content": "Your balance is EUR 125.00.",
            },
            {"id": "browser-user-2", "role": "user", "content": "How much did I spend?"},
        ],
    )

    async def source():
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            initial_message_ids={item["id"] for item in initial_messages},
            initial_message_count=len(initial_messages),
            initial_messages=initial_messages,
            make_text_events=_fallback_text_events,
            make_missing_message_event=_missing_message_event,
        )
    )
    assert [item.type.value for item in result] == ["RUN_ERROR", "RUN_FINISHED"]
    assert result[0].code == "NO_CUSTOMER_MESSAGE"


@pytest.mark.asyncio
async def test_rekeyed_history_and_new_current_turn_reply_only_recover_new_reply():
    initial_messages = [
        {"id": "browser-user-1", "role": "user", "content": "Show my account balance."},
        {
            "id": "browser-assistant-1",
            "role": "assistant",
            "content": "Your balance is EUR 125.00.",
        },
        {"id": "browser-user-2", "role": "user", "content": "How much did I spend?"},
    ]
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "browser-user-1", "role": "user", "content": "Show my account balance."},
            {
                "id": "checkpoint-assistant-1",
                "role": "assistant",
                "content": "Your balance is EUR 125.00.",
            },
            {"id": "browser-user-2", "role": "user", "content": "How much did I spend?"},
            {
                "id": "new-assistant-2",
                "role": "assistant",
                "content": "Your spending total is EUR 42.00.",
            },
        ],
    )

    async def source():
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            initial_message_ids={item["id"] for item in initial_messages},
            initial_message_count=len(initial_messages),
            initial_messages=initial_messages,
            make_text_events=_fallback_text_events,
            make_missing_message_event=_missing_message_event,
        )
    )
    assert [item.type.value for item in result] == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert result[0].message_id == "new-assistant-2"
    assert result[1].delta == "Your spending total is EUR 42.00."


@pytest.mark.asyncio
async def test_identical_new_answer_on_a_later_turn_is_not_collapsed_into_history():
    same_answer = "Welcome back. Your account overview is ready."
    initial_messages = [
        {"id": "browser-user-1", "role": "user", "content": "Hello."},
        {"id": "browser-assistant-1", "role": "assistant", "content": same_answer},
        {"id": "browser-user-2", "role": "user", "content": "Please show that again."},
    ]
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "browser-user-1", "role": "user", "content": "Hello."},
            {"id": "checkpoint-assistant-1", "role": "assistant", "content": same_answer},
            {"id": "browser-user-2", "role": "user", "content": "Please show that again."},
            {"id": "assistant-new-turn", "role": "assistant", "content": same_answer},
        ],
    )

    async def source():
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            initial_message_ids={item["id"] for item in initial_messages},
            initial_message_count=3,
            initial_messages=initial_messages,
            make_text_events=_fallback_text_events,
            make_missing_message_event=_missing_message_event,
        )
    )
    assert [item.type.value for item in result] == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert result[0].message_id == "assistant-new-turn"
    assert result[1].delta == same_answer


@pytest.mark.asyncio
async def test_ambiguous_snapshot_history_boundary_fails_closed_without_replay():
    initial_messages = [
        {"id": "browser-user-1", "role": "user", "content": "How much did I spend?"},
    ]
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "old-assistant-rekeyed", "role": "assistant", "content": "An old answer."},
        ],
    )

    async def source():
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            initial_message_ids={"browser-user-1"},
            initial_message_count=1,
            initial_messages=initial_messages,
            make_text_events=_fallback_text_events,
            make_missing_message_event=_missing_message_event,
        )
    )
    assert [item.type.value for item in result] == ["RUN_ERROR", "RUN_FINISHED"]


@pytest.mark.asyncio
async def test_internal_assessment_snapshot_is_not_exposed_to_the_chat():
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {
                "id": "assessment-1",
                "role": "assistant",
                "content": '{"schema_version":"1.0.0","resolution":"RESOLVED","tasks":[]}',
            },
        ],
    )

    async def source():
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            make_text_events=_fallback_text_events,
            make_missing_message_event=_missing_message_event,
        )
    )
    assert [item.type.value for item in result] == ["RUN_ERROR", "RUN_FINISHED"]


@pytest.mark.asyncio
async def test_scripted_assistant_message_is_split_without_changing_message_lifecycle():
    from services.agent_runtime.streaming import stream_text_events

    message_id = "scripted-message-1"
    original_text = "Your latest verified account information is ready to review."
    source_events = [
        _agui_event("TEXT_MESSAGE_START", message_id=message_id, role="assistant"),
        _agui_event(
            "TEXT_MESSAGE_CONTENT",
            message_id=message_id,
            role="assistant",
            delta=original_text,
            metadata={"source": "scripted"},
        ),
        _agui_event("TEXT_MESSAGE_END", message_id=message_id, role="assistant"),
    ]

    async def source():
        for item in source_events:
            yield item

    result = [item async for item in stream_text_events(source(), target_chars=14, delay_seconds=0)]
    content = [item for item in result if "TEXT_MESSAGE_CONTENT" in item.type.value]

    assert result[0] is source_events[0]
    assert result[-1] is source_events[-1]
    assert len(content) > 1
    assert "".join(item.delta for item in content) == original_text
    assert {item.message_id for item in content} == {message_id}
    assert all(item.metadata == {"source": "scripted"} for item in content)


@pytest.mark.asyncio
async def test_small_provider_deltas_and_user_messages_pass_through_unchanged():
    from services.agent_runtime.streaming import stream_text_events

    assistant = _agui_event(
        "TEXT_MESSAGE_CONTENT", message_id="provider-1", role="assistant", delta="small delta"
    )
    user = _agui_event(
        "TEXT_MESSAGE_CONTENT",
        message_id="user-1",
        role="user",
        delta="a longer user message that must not be split",
    )

    async def source():
        yield assistant
        yield user

    result = [item async for item in stream_text_events(source(), target_chars=14, delay_seconds=0)]
    assert result == [assistant, user]
    assert result[0] is assistant
    assert result[1] is user


@pytest.mark.asyncio
async def test_complete_agui_filter_then_stream_pipeline_progressively_emits_knowledge_text():
    from services.agent_runtime.streaming import (
        filter_internal_assistant_messages,
        stream_text_events,
    )

    message_id = "academy-long-response"
    text = (
        "Savings can help separate money for goals and unexpected costs. "
        "Keep enough accessible money for essential bills and short-term needs. "
        "Compare rates, fees, access conditions, withdrawal limits and interest timing. "
    ) * 3
    events = [
        _agui_event("TEXT_MESSAGE_START", message_id=message_id, role="assistant"),
        _agui_event("TEXT_MESSAGE_CONTENT", message_id=message_id, role="assistant", delta=text),
        _agui_event("TEXT_MESSAGE_END", message_id=message_id, role="assistant"),
    ]

    async def source():
        for event in events:
            yield event

    filtered = filter_internal_assistant_messages(source())
    result = [item async for item in stream_text_events(filtered, target_chars=12, delay_seconds=0)]
    content = [item.delta for item in result if "TEXT_MESSAGE_CONTENT" in item.type.value]
    assert len(content) > 8
    assert "".join(content) == text
    assert {item.message_id for item in result if hasattr(item, "message_id")} == {message_id}
    assert result[0].type.value == "TEXT_MESSAGE_START"
    assert result[-1].type.value == "TEXT_MESSAGE_END"


@pytest.mark.asyncio
async def test_snapshot_only_registered_presentation_call_is_recovered_as_tool_lifecycle():
    request_messages = [{"id": "user-current", "role": "user", "content": "What's my balance?"}]
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "user-current", "role": "user", "content": "What's my balance?"},
            {
                "id": "assistant-current",
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "render-call-1",
                        "name": "render_account_summary",
                        "args": {"title": "Account balances", "accounts": [], "evidence": {}},
                    }
                ],
            },
        ],
    )

    async def source():
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            initial_message_ids={"user-current"},
            initial_message_count=1,
            initial_messages=request_messages,
            make_text_events=_fallback_text_events,
            make_tool_call_events=_fallback_tool_call_events,
            snapshot_tool_call_names={"render_account_summary"},
            make_missing_message_event=_missing_message_event,
        )
    )
    assert [item.type.value for item in result] == [
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "RUN_FINISHED",
    ]
    assert result[0].tool_call_id == "render-call-1"
    assert result[0].tool_call_name == "render_account_summary"
    assert '"title":"Account balances"' in result[1].delta


@pytest.mark.asyncio
async def test_snapshot_only_frontend_action_is_recovered_and_does_not_raise_missing_reply():
    request_messages = [{"id": "user-current", "role": "user", "content": "ok"}]
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "user-current", "role": "user", "content": "ok"},
            {
                "id": "assistant-current",
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "navigate-call-1",
                        "name": "navigate_workspace",
                        "args": {"workspace": "Contacts", "action_id": "nav-123"},
                    }
                ],
            },
        ],
    )

    async def source():
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            initial_message_ids={"user-current"},
            initial_message_count=1,
            initial_messages=request_messages,
            make_text_events=_fallback_text_events,
            make_tool_call_events=_fallback_tool_call_events,
            snapshot_tool_call_names={"navigate_workspace"},
            make_missing_message_event=_missing_message_event,
        )
    )
    assert [item.type.value for item in result] == [
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "RUN_FINISHED",
    ]
    assert not any(item.type.value == "RUN_ERROR" for item in result)


@pytest.mark.asyncio
async def test_snapshot_tool_calls_from_history_and_unregistered_tools_are_not_recovered():
    request_messages = [
        {"id": "user-old", "role": "user", "content": "Show balances"},
        {
            "id": "assistant-old",
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "old-call", "name": "render_account_summary", "args": {"title": "old"}},
            ],
        },
        {"id": "user-current", "role": "user", "content": "ok"},
    ]
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "user-old", "role": "user", "content": "Show balances"},
            {
                "id": "rekeyed-old-assistant",
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "old-call-new-id",
                        "name": "render_account_summary",
                        "args": {"title": "old"},
                    },
                ],
            },
            {"id": "user-current", "role": "user", "content": "ok"},
            {
                "id": "assistant-current",
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "bad-call", "name": "unregistered_component", "args": {"x": 1}},
                ],
            },
        ],
    )

    async def source():
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            initial_message_ids={"user-old", "assistant-old", "user-current"},
            initial_message_count=3,
            initial_messages=request_messages,
            make_text_events=_fallback_text_events,
            make_tool_call_events=_fallback_tool_call_events,
            snapshot_tool_call_names={"render_account_summary"},
            make_missing_message_event=_missing_message_event,
        )
    )
    assert [item.type.value for item in result] == ["RUN_ERROR", "RUN_FINISHED"]


@pytest.mark.asyncio
async def test_snapshot_tool_call_already_streamed_is_not_emitted_twice():
    request_messages = [{"id": "user-current", "role": "user", "content": "Show balances"}]
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "user-current", "role": "user", "content": "Show balances"},
            {
                "id": "assistant-current",
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "render_account_summary",
                        "args": {"title": "Balances"},
                    },
                ],
            },
        ],
    )

    async def source():
        yield _agui_event(
            "TOOL_CALL_START", tool_call_id="call-1", tool_call_name="render_account_summary"
        )
        yield _agui_event("TOOL_CALL_ARGS", tool_call_id="call-1", delta='{"title":"Balances"}')
        yield _agui_event("TOOL_CALL_END", tool_call_id="call-1")
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            initial_message_ids={"user-current"},
            initial_message_count=1,
            initial_messages=request_messages,
            make_text_events=_fallback_text_events,
            make_tool_call_events=_fallback_tool_call_events,
            snapshot_tool_call_names={"render_account_summary"},
            make_missing_message_event=_missing_message_event,
        )
    )
    assert [item.type.value for item in result].count("TOOL_CALL_START") == 1
    assert [item.type.value for item in result].count("TOOL_CALL_END") == 1


@pytest.mark.asyncio
async def test_snapshot_presentation_call_attaches_to_matching_streamed_text_message_id():
    request_messages = [{"id": "user-current", "role": "user", "content": "Show balances"}]
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "user-current", "role": "user", "content": "Show balances"},
            {
                "id": "snapshot-assistant-id",
                "role": "assistant",
                "content": "Here are your balances.",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "render_account_summary",
                        "args": {"title": "Balances"},
                    }
                ],
            },
        ],
    )

    async def source():
        yield _agui_event("TEXT_MESSAGE_START", message_id="stream-assistant-id", role="assistant")
        yield _agui_event(
            "TEXT_MESSAGE_CONTENT",
            message_id="stream-assistant-id",
            role="assistant",
            delta="Here are your balances.",
        )
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            initial_message_ids={"user-current"},
            initial_message_count=1,
            initial_messages=request_messages,
            make_text_events=_fallback_text_events,
            make_tool_call_events=_fallback_tool_call_events,
            snapshot_tool_call_names={"render_account_summary"},
            make_missing_message_event=_missing_message_event,
        )
    )
    recovered_start = next(item for item in result if item.type.value == "TOOL_CALL_START")
    assert recovered_start.parent_message_id == "stream-assistant-id"


@pytest.mark.asyncio
async def test_snapshot_presentation_without_tool_call_id_fails_closed():
    request_messages = [{"id": "user-current", "role": "user", "content": "Show balances"}]
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "user-current", "role": "user", "content": "Show balances"},
            {
                "id": "assistant-current",
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"name": "render_account_summary", "args": {"title": "Balances"}},
                ],
            },
        ],
    )

    async def source():
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            initial_message_ids={"user-current"},
            initial_message_count=1,
            initial_messages=request_messages,
            make_text_events=_fallback_text_events,
            make_tool_call_events=_fallback_tool_call_events,
            snapshot_tool_call_names={"render_account_summary"},
            make_missing_message_event=_missing_message_event,
        )
    )
    assert [item.type.value for item in result] == ["RUN_ERROR", "RUN_FINISHED"]


@pytest.mark.asyncio
async def test_snapshot_only_mcp_tool_activity_recovers_call_and_result_in_message_order():
    request_messages = [{"id": "user-current", "role": "user", "content": "What's my balance?"}]
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "user-current", "role": "user", "content": "What's my balance?"},
            {
                "id": "assistant-mcp",
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "mcp-call-1",
                        "name": "list_customer_accounts",
                        "args": {"include_closed": False},
                    },
                ],
            },
            {
                "id": "tool-result-1",
                "role": "tool",
                "tool_call_id": "mcp-call-1",
                "content": '{"status":"ok","accounts":[]}',
            },
            {
                "id": "assistant-component",
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "component-call-1",
                        "name": "render_account_summary",
                        "args": {"title": "Accounts"},
                    },
                ],
            },
        ],
    )

    async def source():
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            initial_message_ids={"user-current"},
            initial_message_count=1,
            initial_messages=request_messages,
            make_text_events=_fallback_text_events,
            make_tool_call_events=_fallback_tool_call_events,
            make_tool_result_events=_fallback_tool_result_events,
            snapshot_tool_call_names={"list_customer_accounts", "render_account_summary"},
            snapshot_tool_result_names={"list_customer_accounts"},
            make_missing_message_event=_missing_message_event,
        )
    )
    types = [item.type.value for item in result]
    assert types == [
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "TOOL_CALL_RESULT",
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "RUN_FINISHED",
    ]
    assert result[3].message_id == "tool-result-1"
    assert result[3].tool_call_id == "mcp-call-1"
    assert '"accounts":[]' in result[3].content


@pytest.mark.asyncio
async def test_snapshot_tool_result_without_a_registered_call_is_not_emitted():
    request_messages = [{"id": "user-current", "role": "user", "content": "Balance"}]
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "user-current", "role": "user", "content": "Balance"},
            {
                "id": "tool-result-1",
                "role": "tool",
                "name": "unregistered_tool",
                "tool_call_id": "unknown-call",
                "content": "secret",
            },
        ],
    )

    async def source():
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            initial_message_ids={"user-current"},
            initial_message_count=1,
            initial_messages=request_messages,
            make_text_events=_fallback_text_events,
            make_tool_call_events=_fallback_tool_call_events,
            make_tool_result_events=_fallback_tool_result_events,
            snapshot_tool_call_names={"render_account_summary"},
            snapshot_tool_result_names={"list_customer_accounts"},
            make_missing_message_event=_missing_message_event,
        )
    )
    assert [item.type.value for item in result] == ["RUN_ERROR", "RUN_FINISHED"]


@pytest.mark.asyncio
async def test_already_streamed_tool_result_is_not_recovered_again_from_snapshot():
    request_messages = [{"id": "user-current", "role": "user", "content": "Balance"}]
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "user-current", "role": "user", "content": "Balance"},
            {
                "id": "assistant-mcp",
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "mcp-call-1",
                        "name": "list_customer_accounts",
                        "args": {"include_closed": False},
                    },
                ],
            },
            {
                "id": "tool-result-1",
                "role": "tool",
                "name": "list_customer_accounts",
                "tool_call_id": "mcp-call-1",
                "content": "ok",
            },
        ],
    )

    async def source():
        yield _agui_event(
            "TOOL_CALL_START", tool_call_id="mcp-call-1", tool_call_name="list_customer_accounts"
        )
        yield _agui_event(
            "TOOL_CALL_ARGS", tool_call_id="mcp-call-1", delta='{"include_closed":false}'
        )
        yield _agui_event("TOOL_CALL_END", tool_call_id="mcp-call-1")
        yield _agui_event(
            "TOOL_CALL_RESULT", tool_call_id="mcp-call-1", message_id="tool-result-1", content="ok"
        )
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            initial_message_ids={"user-current"},
            initial_message_count=1,
            initial_messages=request_messages,
            make_text_events=_fallback_text_events,
            make_tool_call_events=_fallback_tool_call_events,
            make_tool_result_events=_fallback_tool_result_events,
            snapshot_tool_call_names={"list_customer_accounts"},
            snapshot_tool_result_names={"list_customer_accounts"},
            make_missing_message_event=_missing_message_event,
        )
    )
    assert [item.type.value for item in result].count("TOOL_CALL_START") == 1
    assert [item.type.value for item in result].count("TOOL_CALL_RESULT") == 1


@pytest.mark.asyncio
async def test_snapshot_openai_function_shape_tool_call_is_normalized():
    request_messages = [{"id": "user-current", "role": "user", "content": "Show balances"}]
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "user-current", "role": "user", "content": "Show balances"},
            {
                "id": "assistant-current",
                "role": "assistant",
                "content": "",
                "toolCalls": [
                    {
                        "id": "component-call-raw",
                        "type": "function",
                        "function": {
                            "name": "render_account_summary",
                            "arguments": '{"title":"Balances"}',
                        },
                    },
                ],
            },
        ],
    )

    async def source():
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            initial_message_ids={"user-current"},
            initial_message_count=1,
            initial_messages=request_messages,
            make_text_events=_fallback_text_events,
            make_tool_call_events=_fallback_tool_call_events,
            snapshot_tool_call_names={"render_account_summary"},
            make_missing_message_event=_missing_message_event,
        )
    )
    assert [item.type.value for item in result] == [
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "RUN_FINISHED",
    ]
    assert result[0].tool_call_name == "render_account_summary"
    assert '"title":"Balances"' in result[1].delta


@pytest.mark.asyncio
async def test_snapshot_tool_result_name_must_match_its_registered_call_id():
    request_messages = [{"id": "user-current", "role": "user", "content": "Balance"}]
    snapshot = _agui_event(
        "MESSAGES_SNAPSHOT",
        messages=[
            {"id": "user-current", "role": "user", "content": "Balance"},
            {
                "id": "assistant-mcp",
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "mcp-call-1", "name": "list_customer_accounts", "args": {}},
                ],
            },
            {
                "id": "tool-result-1",
                "role": "tool",
                "name": "get_account_balance",
                "tool_call_id": "mcp-call-1",
                "content": "should be rejected",
            },
        ],
    )

    async def source():
        yield snapshot
        yield _agui_event("RUN_FINISHED")

    result = await _collect(
        synthesize_terminal_assistant_message(
            source(),
            initial_message_ids={"user-current"},
            initial_message_count=1,
            initial_messages=request_messages,
            make_text_events=_fallback_text_events,
            make_tool_call_events=_fallback_tool_call_events,
            make_tool_result_events=_fallback_tool_result_events,
            snapshot_tool_call_names={"list_customer_accounts", "get_account_balance"},
            snapshot_tool_result_names={"list_customer_accounts", "get_account_balance"},
            make_missing_message_event=_missing_message_event,
        )
    )
    assert [item.type.value for item in result].count("TOOL_CALL_START") == 1
    assert not any(item.type.value == "TOOL_CALL_RESULT" for item in result)
