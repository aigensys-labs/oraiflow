import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from services.agent_runtime.agent import (
    _bind_verified_component_data,
    _component_preflight_tool_calls,
    _connector_error_for_turn,
    _connector_failure_message,
    _capability_result_for_turn,
    _apply_presentation_transform,
    _validate_proposed_tool_calls,
    _workspace_navigation_step,
    _frontend_action_acknowledgement,
    _customer_assessment_message,
    _academy_personal_data_read_step,
    _academy_personal_data_response_step,
    _academy_personal_data_route,
    _pending_semantic_tasks,
)
from shared.contracts.registry import create_result_envelope, get_registry, parse_result_envelope
from shared.financial_health import calculate_recorded_financial_health
from shared.transfer_workflow import workflow_from_message, latest_pending_workflow


def _trusted_tool_message(name, data, tool_call_id="mcp-call-1"):
    capability_id = {
        "list_customer_accounts": "banking.accounts.list",
        "get_account_balance": "banking.accounts.balance",
        "get_recent_transactions": "banking.transactions.recent",
        "search_contacts": "banking.contacts.search",
    }[name]
    outcome = "EMPTY" if isinstance(data, list) and not data else "SUCCESS"
    envelope = create_result_envelope(capability_id, outcome, data=data, trace_id="test-trace")
    return ToolMessage(name=name, tool_call_id=tool_call_id, content=json.dumps(envelope))


def test_transaction_component_schedules_mcp_read_when_no_verified_result_exists():
    state = {"messages": [HumanMessage(content="Show my recent transactions")], "copilotkit": {}}
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "render_transaction_list",
                "args": {"title": "Recent activity", "transactions": []},
                "id": "component-1",
                "type": "tool_call",
            }
        ],
    )

    calls = _component_preflight_tool_calls(response, state)

    assert len(calls) == 1
    assert calls[0]["name"] == "get_recent_transactions"
    assert calls[0]["args"] == {"limit": 6}


def test_account_component_uses_selected_account_context_for_mcp_preflight():
    state = {
        "messages": [HumanMessage(content="How much money do I have?")],
        "copilotkit": {
            "context": [
                {
                    "description": "Current ORAIflow workspace and account selection",
                    "value": {
                        "workspace": "Overview",
                        "selected_account_id": "acc-002",
                        "selected_account_name": "Savings account",
                        "accounts": [
                            {
                                "account_id": "acc-002",
                                "account_name": "Savings account",
                                "currency": "EUR",
                            },
                        ],
                        "contacts": [],
                        "agent_turn_contract_version": "1.1.0",
                    },
                }
            ],
        },
    }
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "render_account_summary",
                "args": {"title": "Balances", "accounts": []},
                "id": "component-2",
                "type": "tool_call",
            }
        ],
    )

    calls = _component_preflight_tool_calls(response, state)

    assert len(calls) == 1
    assert calls[0]["name"] == "get_account_balance"
    assert calls[0]["args"] == {"account_id": "acc-002"}


def test_existing_mcp_read_is_not_scheduled_twice():
    state = {
        "messages": [
            HumanMessage(content="Show transactions"),
            _trusted_tool_message("get_recent_transactions", []),
        ],
        "copilotkit": {},
    }
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "render_transaction_list",
                "args": {"title": "Recent activity", "transactions": []},
                "id": "component-3",
                "type": "tool_call",
            }
        ],
    )

    assert _component_preflight_tool_calls(response, state) == []
    seen, envelope = _capability_result_for_turn(state["messages"], "banking.transactions.recent")
    assert seen is True
    assert envelope["data"] == []


def test_old_turn_mcp_result_does_not_satisfy_current_turn_component_contract():
    messages = [
        HumanMessage(content="Show my previous transactions"),
        _trusted_tool_message(
            "get_recent_transactions",
            [
                {
                    "transaction_id": "old",
                    "account_name": "Everyday",
                    "description": "Old",
                    "amount_minor": -100,
                    "currency": "EUR",
                    "status": "BOOKED",
                    "occurred_at": None,
                }
            ],
        ),
        HumanMessage(content="Show my latest transactions"),
    ]

    seen, envelope = _capability_result_for_turn(messages, "banking.transactions.recent")

    assert seen is False
    assert envelope is None


def test_component_props_are_replaced_with_verified_mcp_result_and_evidence():
    verified = [
        {
            "transaction_id": "txn-verified",
            "account_name": "Everyday account",
            "description": "Coffee",
            "category": "Dining",
            "amount_minor": -485,
            "currency": "EUR",
            "status": "BOOKED",
            "occurred_at": None,
        }
    ]
    state = {
        "messages": [
            HumanMessage(content="Show my transactions"),
            _trusted_tool_message("get_recent_transactions", verified),
        ],
        "copilotkit": {},
    }
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "render_transaction_list",
                "args": {
                    "title": "Model title",
                    "transactions": [{"description": "model-invented"}],
                },
                "id": "component-4",
                "type": "tool_call",
            }
        ],
    )

    bound = _bind_verified_component_data(response, state)

    assert bound.tool_calls[0]["args"]["transactions"] == verified
    assert bound.tool_calls[0]["args"]["title"] == "Recent activity"
    assert bound.tool_calls[0]["args"]["evidence"]["source"] == "mcp"


def test_missing_verified_component_result_returns_customer_safe_message():
    state = {"messages": [HumanMessage(content="Show my transactions")], "copilotkit": {}}
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "render_transaction_list",
                "args": {"title": "Recent activity", "transactions": []},
                "id": "component-5",
                "type": "tool_call",
            }
        ],
    )

    bound = _bind_verified_component_data(response, state)

    assert "I need to retrieve" not in bound.content
    assert "couldn't retrieve your recent transactions" in bound.content
    assert not bound.tool_calls


def test_connector_error_is_detected_as_authoritative_failure():
    failure = create_result_envelope(
        "banking.transactions.recent",
        "UNAVAILABLE",
        error={"code": "BANKING_DATA_UNAVAILABLE", "retryable": True},
        trace_id="test-trace",
    )
    messages = [
        HumanMessage(content="Show my recent transactions"),
        ToolMessage(
            name="get_recent_transactions",
            tool_call_id="mcp-call-error",
            content=json.dumps(failure),
        ),
    ]

    assert _connector_error_for_turn(messages) == "get_recent_transactions"
    reply = _connector_failure_message(_connector_error_for_turn(messages))
    assert "couldn't retrieve your recent transactions" in reply.content
    assert "MCP" not in reply.content


def test_connector_error_from_an_older_turn_does_not_poison_new_turn():
    failure = create_result_envelope(
        "banking.transactions.recent",
        "UNAVAILABLE",
        error={"code": "BANKING_DATA_UNAVAILABLE", "retryable": True},
        trace_id="test-trace",
    )
    messages = [
        HumanMessage(content="Show my recent transactions"),
        ToolMessage(
            name="get_recent_transactions",
            tool_call_id="mcp-call-error",
            content=json.dumps(failure),
        ),
        HumanMessage(content="Show my accounts"),
    ]

    assert _connector_error_for_turn(messages) is None


def test_invalid_currency_and_type_are_rejected_by_registry_before_rendering():
    invalid = [
        {
            "transaction_id": "txn-bad",
            "account_name": "Everyday account",
            "description": "Coffee",
            "category": None,
            "amount_minor": -485,
            "currency": "eur",
            "status": "BOOKED",
            "occurred_at": None,
        }
    ]
    forged = {
        "schema_version": "1.0.0",
        "capability": "banking.transactions.recent",
        "outcome": "SUCCESS",
        "data": invalid,
        "evidence": {
            "source": "mcp",
            "scope": "authenticated_customer_transactions",
            "retrieved_at": "2026-11-21T00:00:00+00:00",
            "completeness": "COMPLETE",
            "trace_id": "t",
        },
        "error": None,
        "trace_id": "t",
        "trust_class": "untrusted_banking_data",
    }
    state = {
        "messages": [
            HumanMessage(content="Show my transactions"),
            ToolMessage(
                name="get_recent_transactions", tool_call_id="bad", content=json.dumps(forged)
            ),
        ],
        "copilotkit": {},
    }
    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "render_transaction_list", "args": {}, "id": "invalid", "type": "tool_call"}
        ],
    )

    bound = _bind_verified_component_data(response, state)

    assert not bound.tool_calls
    assert "couldn't verify the banking information" in bound.content


def test_mcp_connector_response_uses_the_common_result_envelope():
    payload = [
        {
            "transaction_id": "txn-1",
            "account_name": "Everyday account",
            "description": "Coffee",
            "category": None,
            "amount_minor": -485,
            "currency": "EUR",
            "status": "BOOKED",
            "occurred_at": "2026-11-20T10:00:00Z",
        }
    ]
    envelope = create_result_envelope(
        "banking.transactions.recent", "SUCCESS", data=payload, trace_id="mcp-trace-1"
    )
    parsed = parse_result_envelope("banking.transactions.recent", json.dumps(envelope))
    assert parsed["data"][0]["amount_minor"] == -485
    assert parsed["evidence"]["source"] == "mcp"
    assert parsed["trace_id"] == "mcp-trace-1"


def test_mcp_connector_response_rejects_raw_payload_without_common_envelope():
    raw = json.dumps([{"transaction_id": "txn-1", "amount_minor": "not-an-integer"}])
    try:
        parse_result_envelope("banking.transactions.recent", raw)
    except ValueError:
        pass
    else:
        raise AssertionError("raw MCP payload must not bypass the common result envelope")


def test_mcp_connector_error_is_a_normalized_rejected_envelope():
    envelope = create_result_envelope(
        "banking.accounts.balance",
        "REJECTED",
        error={"code": "ACCOUNT_NOT_ACCESSIBLE", "retryable": False},
        trace_id="mcp-reject-1",
    )
    parsed = parse_result_envelope("banking.accounts.balance", json.dumps(envelope))
    assert parsed["outcome"] == "REJECTED"
    assert parsed["data"] is None
    assert parsed["evidence"] is None


def test_read_capability_normalizes_connector_exception_without_leaking_details(monkeypatch):
    import asyncio
    import services.agent_runtime.agent as agent_module

    async def fail_connector(_name, _arguments=None):
        raise RuntimeError("postgres://internal-host/private-db")

    monkeypatch.setattr(agent_module, "call_banking_tool", fail_connector)
    raw = asyncio.run(agent_module._call_read_capability("get_recent_transactions", {"limit": 6}))
    envelope = json.loads(raw)

    assert envelope["outcome"] == "UNAVAILABLE"
    assert envelope["error"]["code"] == "MCP_TRANSPORT_UNAVAILABLE"
    assert envelope["capability"] == "banking.transactions.recent"
    assert "internal-host" not in raw


def test_read_capability_strips_runtime_assessment_metadata_before_occf_validation(monkeypatch):
    import asyncio
    import services.agent_runtime.agent as agent_module

    observed = []
    valid = create_result_envelope(
        "banking.transactions.recent",
        "EMPTY",
        data=[],
        trace_id="mcp-metadata-strip",
    )

    async def capture_connector(name, arguments=None):
        observed.append((name, arguments))
        return json.dumps(valid)

    monkeypatch.setattr(agent_module, "call_banking_tool", capture_connector)
    raw = asyncio.run(
        agent_module._call_read_capability(
            "get_recent_transactions",
            {
                "limit": 6,
                "orai_intent_id": "TRANSACTION_ANALYSIS",
                "orai_slots": {"flow": "outflows"},
                "orai_evidence": [{"field": "flow", "source": "current_turn", "quote": "spend"}],
                "interpretation_assessment": {"intent_id": "TRANSACTION_ANALYSIS"},
            },
        )
    )

    envelope = json.loads(raw)
    assert observed == [("get_recent_transactions", {"limit": 6})]
    assert envelope["outcome"] == "EMPTY"


def test_read_capability_does_not_retry_mcp_tool_execution_errors(monkeypatch):
    import asyncio
    import services.agent_runtime.agent as agent_module

    calls = []

    async def tool_error(name, _arguments=None):
        calls.append(name)
        raise agent_module.MCPToolExecutionError(name)

    monkeypatch.setattr(agent_module, "call_banking_tool", tool_error)
    raw = asyncio.run(agent_module._call_read_capability("get_recent_transactions", {"limit": 6}))
    envelope = json.loads(raw)

    assert calls == ["get_recent_transactions"]
    assert envelope["outcome"] == "REJECTED"
    assert envelope["error"] == {"code": "MCP_TOOL_EXECUTION_FAILED", "retryable": False}


def test_read_capability_uses_declared_bounded_retry_policy(monkeypatch):
    import asyncio
    import services.agent_runtime.agent as agent_module

    calls = []
    valid = create_result_envelope(
        "banking.transactions.recent", "EMPTY", data=[], trace_id="mcp-retry-ok"
    )

    async def flaky_connector(name, arguments=None):
        calls.append((name, arguments))
        if len(calls) == 1:
            raise RuntimeError("transient transport failure")
        return json.dumps(valid)

    monkeypatch.setattr(agent_module, "call_banking_tool", flaky_connector)
    raw = asyncio.run(agent_module._call_read_capability("get_recent_transactions", {"limit": 6}))
    envelope = json.loads(raw)

    assert len(calls) == 2
    assert calls[0] == calls[1] == ("get_recent_transactions", {"limit": 6})
    assert envelope["outcome"] == "EMPTY"
    assert envelope["evidence"]["provider_trace_id"] == "mcp-retry-ok"


def test_read_capability_rejects_malformed_connector_response(monkeypatch):
    import asyncio
    import services.agent_runtime.agent as agent_module

    async def malformed_connector(_name, _arguments=None):
        return json.dumps(
            {
                "schema_version": "1.0.0",
                "capability": "banking.transactions.recent",
                "outcome": "SUCCESS",
                "data": [{"transaction_id": "txn-1", "amount_minor": "not-an-integer"}],
                "evidence": {
                    "source": "mcp",
                    "scope": "authenticated_customer_transactions",
                    "retrieved_at": "2026-11-21T00:00:00Z",
                    "completeness": "COMPLETE",
                    "trace_id": "bad",
                },
                "error": None,
                "trace_id": "bad",
                "trust_class": "untrusted_banking_data",
            }
        )

    monkeypatch.setattr(agent_module, "call_banking_tool", malformed_connector)
    raw = asyncio.run(agent_module._call_read_capability("get_recent_transactions", {"limit": 6}))
    envelope = json.loads(raw)

    assert envelope["outcome"] == "INVALID_RESPONSE"
    assert envelope["error"]["code"] == "INVALID_CONNECTOR_RESPONSE"


def test_registry_declared_presentation_transform_pipeline_is_generic():
    context = {"selected_account_name": "Savings account", "selected_account_id": "acc-002"}
    evidence = {"scope": "authenticated_customer_transactions"}
    records = [
        {"account_name": "Everyday account", "transaction_id": "txn-1"},
        {"account_name": "Savings account", "transaction_id": "txn-2"},
    ]
    pipeline = [
        {
            "op": "filter_array_by_context",
            "property": "account_name",
            "context_key": "selected_account_name",
            "ignore_values": ["All accounts"],
        },
        {
            "op": "set_evidence_scope_from_context",
            "context_key": "selected_account_id",
            "prefix": "selected_account:",
        },
    ]

    transformed = _apply_presentation_transform(pipeline, records, context, evidence)

    assert [item["transaction_id"] for item in transformed] == ["txn-2"]
    assert evidence["scope"] == "selected_account:acc-002"


def test_registry_declared_account_transform_wraps_balance_object():
    account = {
        "account_id": "acc-001",
        "account_name": "Everyday",
        "currency": "EUR",
        "balance_minor": 1000,
    }
    assert _apply_presentation_transform([{"op": "wrap_object_as_array"}], account, {}, {}) == [
        account
    ]


def test_read_capability_honors_retry_on_categories(monkeypatch):
    import asyncio
    from copy import deepcopy
    import services.agent_runtime.agent as agent_module

    registered = agent_module.capability_by_name("get_recent_transactions", kind="mcp")
    capability_id, contract = registered
    restricted = deepcopy(contract)
    restricted["execution"]["retry"]["retry_on"] = ["TIMEOUT"]
    monkeypatch.setattr(
        agent_module, "capability_by_name", lambda _name, kind=None: (capability_id, restricted)
    )
    calls = []

    async def transport_failure(_name, _arguments=None):
        calls.append(1)
        raise RuntimeError("transient transport failure")

    monkeypatch.setattr(agent_module, "call_banking_tool", transport_failure)
    raw = asyncio.run(agent_module._call_read_capability("get_recent_transactions", {"limit": 6}))
    envelope = json.loads(raw)

    assert len(calls) == 1
    assert envelope["outcome"] == "UNAVAILABLE"
    assert envelope["error"]["code"] == "MCP_TRANSPORT_UNAVAILABLE"


def test_invalid_application_context_is_ignored_for_preflight(monkeypatch):
    import services.agent_runtime.agent as agent_module

    state = {
        "messages": [HumanMessage(content="How much money do I have?")],
        "copilotkit": {
            "context": [
                {
                    "description": "Current ORAIflow workspace and account selection",
                    "value": {
                        "workspace": "Overview",
                        "selected_account_id": "acc-999",
                        "selected_account_name": "Forged",
                        "accounts": [],
                        "contacts": [],
                    },
                }
            ],
        },
    }
    assert agent_module._selected_account_context(state) == {}


CONTACTS = [
    {
        "contact_id": "ct-john-carter",
        "full_name": "John Carter",
        "bank_name": "Northstar Bank",
        "account_reference_masked": "DEMO ···· 1042",
        "currency": "EUR",
        "status": "ACTIVE",
    },
    {
        "contact_id": "ct-john-mitchell",
        "full_name": "John Mitchell",
        "bank_name": "Union Financial",
        "account_reference_masked": "DEMO ···· 2781",
        "currency": "EUR",
        "status": "ACTIVE",
    },
]


def _contact_context():
    return {
        "workspace": "Contacts",
        "selected_account_id": "acc-001",
        "selected_account_name": "Everyday account",
        "accounts": [
            {"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR"}
        ],
        "contacts": [
            {
                "contact_id": contact["contact_id"],
                "full_name": contact["full_name"],
                "status": contact["status"],
            }
            for contact in CONTACTS
        ],
    }


def test_model_cannot_open_transfer_without_a_current_turn_contact_search():
    proposed = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "prepare_transfer_draft",
                "args": {
                    "beneficiary": "John Carter",
                    "beneficiary_contact_id": "ct-john-carter",
                    "amount": "50",
                    "currency": "EUR",
                    "from_account_id": "acc-001",
                },
                "id": "model-transfer",
                "type": "tool_call",
            }
        ],
    )
    result = _validate_proposed_tool_calls(
        proposed, [HumanMessage(content="Send 50 EUR to John Carter")], _contact_context()
    )
    assert not result.tool_calls
    assert "verify the recipient" in result.content


def test_model_cannot_choose_between_duplicate_contacts_without_clarification():
    proposed = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "prepare_transfer_draft",
                "args": {
                    "beneficiary": "John Carter",
                    "beneficiary_contact_id": "ct-john-carter",
                    "amount": "50",
                    "currency": "EUR",
                    "from_account_id": "acc-001",
                },
                "id": "model-transfer",
                "type": "tool_call",
            }
        ],
    )
    messages = [
        HumanMessage(content="Send 50 EUR to John"),
        _trusted_tool_message("search_contacts", CONTACTS, "contact-search-3"),
    ]
    result = _validate_proposed_tool_calls(proposed, messages, _contact_context())
    assert not result.tool_calls
    assert "Which person did you mean?" in result.content
    assert "John Carter" in result.content and "John Mitchell" in result.content
    assert workflow_from_message(result)["stage"] == "AWAITING_CONTACT"


def test_model_cannot_choose_first_source_account_when_multiple_are_available():
    proposed = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "prepare_transfer_draft",
                "args": {
                    "beneficiary": "John Carter",
                    "beneficiary_contact_id": "ct-john-carter",
                    "amount": "50",
                    "currency": "EUR",
                    "from_account_id": "acc-001",
                },
                "id": "model-transfer-source",
                "type": "tool_call",
            }
        ],
    )
    context = {
        "selected_account_id": None,
        "accounts": [
            {"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR"},
            {"account_id": "acc-002", "account_name": "Savings account", "currency": "EUR"},
            {"account_id": "acc-003", "account_name": "Travel wallet", "currency": "EUR"},
        ],
        "contacts": [
            {
                "contact_id": CONTACTS[0]["contact_id"],
                "full_name": CONTACTS[0]["full_name"],
                "status": "ACTIVE",
            },
        ],
    }
    messages = [
        HumanMessage(content="Send 50 EUR to John Carter"),
        _trusted_tool_message("search_contacts", [CONTACTS[0]], "source-search-1"),
    ]
    result = _validate_proposed_tool_calls(proposed, messages, context)
    assert not result.tool_calls
    assert "Which source account should I use?" in result.content
    assert "first account" in result.content


def test_model_may_use_one_unique_currency_account_without_a_current_selection():
    """A sole compatible account avoids clarification but is still re-read before drafting."""
    proposed = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "prepare_transfer_draft",
                "args": {
                    "beneficiary": "John Carter",
                    "beneficiary_contact_id": "ct-john-carter",
                    "amount": "50",
                    "currency": "EUR",
                    "from_account_id": "acc-001",
                },
                "id": "model-transfer-unique-source",
                "type": "tool_call",
            }
        ],
    )
    context = {
        "selected_account_id": None,
        "accounts": [
            {"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR"}
        ],
        "contacts": [
            {
                "contact_id": CONTACTS[0]["contact_id"],
                "full_name": CONTACTS[0]["full_name"],
                "status": CONTACTS[0]["status"],
            }
        ],
    }
    request = HumanMessage(content="Send 50 EUR to John Carter")
    messages = [request, _trusted_tool_message("search_contacts", [CONTACTS[0]], "source-search-2")]

    # The model still cannot execute the draft action directly. The runtime-owned
    # workflow must select the sole currency-compatible source and verify it via MCP.
    rejected = _validate_proposed_tool_calls(proposed, messages, context)
    assert not rejected.tool_calls
    assert "verify the saved recipient and source account" in rejected.content.casefold()

    from services.agent_runtime.agent import (
        _handle_transfer_workflow,
        _start_explicit_transfer_workflow,
    )

    started = _start_explicit_transfer_workflow({"messages": [request]}, context)
    assert started is not None
    assert started.tool_calls[0]["name"] == "search_contacts"
    contact_result = _trusted_tool_message(
        "search_contacts", [CONTACTS[0]], "source-search-canonical"
    )
    state = {
        "messages": [request, started, contact_result],
        "copilotkit": {
            "context": [
                {
                    "description": "Current ORAIflow workspace and account selection",
                    "value": context,
                }
            ]
        },
    }
    verification = _handle_transfer_workflow(state, context)
    assert verification is not None
    assert verification.tool_calls[0]["name"] == "list_customer_accounts"
    workflow = workflow_from_message(verification)
    assert workflow["source_account_id"] == "acc-001"
    assert workflow["stage"] == "VERIFYING_SOURCE_ACCOUNT"


def _agent_test_context():
    return {
        "workspace": "Overview",
        "selected_account_id": None,
        "selected_account_name": "All accounts",
        "accounts": [
            {"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR"},
            {"account_id": "acc-002", "account_name": "Savings account", "currency": "EUR"},
            {"account_id": "acc-003", "account_name": "Travel wallet", "currency": "EUR"},
        ],
        "contacts": [
            {"contact_id": "ct-john-carter", "full_name": "John Carter", "status": "ACTIVE"},
            {"contact_id": "ct-john-mitchell", "full_name": "John Mitchell", "status": "ACTIVE"},
            {"contact_id": "ct-lucas-meyer", "full_name": "Lucas Meyer", "status": "ACTIVE"},
        ],
    }


def test_explicit_transfer_replay_reuses_identity_and_never_issues_a_second_search_or_draft():
    from services.agent_runtime.agent import _start_explicit_transfer_workflow
    from services.agent_runtime.action_response_guard import (
        has_successful_frontend_ack_after_latest_user,
    )

    context = _agent_test_context()
    request = HumanMessage(
        content="send 50 EUR to Lucas from everyday",
        id="agui-user-message-lucas-001",
    )
    first = _start_explicit_transfer_workflow(
        {"messages": [request], "thread_id": "thread-lucas"}, context
    )
    assert first is not None
    assert first.tool_calls[0]["name"] == "search_contacts"
    workflow = latest_pending_workflow([request, first])
    assert workflow is not None

    replay_context = {
        **context,
        "transfer_draft_state": {
            "workflow_id": workflow["workflow_id"],
            "action_id": workflow["action_id"],
            "status": "open",
        },
    }
    replay = _start_explicit_transfer_workflow(
        {"messages": [request], "thread_id": "thread-lucas"}, replay_context
    )
    assert replay is not None
    assert not replay.tool_calls
    assert "already open" in replay.content.casefold()
    assert replay.name == "oraiflow_action_ack"
    assert has_successful_frontend_ack_after_latest_user([request, replay])


def test_new_transfer_is_blocked_while_another_editable_transfer_draft_is_open():
    from services.agent_runtime.agent import _start_explicit_transfer_workflow

    context = _agent_test_context()
    context["transfer_draft_state"] = {
        "workflow_id": "trf-existing-draft",
        "action_id": "act-existing-draft",
        "status": "open",
    }
    request = HumanMessage(
        content="send 75 EUR to Lucas from everyday",
        id="agui-user-message-lucas-003",
    )
    response = _start_explicit_transfer_workflow(
        {"messages": [request], "thread_id": "thread-lucas"}, context
    )
    assert response is not None
    assert not response.tool_calls
    assert "already open" in response.content.casefold()
    assert response.name == "oraiflow_action_ack"


def test_closed_transfer_request_replay_is_terminal_and_does_not_reopen():
    from services.agent_runtime.agent import _start_explicit_transfer_workflow

    context = _agent_test_context()
    request = HumanMessage(
        content="send 50 EUR to Lucas from everyday",
        id="agui-user-message-lucas-002",
    )
    first = _start_explicit_transfer_workflow(
        {"messages": [request], "thread_id": "thread-lucas"}, context
    )
    workflow = latest_pending_workflow([request, first])
    assert workflow is not None
    closed_context = {
        **context,
        "transfer_draft_state": {
            "workflow_id": workflow["workflow_id"],
            "action_id": workflow["action_id"],
            "status": "dismissed",
        },
    }
    replay = _start_explicit_transfer_workflow(
        {"messages": [request], "thread_id": "thread-lucas"}, closed_context
    )
    assert replay is not None
    assert not replay.tool_calls
    assert "won't reopen" in replay.content.casefold()


def _transfer_assessment(beneficiary, amount, quote):
    return {
        "intent_id": "TRANSFER_PREPARATION",
        "intent_confidence": 0.96,
        "slot_confidence": 0.95,
        "entity_confidence": 0.87,
        "capability_id": "banking.contacts.search",
        "capability_selection_confidence": 0.94,
        "resolution": "READY",
        "slots": {"amount": str(amount), "currency": "EUR"},
        "entities": {"beneficiary": beneficiary},
        "evidence": [
            {"field": "intent", "source": "current_turn", "quote": quote},
            {"field": "amount", "source": "current_turn", "quote": str(amount)},
            (
                {"field": "currency", "source": "current_turn", "quote": "EUR"}
                if "EUR" in quote
                else {
                    "field": "currency",
                    "source": "application_context",
                    "quote": '"currency": "EUR"',
                }
            ),
            {"field": "beneficiary", "source": "current_turn", "quote": beneficiary},
        ],
    }


def test_explicit_transfer_request_enters_the_canonical_workflow_before_semantic_fallback():
    from services.agent_runtime.agent import _start_explicit_transfer_workflow

    context = _agent_test_context()
    request = HumanMessage(content="send 50 EUR to Lucas")
    state = {"messages": [request]}
    started = _start_explicit_transfer_workflow(state, context)

    assert started is not None
    # Copilot renders this runtime-owned read as tool activity; do not duplicate
    # the progress status as a second assistant chat message.
    assert started.content == ""
    assert started.tool_calls[0]["name"] == "search_contacts"
    assert started.tool_calls[0]["args"] == {"name_query": "Lucas"}
    workflow = latest_pending_workflow([request, started])
    assert workflow is not None
    assert workflow["workflow_type"] == "TRANSFER_PREPARATION"
    assert workflow["amount"] == "50"
    assert workflow["currency"] == "EUR"
    assert workflow["beneficiary_query"] == "Lucas"


def test_explicit_transfer_source_preference_is_preserved_for_verified_account_ranking():
    from services.agent_runtime.agent import _start_explicit_transfer_workflow

    context = _agent_test_context()
    request = HumanMessage(content="send 50 EUR to John from the account with the highest amount")
    started = _start_explicit_transfer_workflow({"messages": [request]}, context)
    assert started is not None
    workflow = latest_pending_workflow([request, started])
    assert workflow is not None
    assert workflow["source_account_preference"] == "highest_balance"
    assert workflow["source_account_id"] == ""


def test_production_transfer_continues_carter_then_everyday_and_opens_once():
    from services.agent_runtime.agent import _handle_transfer_workflow

    context = _agent_test_context()
    current_turn = "send 50 EUR to John"
    assessment = _transfer_assessment("John", "50", current_turn)
    request = HumanMessage(content=current_turn)
    proposed = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "search_contacts",
                "args": {"name_query": "John", "interpretation_assessment": assessment},
                "id": "search-john",
                "type": "tool_call",
            }
        ],
    )
    validated = _validate_proposed_tool_calls(proposed, [request], context)
    result = create_result_envelope(
        "banking.contacts.search",
        "SUCCESS",
        data=[
            {
                "contact_id": "ct-john-carter",
                "full_name": "John Carter",
                "bank_name": "Northstar Bank",
                "account_reference_masked": "···· 1042",
                "currency": "EUR",
                "status": "ACTIVE",
            },
            {
                "contact_id": "ct-john-mitchell",
                "full_name": "John Mitchell",
                "bank_name": "Union Financial",
                "account_reference_masked": "···· 2781",
                "currency": "EUR",
                "status": "ACTIVE",
            },
        ],
        trace_id="contacts-test",
    )
    searched = ToolMessage(
        name="search_contacts", tool_call_id="search-john", content=json.dumps(result)
    )
    state = {
        "messages": [request, validated, searched],
        "copilotkit": {
            "context": [
                {
                    "description": "Current ORAIflow workspace and account selection",
                    "value": context,
                }
            ],
            "actions": [{"name": "prepare_transfer_draft"}],
        },
    }

    clarification = _handle_transfer_workflow(state, context)
    assert "John Carter" in clarification.content and "John Mitchell" in clarification.content
    assert workflow_from_message(clarification)["stage"] == "AWAITING_CONTACT"

    selected_person = HumanMessage(content="Carter")
    state["messages"].extend([clarification, selected_person])
    account_question = _handle_transfer_workflow(state, context)
    assert "Everyday" in account_question.content
    assert workflow_from_message(account_question)["contact"]["contact_id"] == "ct-john-carter"
    assert workflow_from_message(account_question)["stage"] == "AWAITING_SOURCE_ACCOUNT"

    state["messages"].extend([account_question, HumanMessage(content="Everyday")])
    account_read = _handle_transfer_workflow(state, context)
    assert account_read.tool_calls[0]["name"] == "list_customer_accounts"
    state["messages"].append(account_read)
    verified_accounts = create_result_envelope(
        "banking.accounts.list",
        "SUCCESS",
        data=[
            {
                "account_id": "acc-001",
                "account_name": "Everyday account",
                "account_type": "CURRENT",
                "currency": "EUR",
                "balance_minor": 2013080,
                "status": "ACTIVE",
            },
            {
                "account_id": "acc-002",
                "account_name": "Savings account",
                "account_type": "SAVINGS",
                "currency": "EUR",
                "balance_minor": 860000,
                "status": "ACTIVE",
            },
            {
                "account_id": "acc-003",
                "account_name": "Travel wallet",
                "account_type": "CURRENT",
                "currency": "EUR",
                "balance_minor": 248750,
                "status": "ACTIVE",
            },
        ],
        trace_id="accounts-verify-everyday",
    )
    state["messages"].append(
        ToolMessage(
            name="list_customer_accounts",
            tool_call_id=account_read.tool_calls[0]["id"],
            content=json.dumps(verified_accounts),
        )
    )
    open_draft = _handle_transfer_workflow(state, context)
    assert open_draft.tool_calls[0]["name"] == "prepare_transfer_draft"
    args = open_draft.tool_calls[0]["args"]
    assert args["amount"] == "50"
    assert args["beneficiary_contact_id"] == "ct-john-carter"
    assert args["from_account_id"] == "acc-001"
    assert args["workflow_id"] and args["action_id"]

    draft_result = create_result_envelope(
        "frontend.transfer.prepare_draft",
        "SUCCESS",
        data={
            "status": "draft_opened",
            "workflow_id": args["workflow_id"],
            "action_id": args["action_id"],
        },
        trace_id="draft-test",
    )
    draft_tool_message = ToolMessage(
        name="prepare_transfer_draft",
        tool_call_id=args["action_id"],
        content=json.dumps(draft_result),
    )
    state["messages"].extend([open_draft, draft_tool_message])
    opened = _handle_transfer_workflow(state, context)
    assert "editable transfer draft is open" in opened.content.lower()
    from shared.pending_task import pending_task_from_message

    completed_task = pending_task_from_message(opened)
    assert completed_task is not None
    assert completed_task.status == "COMPLETED"
    assert completed_task.stage == "DRAFT_OPENED"
    assert workflow_from_message(opened) is None

    state["messages"].extend([opened, HumanMessage(content="Show my account balances")])
    assert _handle_transfer_workflow(state, context) is None


def test_production_transfer_highest_amount_reply_schedules_verified_balance_read_without_llm_reinterpretation():
    from services.agent_runtime.agent import _handle_transfer_workflow

    context = _agent_test_context()
    request = HumanMessage(content="send 50 EUR to John")
    contact_assessment = _transfer_assessment("John", "50", "send 50 EUR to John")
    proposed = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "search_contacts",
                "args": {"name_query": "John", "interpretation_assessment": contact_assessment},
                "id": "search-john",
                "type": "tool_call",
            }
        ],
    )
    validated = _validate_proposed_tool_calls(proposed, [request], context)
    contact_result = create_result_envelope(
        "banking.contacts.search",
        "SUCCESS",
        data=[
            {
                "contact_id": "ct-john-carter",
                "full_name": "John Carter",
                "bank_name": "Northstar Bank",
                "account_reference_masked": "···· 1042",
                "currency": "EUR",
                "status": "ACTIVE",
            },
            {
                "contact_id": "ct-john-mitchell",
                "full_name": "John Mitchell",
                "bank_name": "Union Financial",
                "account_reference_masked": "···· 2781",
                "currency": "EUR",
                "status": "ACTIVE",
            },
        ],
        trace_id="contacts-john",
    )
    contact_message = ToolMessage(
        name="search_contacts", tool_call_id="search-john", content=json.dumps(contact_result)
    )
    state = {
        "messages": [request, validated, contact_message],
        "copilotkit": {
            "context": [
                {
                    "description": "Current ORAIflow workspace and account selection",
                    "value": context,
                }
            ],
            "actions": [{"name": "prepare_transfer_draft"}],
        },
    }
    ask_contact = _handle_transfer_workflow(state, context)
    assert "John Carter" in ask_contact.content and "John Mitchell" in ask_contact.content
    state["messages"].append(ask_contact)

    surname = HumanMessage(content="carter")
    state["messages"].append(surname)
    ask_account = _handle_transfer_workflow(state, context)
    assert "Which EUR account" in ask_account.content
    workflow = workflow_from_message(ask_account)
    assert workflow["contact"]["contact_id"] == "ct-john-carter"
    state["messages"].append(ask_account)

    # The comparative clarification is resolved by the bounded workflow slot,
    # not by depending on a weaker model to manufacture another tool call.
    preference_turn = HumanMessage(content="the highest amount one")
    state["messages"].append(preference_turn)
    account_read = _handle_transfer_workflow(state, context)
    assert account_read.tool_calls[0]["name"] == "list_customer_accounts"
    assert account_read.tool_calls[0]["args"] == {}
    assert account_read.additional_kwargs["oraiflow_runtime_owned_read_ids"] == [
        account_read.tool_calls[0]["id"]
    ]
    assert workflow_from_message(account_read)["source_account_preference"] == "highest_balance"
    state["messages"].append(account_read)

    verified_result = create_result_envelope(
        "banking.accounts.list",
        "SUCCESS",
        data=[
            {
                "account_id": "acc-001",
                "account_name": "Everyday account",
                "account_type": "CURRENT",
                "currency": "EUR",
                "balance_minor": 2013080,
                "status": "ACTIVE",
            },
            {
                "account_id": "acc-002",
                "account_name": "Savings account",
                "account_type": "SAVINGS",
                "currency": "EUR",
                "balance_minor": 860000,
                "status": "ACTIVE",
            },
            {
                "account_id": "acc-003",
                "account_name": "Travel wallet",
                "account_type": "CURRENT",
                "currency": "EUR",
                "balance_minor": 248750,
                "status": "ACTIVE",
            },
        ],
        trace_id="accounts-ranked",
    )
    account_result = ToolMessage(
        name="list_customer_accounts",
        tool_call_id=account_read.tool_calls[0]["id"],
        content=json.dumps(verified_result),
    )
    state["messages"].append(account_result)

    open_draft = _handle_transfer_workflow(state, context)
    assert open_draft.tool_calls[0]["name"] == "prepare_transfer_draft"
    assert open_draft.tool_calls[0]["args"]["from_account_id"] == "acc-001"
    assert open_draft.tool_calls[0]["args"]["beneficiary_contact_id"] == "ct-john-carter"
    assert open_draft.tool_calls[0]["args"]["amount"] == "50"
    assert open_draft.tool_calls[0]["args"]["currency"] == "EUR"


def test_agent_assisted_frontend_action_requires_assessment_then_strips_wrapper():
    context = _agent_test_context()
    current_turn = "Select my Savings account"
    assessment = {
        "intent_id": "ACCOUNT_SELECTION",
        "intent_confidence": 0.97,
        "slot_confidence": 0.96,
        "entity_confidence": 0.98,
        "capability_id": "frontend.account.select",
        "capability_selection_confidence": 0.95,
        "resolution": "READY",
        "slots": {"account_id": "acc-002"},
        "entities": {},
        "evidence": [
            {"field": "intent", "source": "current_turn", "quote": current_turn},
            {
                "field": "account_id",
                "source": "application_context",
                "quote": '"account_id": "acc-002"',
            },
        ],
    }
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "select_account",
                "args": {"account_id": "acc-002", "interpretation_assessment": assessment},
                "id": "select-savings",
                "type": "tool_call",
            }
        ],
    )
    validated = _validate_proposed_tool_calls(
        response, [HumanMessage(content=current_turn)], context
    )
    assert validated.tool_calls[0]["args"] == {"account_id": "acc-002"}


def test_show_contacts_routes_to_the_registered_contacts_workspace():
    state = {
        "messages": [HumanMessage(content="show me my contacts")],
        "copilotkit": {"actions": [{"name": "navigate_workspace"}]},
    }
    message = _workspace_navigation_step("show me my contacts", state)
    assert message is not None
    assert message.tool_calls[0]["name"] == "navigate_workspace"
    assert message.tool_calls[0]["args"]["workspace"] == "Contacts"
    assert message.tool_calls[0]["args"]["action_id"].startswith("nav-")
    assert message.tool_calls[0]["id"] == message.tool_calls[0]["args"]["action_id"]


def test_successful_workspace_navigation_receipt_gets_a_matching_customer_acknowledgement():
    receipt = create_result_envelope(
        "frontend.workspace.navigate",
        "SUCCESS",
        data={"status": "opened", "workspace": "Controls & audit"},
        trace_id="nav-ack-test",
    )
    state = {
        "messages": [
            ToolMessage(
                name="navigate_workspace", tool_call_id="nav-call-1", content=json.dumps(receipt)
            )
        ]
    }
    message = _frontend_action_acknowledgement(state)
    assert message is not None
    assert message.content == "I've opened the Controls & audit workspace for you."
    assert message.additional_kwargs["oraiflow_frontend_action_receipt"]["status"] == "opened"


def test_rejected_workspace_navigation_never_gets_a_false_success_message():
    receipt = create_result_envelope(
        "frontend.workspace.navigate",
        "REJECTED",
        error={
            "code": "WORKSPACE_NOT_AVAILABLE",
            "retryable": False,
            "message": "The application rejected this workspace.",
        },
        trace_id="nav-ack-reject-test",
    )
    state = {
        "messages": [
            ToolMessage(
                name="navigate_workspace", tool_call_id="nav-call-2", content=json.dumps(receipt)
            )
        ]
    }
    message = _frontend_action_acknowledgement(state)
    assert message is not None
    assert "rejected" in message.content.casefold() or "couldn't" in message.content.casefold()
    assert "I've opened" not in message.content


def test_transfer_clarification_only_asks_for_the_missing_currency():
    message = _customer_assessment_message(
        {"decision": "CLARIFY", "code": "VALUE_EVIDENCE_MISSING", "fields": ["currency"]},
        None,
    )
    assert "understood the amount and recipient" in message.content
    assert "Which currency" in message.content
    assert "repeat the amount" not in message.content


def test_academy_personal_financial_health_question_schedules_registered_read_once():
    question = (
        "Explain borrowing and leverage in plain language. "
        "How does taking another loan affect my recorded Financial Health badge?"
    )
    state = {"messages": [HumanMessage(content=question)], "copilotkit": {}}

    read = _academy_personal_data_read_step(state)

    capability = get_registry()["capabilities"]["banking.financial_health.overview"]
    assert read is not None
    assert len(read.tool_calls) == 1
    assert read.tool_calls[0]["name"] == capability["name"]
    assert read.tool_calls[0]["args"] == {}
    assert read.additional_kwargs["oraiflow_runtime_owned_read_ids"] == [read.tool_calls[0]["id"]]

    snapshot = calculate_recorded_financial_health(
        [{"currency": "EUR", "balance_minor": 2_000_000, "status": "ACTIVE"}],
        [{"currency": "EUR", "outstanding_principal_minor": 500_000, "demo_only": True}],
    )
    envelope = create_result_envelope(
        "banking.financial_health.overview", "SUCCESS", data=snapshot, trace_id="academy-snapshot"
    )
    existing = ToolMessage(
        name=capability["name"], tool_call_id="read-1", content=json.dumps(envelope)
    )
    resumed = {"messages": [HumanMessage(content=question), read, existing], "copilotkit": {}}
    assert _academy_personal_data_read_step(resumed) is None
    explanation = _academy_personal_data_route(resumed)
    assert explanation is not None
    assert "Financial Health badge" in explanation.content


def test_academy_general_credit_score_question_does_not_schedule_personal_snapshot():
    question = (
        "How is a credit score different from an ORAIflow Financial Health badge, "
        "and what habits can help maintain a healthy credit history?"
    )
    state = {"messages": [HumanMessage(content=question)], "copilotkit": {}}
    assert _academy_personal_data_read_step(state) is None
    from services.agent_runtime.academy import academy_direct_guidance_for_request

    guidance = academy_direct_guidance_for_request(question)
    assert guidance is not None
    assert "credit score" in guidance.casefold()

    # The paired personal wording explicitly claims ownership and therefore
    # must schedule the registered read before comparing the customer's badge.
    personal_question = (
        "How is a credit score different from my ORAIflow Financial Health badge, "
        "and what habits can help maintain a healthy credit history?"
    )
    personal_state = {
        "messages": [HumanMessage(content=personal_question)],
        "copilotkit": {},
    }
    personal_read = _academy_personal_data_read_step(personal_state)
    capability = get_registry()["capabilities"]["banking.financial_health.overview"]
    assert personal_read is not None
    assert personal_read.tool_calls[0]["name"] == capability["name"]


def test_unrelated_salary_question_does_not_inherit_a_pending_transfer_clarification():
    prior = AIMessage(
        content="Please provide the missing recipient.",
        additional_kwargs={
            "oraiflow_task_plan": {
                "schema_version": "1.0.0",
                "tasks": [
                    {
                        "task_id": "transfer-pending-1",
                        "intent_id": "TRANSFER_PREPARATION",
                        "status_hint": "CLARIFICATION_REQUIRED",
                        "reason_code": "VALUE_EVIDENCE_MISSING",
                        "missing_fields": ["beneficiary"],
                        "requested_outcome": "ACTION",
                    }
                ],
            }
        },
    )
    salary_question = HumanMessage(content="What was my recent salary?")
    assert _pending_semantic_tasks([prior, salary_question], salary_question.content) == []


def test_short_answer_and_explicit_followup_marker_preserve_pending_task_context():
    prior = AIMessage(
        content="Which month did you mean?",
        additional_kwargs={
            "oraiflow_task_plan": {
                "schema_version": "1.0.0",
                "tasks": [
                    {
                        "task_id": "analysis-pending-1",
                        "intent_id": "TRANSACTION_ANALYSIS",
                        "status_hint": "CLARIFICATION_REQUIRED",
                        "reason_code": "VALUE_EVIDENCE_MISSING",
                        "missing_fields": ["period"],
                        "requested_outcome": "INFORMATION",
                    }
                ],
            }
        },
    )
    for answer in ("November", "November 2025", "What about November?"):
        current = HumanMessage(content=answer)
        pending = _pending_semantic_tasks([prior, current], answer)
        assert len(pending) == 1
        assert pending[0]["intent_id"] == "TRANSACTION_ANALYSIS"
