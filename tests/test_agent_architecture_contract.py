"""Static architecture guards for the model/runtime authority boundary.

These supplement behavior tests: the Agent Runtime integration dependencies
are intentionally optional in the lightweight source-audit environment.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_SOURCE = ROOT / "services/agent_runtime/agent.py"


def _bind_tools_arguments() -> list[ast.AST]:
    tree = ast.parse(AGENT_SOURCE.read_text(encoding="utf-8"))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == "bind_tools":
            calls.append(node.args[0] if node.args else ast.Constant(None))
    return calls


def _list_name(expression: ast.AST) -> str | None:
    if isinstance(expression, ast.List) and len(expression.elts) == 1:
        item = expression.elts[0]
        return item.id if isinstance(item, ast.Name) else None
    return None


def test_model_boundaries_expose_only_assessment_or_customer_response_contracts():
    # call_model uses provider-native structured output first and the required
    # oraiflow_assess_turn tool only as a compatibility adapter. Executable
    # banking/UI tools are constructed and validated by the runtime, not bound
    # to the interpretation model.
    bound = {_list_name(item) for item in _bind_tools_arguments()}
    assert bound == {"customer_response_tool"}

    interpretation_source = (ROOT / "services/agent_runtime/task_interpretation.py").read_text(encoding="utf-8")
    assert "model.bind_tools([assessment_tool], tool_choice=choice)" in interpretation_source
    assert "method(AssessmentToolArguments)" in interpretation_source
    assert "with_structured_output" in interpretation_source


def test_customer_response_composition_is_buffered_until_contract_validation():
    source = AGENT_SOURCE.read_text(encoding="utf-8")
    assert "invoke_internal_model(" in source
    assert "invoke_internal_model(\n                    response_model" in source
    streaming_source = (ROOT / "services/agent_runtime/streaming.py").read_text(encoding="utf-8")
    assert "config=internal_model_run_config(phase)" in streaming_source
    assert "response_model.astream(" not in source
    assert "_customer_response_from_protocol(" in source


def test_runtime_task_plans_keep_one_deadline_across_validation_rebuilds():
    tree = ast.parse(AGENT_SOURCE.read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_task_plan_records"
    ]
    assert len(calls) >= 3
    assert all(
        any(keyword.arg == "model_deadline_epoch" for keyword in call.keywords)
        for call in calls
    )


def test_all_private_model_phases_use_callback_isolation_and_agui_filters_internal_messages():
    source = AGENT_SOURCE.read_text(encoding="utf-8")
    assert source.count("invoke_internal_model(") >= 4
    assert ".ainvoke(" not in source[source.index("async def compose_grounded_response"):source.index("async def execute_tools")]

    agui_source = (ROOT / "services/agent_runtime/agui.py").read_text(encoding="utf-8")
    assert "synthesize_terminal_assistant_message(" in agui_source
    assert "filter_internal_assistant_messages(recovered_events)" in agui_source

    streaming_source = (ROOT / "services/agent_runtime/streaming.py").read_text(encoding="utf-8")
    assert '"callbacks": []' in streaming_source
    assert "_is_internal_assessment_text" in streaming_source


def test_internal_read_boundary_strips_interpretation_transport_metadata():
    source = AGENT_SOURCE.read_text(encoding="utf-8")
    start = source.index("async def _call_read_capability(")
    end = source.index("\ndef _build_mcp_agent_tool", start)
    body = source[start:end]
    assert "strip_assessment_tool_arguments(raw_arguments)" in body
    assert body.index("strip_assessment_tool_arguments(raw_arguments)") < body.index("validate_capability_input(capability_id, payload)")


def test_final_composed_message_retains_the_task_plan_for_follow_up_continuity():
    source = AGENT_SOURCE.read_text(encoding="utf-8")
    assert "return emit(_attach_task_plan(composed, existing_task_plan))" in source


def test_compose_passes_the_phase_specific_llm_timeout_settings():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name, default in (
        ("LLM_TURN_MODEL_BUDGET_SECONDS", "70"),
        ("LLM_INTERPRETATION_TIMEOUT_SECONDS", "50"),
        ("LLM_RESPONSE_TIMEOUT_SECONDS", "15"),
    ):
        assert f"{name}: ${{{name}:-{default}}}" in compose
        assert f"{name}={default}" in example
