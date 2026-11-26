from types import SimpleNamespace

import pytest

from services.agent_runtime.streaming import (
    filter_internal_assistant_messages,
    internal_model_run_config,
    invoke_internal_model,
)


def event(kind, **kwargs):
    return SimpleNamespace(type=SimpleNamespace(value=kind), **kwargs)


async def collect(source):
    return [item async for item in filter_internal_assistant_messages(source)]


async def async_events(items):
    for item in items:
        yield item


def test_internal_model_runs_disable_inherited_graph_callbacks():
    config = internal_model_run_config("turn-assessment")
    assert config["callbacks"] == []
    assert "oraiflow-internal" in config["tags"]
    assert "oraiflow-phase:turn-assessment" in config["tags"]


@pytest.mark.asyncio
async def test_private_model_invocation_explicitly_overrides_inherited_callbacks():
    class FakeModel:
        def __init__(self):
            self.config = None
            self.inputs = None

        async def ainvoke(self, inputs, *, config=None):
            self.inputs = inputs
            self.config = config
            return "structured-result"

    model = FakeModel()
    result = await invoke_internal_model(model, ["private prompt"], phase="turn-assessment")
    assert result == "structured-result"
    assert model.inputs == ["private prompt"]
    assert model.config["callbacks"] == []
    assert "oraiflow-internal" in model.config["tags"]


@pytest.mark.asyncio
async def test_assessment_json_message_is_suppressed_as_a_whole():
    message_id = "internal-assessment-1"
    payload = (
        '{"schema_version":"1.0.0","resolution":"RESOLVED",'
        '"tasks":[{"intent_id":"TRANSFER_PREPARATION"}]}'
    )
    chunks = [payload[:40], payload[40:95], payload[95:]]
    events = [event("TEXT_MESSAGE_START", message_id=message_id, role="assistant")]
    events.extend(event("TEXT_MESSAGE_CONTENT", message_id=message_id, delta=part) for part in chunks)
    events.append(event("TEXT_MESSAGE_END", message_id=message_id, role="assistant"))

    assert await collect(async_events(events)) == []


@pytest.mark.asyncio
async def test_truncated_assessment_json_is_not_leaked_before_run_finish():
    message_id = "internal-assessment-truncated"
    partial = '{"schema_version":"1.0.0","resolution":"RESOLVED","tasks":[{"intent_id":"TRANSFER_PREPARATION"'
    events = [
        event("TEXT_MESSAGE_START", message_id=message_id, role="assistant"),
        event("TEXT_MESSAGE_CONTENT", message_id=message_id, delta=partial),
        event("RUN_FINISHED"),
    ]

    result = await collect(async_events(events))
    assert [item.type.value for item in result] == ["RUN_FINISHED"]


@pytest.mark.asyncio
async def test_plain_assistant_prose_is_forwarded_without_waiting_for_json_buffer():
    message_id = "customer-message-1"
    events = [
        event("TEXT_MESSAGE_START", message_id=message_id, role="assistant"),
        event("TEXT_MESSAGE_CONTENT", message_id=message_id, delta="Here are your recent transactions."),
        event("TEXT_MESSAGE_CONTENT", message_id=message_id, delta=" I found six records."),
        event("TEXT_MESSAGE_END", message_id=message_id, role="assistant"),
    ]

    result = await collect(async_events(events))
    assert result == events


@pytest.mark.asyncio
async def test_customer_json_without_internal_assessment_markers_is_preserved():
    message_id = "customer-json-1"
    events = [
        event("TEXT_MESSAGE_START", message_id=message_id, role="assistant"),
        event("TEXT_MESSAGE_CONTENT", message_id=message_id, delta='{"total": 3, "currency": "EUR"}'),
        event("TEXT_MESSAGE_END", message_id=message_id, role="assistant"),
    ]

    assert await collect(async_events(events)) == events


@pytest.mark.asyncio
async def test_non_assistant_json_message_is_not_filtered():
    message_id = "user-message-1"
    events = [
        event("TEXT_MESSAGE_START", message_id=message_id, role="user"),
        event("TEXT_MESSAGE_CONTENT", message_id=message_id, delta='{"schema_version":"1.0.0","tasks":[]}'),
        event("TEXT_MESSAGE_END", message_id=message_id, role="user"),
    ]

    assert await collect(async_events(events)) == events
