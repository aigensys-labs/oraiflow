"""Static source contracts for the AG-UI demo adapter.

These checks intentionally do not import the optional LangGraph/AG-UI runtime,
so adapter ownership regressions remain testable in lightweight environments.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_PATH = ROOT / "services" / "agent_runtime" / "agent.py"
AGUI_PATH = ROOT / "services" / "agent_runtime" / "agui.py"


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_agui_imports_only_existing_agent_runtime_symbols():
    agent_tree = _module(AGENT_PATH)
    agui_tree = _module(AGUI_PATH)
    defined = {
        node.name
        for node in agent_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    defined.update(
        target.id
        for node in agent_tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    )

    missing = []
    for node in agui_tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module != "services.agent_runtime.agent":
            continue
        for alias in node.names:
            if alias.name not in defined:
                missing.append(alias.name)

    assert missing == [], f"AG-UI imports deleted Agent symbols: {missing}"
    assert "resolve_contact_transfer_step" not in defined


def test_demo_agui_graph_uses_the_canonical_transfer_workflow_owner():
    tree = _module(AGUI_PATH)
    source = AGUI_PATH.read_text(encoding="utf-8")

    assert "_start_explicit_transfer_workflow(transfer_state, context)" in source
    assert "_handle_transfer_workflow(transfer_state, context)" in source
    assert "resolve_contact_transfer_step" not in source

    # The demo adapter may execute only the two registered read operations
    # required by the canonical transfer state machine. Draft preparation is
    # returned to the browser as an editable frontend action.
    assert 'tool_name not in {"search_contacts", "list_customer_accounts"}' in source

    function_names = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "build_demo_agui_graph" in function_names


def test_no_parallel_legacy_contact_transfer_owner_remains_in_runtime_sources():
    search_roots = [ROOT / "services", ROOT / "shared"]
    forbidden = (
        "resolve_contact_transfer_step",
        "_contact_action_call",
        "_PENDING_TRANSFER_KEY",
    )
    hits = []
    for base in search_roots:
        for path in base.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in source:
                    hits.append(f"{path.relative_to(ROOT)}:{token}")
    assert hits == [], f"Legacy parallel transfer implementation remains: {hits}"


def test_call_model_resumes_pending_transfer_before_navigation_and_semantic_interpretation():
    source = AGENT_PATH.read_text(encoding="utf-8")
    start = source.index("    async def call_model(state: ORAIState)")
    end = source.find("\n    async def ", start + 10)
    body = source[start : end if end >= 0 else None]

    explicit_start = body.index("transfer_start = _start_explicit_transfer_workflow")
    transfer_resume = body.index("transfer_step = _handle_transfer_workflow")
    navigation_resume = body.index("navigation_step = _pending_navigation_message")
    assert explicit_start < transfer_resume < navigation_resume


def test_runtime_owned_transfer_calls_keep_progress_as_protocol_not_customer_prose():
    tree = _module(AGENT_PATH)
    target_names = {
        "_start_explicit_transfer_workflow",
        "_internal_read_tool_call",
        "_prepare_transfer_draft_action",
    }
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in target_names
    }
    assert set(functions) == target_names

    for name, function in functions.items():
        calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "AIMessage"
        ]
        assert calls, f"{name} must construct an assistant/tool message"
        for call in calls:
            content = next((kw.value for kw in call.keywords if kw.arg == "content"), None)
            if isinstance(content, ast.Name):
                assignments = [
                    node
                    for node in ast.walk(function)
                    if isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == content.id
                        for target in node.targets
                    )
                ]
                text_values = [
                    node.value
                    for assignment in assignments
                    for node in ast.walk(assignment.value)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                ]
            else:
                text_values = (
                    [
                        node.value
                        for node in ast.walk(content)
                        if isinstance(node, ast.Constant) and isinstance(node.value, str)
                    ]
                    if content is not None
                    else []
                )
            # Progress is already shown by the frontend's tool activity UI.
            # Keep these protocol tool-call messages text-empty so the customer
            # receives only the workflow's targeted clarification and terminal ack.
            assert text_values == [""], f"{name} must not duplicate tool progress as chat prose"


def test_transaction_analysis_is_preserved_as_a_pending_semantic_task():
    source = AGENT_PATH.read_text(encoding="utf-8")
    start = source.index("def _pending_semantic_tasks(")
    end = source.find("\ndef ", start + 5)
    body = source[start : end if end >= 0 else None]
    assert '"TRANSACTION_ANALYSIS"' in body
    assert '"semantic_slots"' in body
    assert '"semantic_evidence"' in body


def test_graph_route_stops_after_a_successful_frontend_action_acknowledgement():
    tree = _module(AGENT_PATH)
    route = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "route"
    )
    calls = [
        node
        for node in ast.walk(route)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "has_successful_frontend_ack_after_latest_user"
    ]
    assert calls, "Graph re-entry must check the current turn's verified action receipt"
    assert any(
        isinstance(node, ast.Return) and isinstance(node.value, ast.Name) and node.value.id == "END"
        for node in ast.walk(route)
    )


def test_demo_agui_graph_resumes_pending_navigation_before_generic_help_or_chat_fallback():
    source = AGUI_PATH.read_text(encoding="utf-8")
    assert "_pending_navigation_message," in source
    continuation = source.index("navigation_continuation = _pending_navigation_message(state)")
    help_fallback = source.index("workspace_help = workspace_help_for_request", continuation)
    navigation_dispatch = source.index(
        "navigation_step = _workspace_navigation_step", help_fallback
    )
    assert continuation < help_fallback < navigation_dispatch


def test_snapshot_recovery_preserves_registered_copilotkit_presentation_and_frontend_calls():
    source = AGUI_PATH.read_text(encoding="utf-8")
    assert "ToolCallStartEvent" in source
    assert "ToolCallArgsEvent" in source
    assert "ToolCallEndEvent" in source
    assert "ToolCallResultEvent" in source
    assert 'registry.get("presentations")' in source
    assert 'contract.get("kind") == "mcp"' in source
    assert 'contract.get("kind") in {"frontend", "mcp"}' in source
    assert "snapshot_tool_call_names=_snapshot_recovery_tool_names()" in source


def test_demo_agui_stops_after_verified_frontend_action_acknowledgement():
    source = AGUI_PATH.read_text(encoding="utf-8")
    guard = source.index("if has_successful_frontend_ack_after_latest_user(messages):")
    latest_user = source.index("latest = next((m for m in reversed(messages)", guard)
    assert guard < latest_user
    assert 'return {"messages": []}' in source[guard:latest_user]


def test_demo_agui_wraps_successful_browser_receipts_in_a_durable_action_ack():
    source = AGUI_PATH.read_text(encoding="utf-8")
    assert "_frontend_action_ack_message," in source
    assert '"capability_id": registered[0]' in source
    assert '"tool_call_id": str(message_value(last_message, "tool_call_id", ""))' in source
    assert (
        'if outcome == "SUCCESS" and status in {"draft_opened", "opened", "selected", "cleared"}'
        in source
    )
    assert 'return {"messages": [_frontend_action_ack_message(reply, receipt)]}' in source


def test_demo_agui_reads_serialized_messages_via_shared_message_accessors():
    source = AGUI_PATH.read_text(encoding="utf-8")
    assert "from shared.pending_task import message_type, message_value" in source
    assert 'message_type(last_message) == "tool"' in source
    assert 'message_type(m) == "human"' in source
    assert 'message_value(latest, "content", "")' in source


def test_demo_agui_reaches_browser_receipt_acknowledgement_before_final_replay_guard():
    source = AGUI_PATH.read_text(encoding="utf-8")
    respond_start = source.index("async def _respond(state: ORAIState):")
    guard = source.index(
        "if has_successful_frontend_ack_after_latest_user(messages):", respond_start
    )
    tool_result = source.index(
        'if last_message is not None and message_type(last_message) == "tool":', respond_start
    )
    ack_return = source.index(
        'return {"messages": [_frontend_action_ack_message(reply, receipt)]}', tool_result
    )
    assert guard < tool_result < ack_return
    # The replay helper's unit contract deliberately excludes raw ToolMessages;
    # this block therefore runs on a new browser receipt and emits the final ack.


def test_explicit_transfer_replay_uses_stable_request_and_operation_ids():
    source = AGENT_PATH.read_text(encoding="utf-8")
    start = source.index("def _start_explicit_transfer_workflow(")
    end = source.find("\ndef ", start + 5)
    body = source[start : end if end >= 0 else None]
    assert "transfer_workflow_id(messages, thread_id)" in body
    assert "transfer_draft_replay_status(" in body
    assert "transfer_operation_id(" in body
    assert "transfer_workflow_replay_suppressed" in body


def test_transfer_frontend_action_registration_is_stable_across_lifecycle_updates():
    source = (ROOT / "apps" / "web" / "src" / "main.jsx").read_text(encoding="utf-8")
    marker = "name: 'prepare_transfer_draft'"
    start = source.index(marker)
    next_hook = source.find("\n  const dateLabel", start)
    body = source[start:next_hook]
    assert "transferActionRuntimeRef.current" in body
    assert "}, [])" in body
    assert "}, [accounts, contacts, transferDraftLifecycle])" not in body
