from __future__ import annotations

import ast
import re
from pathlib import Path

from shared.contracts.registry import get_registry

ROOT = Path(__file__).resolve().parents[1]


def _python_source(path: str) -> tuple[str, ast.Module]:
    source = (ROOT / path).read_text(encoding="utf-8")
    return source, ast.parse(source)


def _mcp_tool_names() -> set[str]:
    _, tree = _python_source("services/mcp/main.py")
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "mcp"
                and decorator.func.attr == "tool"
            ):
                names.add(node.name)
    return names


def _fastapi_route_strings() -> set[str]:
    _, tree = _python_source("services/api/main.py")
    routes: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if not isinstance(decorator.func.value, ast.Name) or decorator.func.value.id != "app":
                continue
            if decorator.args and isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str):
                routes.add(f"{decorator.func.attr.upper()} {decorator.args[0].value}")
    return routes


def test_mcp_server_tools_match_every_registered_mcp_capability():
    registry = get_registry()
    declared = {item["name"] for item in registry["capabilities"].values() if item["kind"] == "mcp"}
    assert _mcp_tool_names() == declared


def test_frontend_tools_match_every_registered_frontend_capability():
    source = (ROOT / "apps/web/src/main.jsx").read_text(encoding="utf-8")
    registered = {item["name"] for item in get_registry()["capabilities"].values() if item["kind"] == "frontend"}
    implemented = set(re.findall(r"useFrontendTool\(\{\s*name:\s*['\"]([^'\"]+)['\"]", source))
    assert implemented == registered


def test_frontend_renderers_match_every_registered_presentation():
    source = (ROOT / "apps/web/src/main.jsx").read_text(encoding="utf-8")
    registered = {item["name"] for item in get_registry()["presentations"].values()}
    implemented = set(re.findall(r"useComponent\(\{\s*name:\s*['\"]([^'\"]+)['\"]", source))
    assert implemented == registered


def test_kernel_routes_match_registered_kernel_operations():
    registered = {item["route"] for item in get_registry()["kernel_operations"].values()}
    implemented = _fastapi_route_strings()
    assert registered <= implemented


def test_render_tool_status_views_cover_all_agent_invocable_capabilities():
    source = (ROOT / "apps/web/src/main.jsx").read_text(encoding="utf-8")
    registry = get_registry()
    registered = {
        item["name"] for item in registry["capabilities"].values()
        if item["authorization"]["agent_invocable"]
    }
    rendered = set(re.findall(r"useRenderTool\(\{\s*name:\s*['\"]([^'\"]+)['\"]", source))
    assert rendered == registered


def test_application_context_uses_generated_contract_in_browser_and_agent_runtime():
    frontend = (ROOT / "apps/web/src/main.jsx").read_text(encoding="utf-8")
    frontend_contracts = (ROOT / "apps/web/src/contracts.js").read_text(encoding="utf-8")
    agent = (ROOT / "services/agent_runtime/agent.py").read_text(encoding="utf-8")
    assert "validateApplicationContext" in frontend_contracts
    assert "validateApplicationContext({" in frontend
    assert "value: agentApplicationContext" in frontend
    assert "validate_application_context" in agent
    assert "return validate_application_context(context)" in agent


def test_mcp_tool_python_signatures_match_registry_input_contracts():
    import ast

    registry = get_registry()
    _, tree = _python_source("services/mcp/main.py")
    functions = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    type_names = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}

    def annotation_types(annotation):
        if isinstance(annotation, ast.Name):
            return {type_names[annotation.id]}, False
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            left, left_nullable = annotation_types(annotation.left)
            right, right_nullable = annotation_types(annotation.right)
            nullable = left_nullable or right_nullable or isinstance(annotation.left, ast.Constant) and annotation.left.value is None or isinstance(annotation.right, ast.Constant) and annotation.right.value is None
            return left | right, bool(nullable)
        if isinstance(annotation, ast.Constant) and annotation.value is None:
            return set(), True
        raise AssertionError(f"Unsupported MCP annotation syntax: {ast.dump(annotation)}")

    for contract in registry["capabilities"].values():
        if contract["kind"] != "mcp":
            continue
        function = functions[contract["name"]]
        args = [arg for arg in function.args.args if arg.arg not in {"self", "cls"}]
        schema = contract["input_schema"]
        properties = schema.get("properties", {})
        assert {arg.arg for arg in args} == set(properties)
        defaults = [None] * (len(args) - len(function.args.defaults)) + list(function.args.defaults)
        for argument, default in zip(args, defaults):
            annotation_types_set, annotation_nullable = annotation_types(argument.annotation)
            schema_type = properties[argument.arg]["type"]
            schema_types = set(schema_type) if isinstance(schema_type, list) else {schema_type}
            assert annotation_types_set == (schema_types - {"null"})
            assert annotation_nullable is ("null" in schema_types)
            required = argument.arg in schema.get("required", [])
            assert required is (default is None)
            if default is not None:
                assert isinstance(default, ast.Constant)
                assert default.value == properties[argument.arg].get("default")


def test_kernel_authorization_is_derived_from_registered_operation_contracts():
    source = (ROOT / "services/api/main.py").read_text(encoding="utf-8")
    assert "def _require_kernel_operation(operation_id: str)" in source
    assert "get_kernel_operation(operation_id)" in source
    for operation_id in get_registry()["kernel_operations"]:
        assert f'_require_kernel_operation("{operation_id}")' in source


def test_mcp_authorization_is_derived_from_registered_capability_contracts():
    source = (ROOT / "services/mcp/main.py").read_text(encoding="utf-8")
    assert "def _require_mcp_capability(capability_id: str)" in source
    assert "get_capability(capability_id)" in source
    for capability_id, contract in get_registry()["capabilities"].items():
        if contract["kind"] == "mcp":
            assert f'_require_mcp_capability("{capability_id}")' in source


def test_every_agent_invocable_capability_has_a_frontend_render_status():
    source = (ROOT / "apps/web/src/main.jsx").read_text(encoding="utf-8")
    registry = get_registry()
    registered = {
        contract["name"] for contract in registry["capabilities"].values()
        if contract["authorization"]["agent_invocable"]
    }
    rendered = set(re.findall(r"useRenderTool\(\{\s*name:\s*['\"]([^'\"]+)['\"]", source))
    assert rendered == registered


def test_frontend_capability_executor_enforces_registry_preconditions_and_result_outcomes():
    source = (ROOT / "apps/web/src/contracts.js").read_text(encoding="utf-8")
    app = (ROOT / "apps/web/src/main.jsx").read_text(encoding="utf-8")
    assert "function evaluateFrontendPreconditions(contract, input, context)" in source
    assert "evaluateFrontendPreconditions(contract, input, context)" in source
    assert "case 'workspace_allowlist'" in source
    assert "case 'context_field_present'" in source
    assert "case 'context_collection_contains_argument'" in source
    assert "outcome: 'INVALID_RESPONSE'" in source
    assert app.count("{ context: agentApplicationContext }") == 3
    assert "navigationActionRuntimeRef.current = { navigate, context: agentApplicationContext, navigationEpoch: manualNavigationEpochRef.current }" in app
    assert "navigationActionReceiptsRef.current" in app
    assert "executeNavigationOnce(navigationActionReceiptsRef.current, action_id" in app
    assert "  }, [])" in app


def test_registry_is_the_source_of_truth_for_navigation_prompt_and_browser_allowlist():
    agent = (ROOT / "services/agent_runtime/agent.py").read_text(encoding="utf-8")
    frontend = (ROOT / "apps/web/src/contracts.js").read_text(encoding="utf-8")
    assert 'get_registry().get("application_policy", {}).get("navigation", {}).get("pages", [])' in agent
    assert "CONTRACT_REGISTRY.application_policy?.navigation?.aliases" in frontend
    assert "const WORKSPACE_ALIASES" not in (ROOT / "apps/web/src/main.jsx").read_text(encoding="utf-8")
