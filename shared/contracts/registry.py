"""Load and enforce the language-neutral ORAIflow capability registry.

The JSON registry is the canonical wire-contract source. Python services
validate against it; the frontend schemas are generated from the same file.
This module intentionally does not grant authority: MCP and the Banking Kernel
still perform their own identity, capability and policy checks.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

_REGISTRY_FILE = Path(__file__).with_name("registry.json")
_FORMAT_CHECKER = FormatChecker()


class ContractViolation(ValueError):
    """A request or result does not conform to its declared contract."""

    def __init__(self, contract_id: str, section: str, message: str):
        super().__init__(f"{contract_id} {section}: {message}")
        self.contract_id = contract_id
        self.section = section
        self.message = message


def _resolve_local_refs(schema: Any, root: dict[str, Any], stack: tuple[str, ...] = ()) -> Any:
    """Expand local JSON Schema references against the canonical registry.

    External references are intentionally unsupported: contract validation must
    be deterministic and must not fetch schemas over the network at runtime.
    """
    if isinstance(schema, list):
        return [_resolve_local_refs(item, root, stack) for item in schema]
    if not isinstance(schema, dict):
        return schema
    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise RuntimeError(f"Unsupported non-local contract schema reference: {reference!r}")
        if reference in stack:
            raise RuntimeError(f"Recursive contract schema reference: {reference}")
        target: Any = root
        for raw_part in reference[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                raise RuntimeError(f"Unresolved contract schema reference: {reference}")
            target = target[part]
        resolved = _resolve_local_refs(target, root, (*stack, reference))
        siblings = {key: value for key, value in schema.items() if key != "$ref"}
        if not siblings:
            return resolved
        return {"allOf": [resolved, _resolve_local_refs(siblings, root, stack)]}
    return {key: _resolve_local_refs(value, root, stack) for key, value in schema.items()}


@lru_cache(maxsize=1)
def get_registry() -> dict[str, Any]:
    """Load and return the canonical language-neutral capability registry."""
    with _REGISTRY_FILE.open("r", encoding="utf-8") as stream:
        registry = json.load(stream)
    if registry.get("registry_version") not in {"1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.4.1", "1.4.2", "1.4.3", "1.4.4", "1.4.5", "1.4.6", "1.4.7", "1.4.8", "1.4.9", "1.5.0", "1.5.1", "1.5.2", "1.5.3", "1.5.4", "1.5.5", "1.5.6", "1.5.7", "1.5.8", "1.5.9", "1.5.10", "1.5.11", "1.5.12", "1.5.13", "1.5.14", "1.5.15", "1.5.16", "1.5.17"}:
        raise RuntimeError("Unsupported ORAIflow contract registry version")
    if registry.get("execution_lifecycle", {}).get("version") != "1.0.0":
        raise RuntimeError("Unsupported ORAIflow execution lifecycle version")
    for group in ("capabilities", "presentations", "kernel_operations"):
        for item in registry.get(group, {}).values():
            for schema_key in ("input_schema", "output_schema", "props_schema"):
                schema = item.get(schema_key)
                if schema is not None:
                    Draft202012Validator.check_schema(_resolve_local_refs(schema, registry))
    Draft202012Validator.check_schema(_resolve_local_refs(registry["result_envelope_schema"], registry))
    Draft202012Validator.check_schema(_resolve_local_refs(registry["application_context"]["schema"], registry))
    Draft202012Validator.check_schema(_resolve_local_refs(registry["turn_outcome_schema"], registry))
    Draft202012Validator.check_schema(_resolve_local_refs(registry["agent_turn_context_schema"], registry))
    Draft202012Validator.check_schema(_resolve_local_refs(registry["agent_turn_response_schema"], registry))
    validate_registry_consistency(registry)
    return registry


def validate_registry_consistency(registry: Mapping[str, Any] | None = None) -> None:
    """Validate cross-references and lifecycle semantics across the whole registry.

    JSON Schema validates individual payload shapes. This second layer validates
    the architecture connecting intents, capabilities, presentations and Kernel
    operations so incomplete declarations cannot quietly enter runtime.
    """
    if registry is None:
        with _REGISTRY_FILE.open("r", encoding="utf-8") as stream:
            registry = json.load(stream)
    registry = dict(registry)
    capabilities = registry.get("capabilities", {})
    presentations = registry.get("presentations", {})
    intents = registry.get("intents", {})
    kernel_operations = registry.get("kernel_operations", {})

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise RuntimeError(f"Invalid ORAIflow contract registry: {message}")

    capability_names: dict[str, str] = {}
    for capability_id, contract in capabilities.items():
        require(bool(contract.get("version")), f"{capability_id} is missing a version")
        require(contract.get("kind") in {"mcp", "frontend"}, f"{capability_id} has an unsupported kind")
        name = contract.get("name")
        require(isinstance(name, str) and name.strip(), f"{capability_id} is missing its exposed name")
        require(name not in capability_names, f"duplicate exposed capability name {name!r}")
        capability_names[name] = capability_id
        authorization = contract.get("authorization", {})
        require(bool(authorization.get("required_capability")), f"{capability_id} is missing its required capability")
        require(bool(authorization.get("principal")), f"{capability_id} is missing its principal declaration")
        require(isinstance(authorization.get("agent_invocable"), bool), f"{capability_id} must explicitly declare agent_invocable")
        execution = contract.get("execution", {})
        require(bool(execution.get("effect")), f"{capability_id} is missing its effect")
        if contract.get("kind") == "mcp" and authorization.get("agent_invocable") is True:
            require(execution.get("effect") in {"read", "read_only"}, f"agent-invocable MCP capability {capability_id} must be read-only")
        if contract.get("kind") == "frontend":
            require(isinstance(execution.get("model_invocable"), bool), f"{capability_id} must explicitly declare model_invocable")
            if execution.get("model_invocable") is True:
                require(authorization.get("agent_invocable") is True, f"model-invocable frontend capability {capability_id} must be agent-invocable")
                require(execution.get("effect") == "ui_state_only", f"model-invocable frontend capability {capability_id} must be UI-state-only")
        require(isinstance(execution.get("idempotent"), bool), f"{capability_id} must explicitly declare idempotency")
        require(isinstance(execution.get("timeout_ms"), int) and execution["timeout_ms"] > 0, f"{capability_id} has an invalid timeout")
        retry = execution.get("retry", {})
        strategy = retry.get("strategy")
        require(strategy in {"none", "bounded_exponential", "caller_controlled_idempotency_key"}, f"{capability_id} has an unsupported retry strategy")
        require(isinstance(retry.get("max_attempts"), int) and retry["max_attempts"] >= 1, f"{capability_id} has invalid retry attempts")
        require(isinstance(retry.get("retry_on"), list), f"{capability_id} retry_on must be a list")
        require(isinstance(retry.get("backoff_ms"), list) and all(isinstance(value, int) and value >= 0 for value in retry["backoff_ms"]), f"{capability_id} has invalid retry backoff")
        if strategy == "bounded_exponential":
            require(contract["kind"] == "mcp", f"{capability_id} cannot use connector retry for a non-MCP capability")
            require(execution["idempotent"] is True, f"{capability_id} cannot retry a non-idempotent operation")
            require(retry["max_attempts"] >= 2 and bool(retry["retry_on"]), f"{capability_id} bounded retry policy is incomplete")
            require(len(retry["backoff_ms"]) >= retry["max_attempts"] - 1, f"{capability_id} must declare a backoff for each retry")
        if contract["kind"] in {"mcp", "frontend"}:
            evidence = contract.get("evidence", {})
            require(bool(evidence.get("source")) and bool(evidence.get("scope")), f"{capability_id} is missing evidence source/scope")
            require(bool(contract.get("safe_failure_message")), f"{capability_id} is missing a customer-safe failure message")
            preconditions = contract.get("preconditions")
            require(isinstance(preconditions, list) and bool(preconditions), f"{capability_id} must declare its execution preconditions")
            precondition_ids = set()
            for precondition in preconditions:
                require(isinstance(precondition, dict) and bool(precondition.get("id")), f"{capability_id} has an invalid precondition declaration")
                require(precondition.get("enforced_by") in {"mcp_server", "frontend"}, f"{capability_id} precondition has an invalid enforcement boundary")
                require(precondition["id"] not in precondition_ids, f"{capability_id} has duplicate precondition {precondition['id']}")
                precondition_ids.add(precondition["id"])
                required_capability = precondition.get("required_capability")
                if required_capability:
                    require(required_capability == authorization.get("required_capability"), f"{capability_id} precondition capability differs from authorization declaration")

    application_policy = registry.get("application_policy", {})
    navigation_policy = application_policy.get("navigation", {})
    allowed_pages = navigation_policy.get("pages", [])
    aliases = navigation_policy.get("aliases", {})
    require(isinstance(allowed_pages, list) and bool(allowed_pages) and all(isinstance(page, str) and page.strip() for page in allowed_pages) and len(allowed_pages) == len(set(allowed_pages)), "navigation pages must be a unique non-empty list of names")
    require(isinstance(aliases, dict) and bool(aliases), "navigation aliases must be a non-empty object")
    require(all(isinstance(alias, str) and alias.strip() and page in allowed_pages for alias, page in aliases.items()), "navigation aliases must resolve to registered pages")
    workspace_catalogue = application_policy.get("workspaces", {})
    require(bool(application_policy.get("workspace_catalogue_version")), "workspace knowledge catalogue must be versioned")
    require(bool(application_policy.get("academy_catalogue_version")), "Academy topic catalogue must be versioned")
    require(bool(application_policy.get("assistant_capability_catalogue_version")), "assistant capability catalogue must be versioned")
    require(application_policy.get("agent_turn_contract_version") == "1.1.0", "unsupported Agent turn contract version")
    require(registry.get("application_context", {}).get("version") == "1.3.0", "unsupported application context contract version")
    require(registry.get("application_context", {}).get("schema", {}).get("properties", {}).get("agent_turn_contract_version", {}).get("const") == application_policy.get("agent_turn_contract_version"), "UI/Agent turn contract version mismatch")
    require(registry.get("agent_turn_context_schema", {}).get("properties", {}).get("schema_version", {}).get("const") == application_policy.get("agent_turn_contract_version"), "Agent turn context schema/version mismatch")
    require(registry.get("agent_turn_response_schema", {}).get("properties", {}).get("schema_version", {}).get("const") == application_policy.get("agent_turn_contract_version"), "Agent turn response schema/version mismatch")
    require(isinstance(navigation_policy.get("explanation_cues", []), list), "workspace explanation cues must be a list")
    require(isinstance(navigation_policy.get("all_workspaces_phrases", []), list), "workspace overview phrases must be a list")
    require(application_policy.get("turn_outcome_contract_version") == "1.2.0", "unsupported turn outcome contract version")
    require(registry.get("turn_outcome_schema", {}).get("properties", {}).get("schema_version", {}).get("const") == application_policy.get("turn_outcome_contract_version"), "turn outcome schema/version mismatch")
    require(isinstance(workspace_catalogue, dict) and set(workspace_catalogue) == set(allowed_pages), "workspace knowledge catalogue must describe every registered page exactly once")
    for workspace_name, workspace in workspace_catalogue.items():
        require(bool(workspace.get("purpose")) and bool(workspace.get("value")), f"workspace {workspace_name} must declare purpose and customer value")
        require(isinstance(workspace.get("features"), list) and bool(workspace["features"]), f"workspace {workspace_name} must declare supported features")
        require(isinstance(workspace.get("limitations"), list), f"workspace {workspace_name} must declare limitations")
    academy_topics = application_policy.get("academy_topics", [])
    require(isinstance(academy_topics, list) and bool(academy_topics), "Academy topic catalogue must be a non-empty list")
    academy_topic_ids = [topic.get("topic_id") for topic in academy_topics]
    require(all(isinstance(topic_id, str) and topic_id.strip() for topic_id in academy_topic_ids), "Academy topics must have stable topic IDs")
    require(len(academy_topic_ids) == len(set(academy_topic_ids)), "Academy topic IDs must be unique")
    for topic in academy_topics:
        require(bool(topic.get("title")) and bool(topic.get("description")) and bool(topic.get("starter_prompt")), "Academy topic is missing title, description or starter prompt")
        require(isinstance(topic.get("learning_points"), list) and bool(topic["learning_points"]), "Academy topic must contain learning points")
        require(isinstance(topic.get("limitations"), list), "Academy topic limitations must be a list")
        require(isinstance(topic.get("keywords", []), list), "Academy topic keywords must be a list")
    assistant_capabilities = application_policy.get("assistant_capabilities", [])
    require(isinstance(assistant_capabilities, list) and bool(assistant_capabilities), "assistant capability catalogue must be a non-empty list")
    assistant_capability_ids = [item.get("capability_id") for item in assistant_capabilities if isinstance(item, dict)]
    require(len(assistant_capability_ids) == len(assistant_capabilities), "assistant capability catalogue contains an invalid entry")
    require(all(isinstance(item, str) and item.strip() for item in assistant_capability_ids), "assistant capability IDs must be stable non-empty strings")
    require(len(assistant_capability_ids) == len(set(assistant_capability_ids)), "assistant capability IDs must be unique")
    workspace_ids = set(workspace_catalogue)
    academy_topic_id_set = set(academy_topic_ids)
    for item in assistant_capabilities:
        require(bool(item.get("title")) and bool(item.get("description")) and bool(item.get("starter_prompt")), "assistant capability entry is missing customer-facing content")
        require(bool(item.get("icon")) and bool(item.get("category")), "assistant capability entry is missing icon/category")
        require(item.get("workspace") in workspace_ids, "assistant capability references an unknown workspace")
        require(isinstance(item.get("intent_ids", []), list) and all(value in intents for value in item.get("intent_ids", [])), "assistant capability references an unknown intent")
        require(isinstance(item.get("topic_ids", []), list) and all(value in academy_topic_id_set for value in item.get("topic_ids", [])), "assistant capability references an unknown Academy topic")
        require(isinstance(item.get("examples", []), list), "assistant capability examples must be a list")
        require(isinstance(item.get("sort_order"), int), "assistant capability sort order must be an integer")
    application_context_contract = registry.get("application_context", {})
    context_schema_properties = application_context_contract.get("schema", {}).get("properties", {})
    context_properties_for_preconditions = set(context_schema_properties)
    semantic_rules = application_context_contract.get("semantic_rules", [])
    require(isinstance(semantic_rules, list), "application context semantic_rules must be a list")
    semantic_rule_ids = set()
    for rule in semantic_rules:
        require(isinstance(rule, dict) and bool(rule.get("id")), "application context contains an invalid semantic rule")
        require(rule["id"] not in semantic_rule_ids, f"duplicate application context semantic rule {rule['id']}")
        semantic_rule_ids.add(rule["id"])
        evaluator = rule.get("evaluator")
        require(evaluator in {"unique_collection_property", "selected_record_matches"}, f"unsupported application context semantic evaluator: {evaluator}")
        collection = rule.get("collection")
        require(collection in context_schema_properties and context_schema_properties[collection].get("type") == "array", f"{rule['id']} references a non-array context collection")
        item_schema = _resolve_local_refs(context_schema_properties[collection].get("items", {}), registry)
        item_properties = set(item_schema.get("properties", {}))
        if evaluator == "unique_collection_property":
            require(rule.get("property") in item_properties, f"{rule['id']} references an unknown collection property")
        elif evaluator == "selected_record_matches":
            require(rule.get("selector") in context_schema_properties, f"{rule['id']} references an unknown selector field")
            require(rule.get("record_key") in item_properties, f"{rule['id']} references an unknown record key")
            mappings = rule.get("field_mappings", {})
            require(isinstance(mappings, dict) and bool(mappings), f"{rule['id']} requires field mappings")
            require(set(mappings) <= context_properties_for_preconditions and set(mappings.values()) <= item_properties, f"{rule['id']} contains invalid field mappings")
    for capability_id, contract in capabilities.items():
        if contract.get("kind") != "frontend":
            continue
        success_status = contract.get("result_semantics", {}).get("success_status")
        output_statuses = registry.get("$defs", {}).get("FrontendActionResult", {}).get("properties", {}).get("status", {}).get("enum", [])
        require(success_status in output_statuses and success_status != "rejected", f"{capability_id} has an invalid frontend success status")
        input_properties = set(contract.get("input_schema", {}).get("properties", {}))
        for precondition in contract.get("preconditions", []):
            evaluator = precondition.get("evaluator")
            require(evaluator in {"workspace_allowlist", "context_field_present", "context_collection_contains_argument"}, f"{capability_id} has an unsupported frontend precondition evaluator")
            if evaluator == "workspace_allowlist":
                require(precondition.get("argument") in input_properties, f"{capability_id} workspace precondition references an undeclared argument")
                require(bool(allowed_pages), f"{capability_id} workspace precondition has no registered pages")
            elif evaluator == "context_field_present":
                require(precondition.get("context_key") in context_properties_for_preconditions, f"{capability_id} context precondition references an undeclared context field")
            elif evaluator == "context_collection_contains_argument":
                argument = precondition.get("argument")
                context_key = precondition.get("context_key")
                require(argument in input_properties, f"{capability_id} collection precondition references an undeclared input argument")
                require(context_key in context_properties_for_preconditions, f"{capability_id} collection precondition references an undeclared context field")
                collection_schema = registry["application_context"]["schema"]["properties"][context_key]
                require(collection_schema.get("type") == "array", f"{capability_id} collection precondition context field is not an array")
                item_schema = _resolve_local_refs(collection_schema.get("items", {}), registry)
                require(precondition.get("item_key") in item_schema.get("properties", {}), f"{capability_id} collection precondition references an unknown item field")

    presentation_names: set[str] = set()
    for presentation_id, presentation in presentations.items():
        presentation_name = presentation.get("name")
        require(isinstance(presentation_name, str) and presentation_name.strip(), f"{presentation_id} is missing its exposed renderer name")
        require(presentation_name not in presentation_names, f"duplicate presentation renderer name {presentation_name!r}")
        presentation_names.add(presentation_name)
        binding = presentation.get("binding", {})
        source_capabilities = binding.get("source_capabilities", [])
        require(bool(source_capabilities), f"{presentation_id} must declare source capabilities")
        for capability_id in source_capabilities:
            require(capability_id in capabilities, f"{presentation_id} references unknown source capability {capability_id}")
            require(capabilities[capability_id].get("kind") == "mcp", f"{presentation_id} source {capability_id} is not a read capability")
        properties = presentation.get("props_schema", {}).get("properties", {})
        data_property = binding.get("data_property")
        require(isinstance(data_property, str) and data_property in properties, f"{presentation_id} data_property is not declared by its props schema")
        require("evidence" in properties, f"{presentation_id} must declare evidence in its props schema")
        transform_pipeline = binding.get("transform", [])
        require(isinstance(transform_pipeline, list), f"{presentation_id} transform must be a list of declarative operations")
        for operation in transform_pipeline:
            op = operation.get("op") if isinstance(operation, dict) else None
            require(op in {"wrap_object_as_array", "filter_array_by_context", "set_evidence_scope_from_context"}, f"{presentation_id} has unsupported transform operation {op}")
            if op == "filter_array_by_context":
                require(bool(operation.get("property")) and bool(operation.get("context_key")), f"{presentation_id} filter transform is missing property/context key")
                source_item_properties = set()
                for capability_id in source_capabilities:
                    source_schema = _resolve_local_refs(capabilities[capability_id]["output_schema"], registry)
                    if source_schema.get("type") == "array":
                        source_item_properties.update(source_schema.get("items", {}).get("properties", {}).keys())
                require(operation["property"] in source_item_properties, f"{presentation_id} filter property is not present in its source record schema")
                require(isinstance(operation.get("ignore_values", []), list), f"{presentation_id} filter ignore_values must be a list")
            if op == "set_evidence_scope_from_context":
                require(bool(operation.get("context_key")) and isinstance(operation.get("prefix", ""), str), f"{presentation_id} evidence-scope transform is incomplete")
        require(isinstance(presentation.get("follow_up"), bool), f"{presentation_id} must explicitly declare follow_up")
        require(bool(presentation.get("failure_message")), f"{presentation_id} is missing a safe failure message")
        allowed_context_keys = set()
        for rule in binding.get("preflight", []):
            capability_id = rule.get("capability")
            require(capability_id in capabilities, f"{presentation_id} preflight references unknown capability {capability_id}")
            require(capabilities[capability_id].get("kind") == "mcp", f"{presentation_id} preflight {capability_id} is not MCP")
            arguments = rule.get("arguments", {})
            preflight_schema = _resolve_local_refs(capabilities[capability_id]["input_schema"], registry)
            context_arguments = rule.get("arguments_from_context", {})
            require(isinstance(arguments, dict) and isinstance(context_arguments, dict), f"{presentation_id} preflight arguments must be objects")
            properties = preflight_schema.get("properties", {})
            for target, context_key in context_arguments.items():
                require(target in properties, f"{presentation_id} maps context into unknown input {target}")
                allowed_context_keys.add(context_key)
            allowed_static_schema = dict(preflight_schema)
            allowed_static_schema["required"] = [
                key for key in preflight_schema.get("required", []) if key not in context_arguments
            ]
            try:
                Draft202012Validator(allowed_static_schema, format_checker=_FORMAT_CHECKER).validate(arguments)
            except Exception as exc:
                raise RuntimeError(f"Invalid preflight arguments in {presentation_id}: {exc.message if hasattr(exc, 'message') else type(exc).__name__}") from exc
            for context_key in (rule.get("when_context_present"), rule.get("when_context_absent")):
                if context_key:
                    allowed_context_keys.add(context_key)
        title_context_key = binding.get("title_context_key")
        if title_context_key:
            allowed_context_keys.add(title_context_key)
        require(all(isinstance(key, str) and key for key in allowed_context_keys), f"{presentation_id} contains an invalid context mapping")

    for intent_id, intent in intents.items():
        require(bool(intent.get("version")), f"{intent_id} is missing a version")
        recognition = intent.get("recognition", {})
        mode = recognition.get("mode")
        require(mode in {"deterministic", "agent_assisted"}, f"{intent_id} has an unsupported recognition mode")
        semantic_discovery = intent.get("semantic_discovery", {})
        if semantic_discovery:
            require(isinstance(semantic_discovery, dict), f"{intent_id} semantic_discovery must be an object")
            require(isinstance(semantic_discovery.get("phrases", []), list), f"{intent_id} semantic phrases must be a list")
            require(isinstance(semantic_discovery.get("keywords", []), list), f"{intent_id} semantic keywords must be a list")
            require(all(isinstance(value, str) and value.strip() for value in semantic_discovery.get("phrases", []) + semantic_discovery.get("keywords", [])), f"{intent_id} semantic discovery entries must be non-empty strings")
            require(isinstance(semantic_discovery.get("auto_resolve", False), bool), f"{intent_id} auto_resolve must be boolean")
            if semantic_discovery.get("auto_resolve"):
                auto_capability_ids = [intent.get("execution_plan", {}).get("capability"), *intent.get("execution_plan", {}).get("supporting_capabilities", [])]
                auto_capability_ids = [value for value in auto_capability_ids if value]
                require(bool(auto_capability_ids), f"{intent_id} semantic auto-resolution requires a registered capability")
                for auto_capability_id in auto_capability_ids:
                    auto_capability = capabilities.get(auto_capability_id, {})
                    require(auto_capability.get("kind") == "mcp" and auto_capability.get("authorization", {}).get("agent_invocable") is True, f"{intent_id} semantic auto-resolution is limited to agent-invocable MCP reads")
                    require(auto_capability.get("execution", {}).get("effect") == "read_only", f"{intent_id} semantic auto-resolution cannot select a mutating capability")
        if mode == "deterministic":
            require(bool(recognition.get("patterns")), f"{intent_id} deterministic recognition requires patterns")
            for pattern in recognition.get("patterns", []) + recognition.get("exclusions", []):
                try:
                    re.compile(pattern, re.IGNORECASE)
                except (TypeError, re.error) as exc:
                    raise RuntimeError(f"Invalid intent pattern in {intent_id}: {pattern!r}") from exc
        plan = intent.get("execution_plan", {})
        capability_id = plan.get("capability")
        presentation_id = plan.get("presentation")
        if capability_id:
            require(capability_id in capabilities, f"{intent_id} references unknown capability {capability_id}")
        if presentation_id:
            require(presentation_id in presentations, f"{intent_id} references unknown presentation {presentation_id}")
        if capability_id and presentation_id:
            declared_sources = set(presentations[presentation_id].get("binding", {}).get("source_capabilities", []))
            declared_preflight = {rule.get("capability") for rule in presentations[presentation_id].get("binding", {}).get("preflight", [])}
            require(capability_id in declared_sources or capability_id in declared_preflight, f"{intent_id} capability is not a source/preflight dependency of its presentation")
        if mode == "deterministic":
            require(bool(presentation_id), f"{intent_id} deterministic intent must declare a canonical presentation")
        elif capability_id:
            capability = capabilities[capability_id]
            require(capability.get("authorization", {}).get("agent_invocable") is True, f"{intent_id} targets a capability unavailable to the agent")
            require(bool(plan.get("response_mode")), f"{intent_id} agent-assisted execution must declare a response mode")

        # Semantic inputs describe customer meaning, not executable tool
        # selection. Their argument mappings are runtime-owned compilation
        # rules and must target only properties in this intent's registered
        # primary capability schema.
        semantic_inputs = plan.get("semantic_inputs", {})
        require(isinstance(semantic_inputs, dict), f"{intent_id} semantic_inputs must be an object")
        for field_name, semantic_input in semantic_inputs.items():
            require(isinstance(field_name, str) and field_name.strip(), f"{intent_id} contains an invalid semantic input name")
            require(isinstance(semantic_input, dict), f"{intent_id}.{field_name} semantic input must be an object")
            require(semantic_input.get("location") in {"slots", "entities"}, f"{intent_id}.{field_name} has an unsupported semantic input location")
            allowed_values = semantic_input.get("allowed_values")
            require(isinstance(allowed_values, list) and bool(allowed_values), f"{intent_id}.{field_name} must declare allowed_values")
            require(all(isinstance(value, str) and value for value in allowed_values), f"{intent_id}.{field_name} allowed_values must be non-empty strings")
            require(len(set(allowed_values)) == len(allowed_values), f"{intent_id}.{field_name} allowed_values must be unique")
            default_value = semantic_input.get("default")
            if default_value is not None:
                require(default_value in allowed_values, f"{intent_id}.{field_name} default is not an allowed value")
                require(semantic_input.get("default_policy") == "when_unspecified", f"{intent_id}.{field_name} default requires an explicit when_unspecified policy")
            elif semantic_input.get("default_policy") is not None:
                require(semantic_input.get("default_policy") == "when_unspecified", f"{intent_id}.{field_name} has an unsupported default policy")
            require(isinstance(semantic_input.get("description"), str) and semantic_input.get("description", "").strip(), f"{intent_id}.{field_name} needs a semantic description")
            require(isinstance(semantic_input.get("required", False), bool), f"{intent_id}.{field_name} required must be a boolean")
            conditional_defaults = semantic_input.get("conditional_defaults", [])
            require(isinstance(conditional_defaults, list), f"{intent_id}.{field_name} conditional_defaults must be a list")
            for conditional in conditional_defaults:
                require(isinstance(conditional, dict), f"{intent_id}.{field_name} conditional default must be an object")
                when = conditional.get("when")
                value = conditional.get("value")
                require(isinstance(when, dict) and bool(when), f"{intent_id}.{field_name} conditional default needs a non-empty when object")
                require(value in allowed_values, f"{intent_id}.{field_name} conditional default value is not allowed")
                require(semantic_input.get("default_policy") == "when_unspecified", f"{intent_id}.{field_name} conditional defaults require when_unspecified policy")
                for dependency, expected in when.items():
                    require(dependency != field_name, f"{intent_id}.{field_name} conditional default cannot depend on itself")
                    dependency_spec = semantic_inputs.get(dependency)
                    require(isinstance(dependency_spec, dict), f"{intent_id}.{field_name} conditional default references unknown semantic input {dependency}")
                    require(expected in dependency_spec.get("allowed_values", []), f"{intent_id}.{field_name} conditional default references unsupported {dependency} value")
            # semantic_inputs is the single source of truth for rich fields.
            # Reject parallel legacy declarations rather than maintaining two
            # synchronized copies of the same field and its allowed values.
            if semantic_input["location"] == "slots":
                legacy_declarations = {
                    "required_slots": plan.get("required_slots", []),
                    "optional_slots": plan.get("optional_slots", []),
                    "slot_descriptions": plan.get("slot_descriptions", {}),
                    "slot_values": plan.get("slot_values", {}),
                }
            else:
                legacy_declarations = {
                    "required_entities": plan.get("required_entities", []),
                    "optional_entities": plan.get("optional_entities", []),
                    "entity_descriptions": plan.get("entity_descriptions", {}),
                }
            for legacy_name, legacy_fields in legacy_declarations.items():
                if isinstance(legacy_fields, dict):
                    duplicated = field_name in legacy_fields
                else:
                    duplicated = field_name in legacy_fields
                require(
                    not duplicated,
                    f"{intent_id}.{field_name} is declared in both semantic_inputs and {legacy_name}; keep semantic_inputs as the single source",
                )

            argument_mappings = semantic_input.get("argument_mappings", {})
            require(isinstance(argument_mappings, dict), f"{intent_id}.{field_name} argument_mappings must be an object")
            if argument_mappings:
                require(bool(capability_id), f"{intent_id}.{field_name} cannot compile arguments without a primary capability")
                capability_schema = _resolve_local_refs(capabilities[capability_id].get("input_schema", {}), registry)
                capability_properties = set(capability_schema.get("properties", {}))
                for semantic_value, mapped_arguments in argument_mappings.items():
                    require(semantic_value in allowed_values, f"{intent_id}.{field_name} maps an undeclared semantic value")
                    require(isinstance(mapped_arguments, dict), f"{intent_id}.{field_name} argument mapping must be an object")
                    require(set(mapped_arguments) <= capability_properties, f"{intent_id}.{field_name} maps to undeclared capability arguments")
                    compiled_arguments = dict(plan.get("arguments", {}))
                    compiled_arguments.update(mapped_arguments)
                    try:
                        Draft202012Validator(capability_schema, format_checker=_FORMAT_CHECKER).validate(compiled_arguments)
                    except Exception as exc:
                        raise RuntimeError(f"Invalid semantic argument mapping in {intent_id}.{field_name}: {exc.message if hasattr(exc, 'message') else type(exc).__name__}") from exc
        supporting_capabilities = plan.get("supporting_capabilities", [])
        require(isinstance(supporting_capabilities, list), f"{intent_id} supporting_capabilities must be a list")
        for supporting_capability_id in supporting_capabilities:
            supporting_capability = capabilities.get(supporting_capability_id)
            require(supporting_capability is not None, f"{intent_id} references unknown supporting capability {supporting_capability_id}")
            require(supporting_capability.get("kind") == "mcp", f"{intent_id} supporting capability {supporting_capability_id} must be MCP")
            require(supporting_capability.get("authorization", {}).get("agent_invocable") is True, f"{intent_id} supporting capability {supporting_capability_id} is unavailable to the agent")

    for operation_id, operation in kernel_operations.items():
        require(bool(operation.get("version")), f"{operation_id} is missing a version")
        require(operation.get("agent_callable") is False, f"{operation_id} must remain unavailable to the agent")
        require(operation.get("authority") == "banking_kernel", f"{operation_id} must be Banking Kernel-authoritative")
        require(bool(operation.get("required_capability")) and bool(operation.get("route")), f"{operation_id} is missing route/capability metadata")
        execution = operation.get("execution", {})
        require(bool(execution.get("effect")) and isinstance(execution.get("idempotent"), bool), f"{operation_id} is missing execution semantics")
        retry = execution.get("retry", {})
        require(retry.get("strategy") in {"none", "bounded_exponential", "caller_controlled_idempotency_key"}, f"{operation_id} has an unsupported retry strategy")
        require(isinstance(retry.get("max_attempts"), int) and retry["max_attempts"] >= 1, f"{operation_id} has invalid retry attempts")
        if retry.get("strategy") == "caller_controlled_idempotency_key":
            require(execution.get("idempotent") is True and retry["max_attempts"] == 1, f"{operation_id} caller-controlled retry requires Kernel idempotency and no automatic retry")
        if retry.get("strategy") == "none":
            require(retry["max_attempts"] == 1, f"{operation_id} cannot declare multiple attempts with retry disabled")

    context_contract = registry.get("application_context", {})
    require(context_contract.get("version") in {"1.0.0", "1.1.0", "1.2.0", "1.3.0"}, "application context contract version is missing or unsupported")
    require(context_contract.get("authority") == "frontend_read_only", "application context must remain frontend read-only")
    require(context_contract.get("may_authorize_banking_operations") is False, "application context must never authorize banking operations")
    context_properties = set(context_contract.get("schema", {}).get("properties", {}))
    for presentation_id, presentation in presentations.items():
        binding = presentation.get("binding", {})
        context_keys = set()
        for transform in binding.get("transform", []):
            if transform.get("context_key"):
                context_keys.add(transform["context_key"])
        for rule in binding.get("preflight", []):
            context_keys.update(value for value in rule.get("arguments_from_context", {}).values())
            context_keys.update(value for value in (rule.get("when_context_present"), rule.get("when_context_absent")) if value)
        if binding.get("title_context_key"):
            context_keys.add(binding["title_context_key"])
        require(context_keys <= context_properties, f"{presentation_id} references undeclared application context fields: {sorted(context_keys - context_properties)}")

    lifecycle_outcomes = set(registry.get("execution_lifecycle", {}).get("terminal_outcomes", []))
    envelope_outcomes = set(registry.get("result_envelope_schema", {}).get("properties", {}).get("outcome", {}).get("enum", []))
    semantic_outcomes = set(registry.get("execution_lifecycle", {}).get("outcome_semantics", {}))
    require(bool(lifecycle_outcomes) and lifecycle_outcomes == envelope_outcomes == semantic_outcomes, "lifecycle, outcome semantics and result envelope outcomes differ")


def _get(group: str, contract_id: str) -> dict[str, Any]:
    try:
        return get_registry()[group][contract_id]
    except KeyError as exc:
        raise KeyError(f"Unknown ORAIflow {group[:-1]} contract: {contract_id}") from exc


def get_capability(contract_id: str) -> dict[str, Any]:
    """Return a capability contract by its registered identifier."""
    return _get("capabilities", contract_id)


def get_presentation(presentation_id: str) -> dict[str, Any]:
    """Return a presentation contract by its registered identifier."""
    return _get("presentations", presentation_id)


def get_kernel_operation(operation_id: str) -> dict[str, Any]:
    """Return a Banking Kernel operation contract by identifier."""
    return _get("kernel_operations", operation_id)


def capability_by_name(name: str, kind: str | None = None) -> tuple[str, dict[str, Any]] | None:
    """Resolve a capability by name and optional execution kind."""
    for contract_id, contract in get_registry()["capabilities"].items():
        if contract.get("name") == name and (kind is None or contract.get("kind") == kind):
            return contract_id, contract
    return None


def intent_by_id(intent_id: str) -> dict[str, Any]:
    """Return an intent contract by its registered identifier."""
    return _get("intents", intent_id)


def validate_schema(contract_id: str, section: str, schema: Mapping[str, Any], payload: Any) -> Any:
    """Validate a payload against the selected contract schema."""
    resolved_schema = _resolve_local_refs(schema, get_registry())
    validator = Draft202012Validator(resolved_schema, format_checker=_FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(payload), key=lambda err: (list(err.absolute_path), err.message))
    if errors:
        first = errors[0]
        path = ".".join(str(part) for part in first.absolute_path) or "$"
        message = f"{path}: {first.message}"
        raise ContractViolation(contract_id, section, message)
    return payload


def pydantic_model_for_schema(schema: Mapping[str, Any], model_name: str):
    """Build a strict Pydantic v2 model from a registry JSON Schema declaration.

    This is used for model-facing LangChain tool argument schemas so the LLM
    sees the same constraints that MCP and the runtime validate. The supported
    subset deliberately covers the current capability schemas and fails closed
    on unknown schema constructs rather than weakening validation.
    """
    from datetime import datetime
    from typing import Annotated, Literal, Union
    from pydantic import ConfigDict, Field, create_model

    root = get_registry()
    resolved = _resolve_local_refs(dict(schema), root)

    def annotation_for(definition: Mapping[str, Any], nested_name: str):
        if "const" in definition:
            return Literal[definition["const"]]
        if isinstance(definition.get("enum"), list):
            values = tuple(definition["enum"])
            if not values:
                raise RuntimeError(f"Empty enum in capability schema for {nested_name}")
            return Literal[values]
        alternatives = definition.get("oneOf") or definition.get("anyOf")
        if alternatives:
            return Union[tuple(annotation_for(item, f"{nested_name}Variant{index}") for index, item in enumerate(alternatives))]
        schema_type = definition.get("type")
        if isinstance(schema_type, list):
            return Union[tuple(annotation_for({**definition, "type": item}, nested_name) for item in schema_type)]
        if schema_type == "object":
            properties = definition.get("properties", {})
            required = set(definition.get("required", []))
            fields = {}
            for field_name, field_schema in properties.items():
                field_type = annotation_for(field_schema, f"{nested_name}_{field_name}")
                constraints = {}
                for source, target in (("minLength", "min_length"), ("maxLength", "max_length"), ("pattern", "pattern"), ("minimum", "ge"), ("maximum", "le"), ("minItems", "min_length"), ("maxItems", "max_length")):
                    if source in field_schema:
                        constraints[target] = field_schema[source]
                default = ... if field_name in required else field_schema.get("default", None)
                fields[field_name] = (field_type, Field(default=default, description=field_schema.get("description"), **constraints))
            extra = "forbid" if definition.get("additionalProperties", False) is False else "allow"
            return create_model(nested_name, __config__=ConfigDict(extra=extra, strict=True), **fields)
        if schema_type == "array":
            item_type = annotation_for(definition.get("items", {}), f"{nested_name}Item")
            constraints = {}
            if "minItems" in definition:
                constraints["min_length"] = definition["minItems"]
            if "maxItems" in definition:
                constraints["max_length"] = definition["maxItems"]
            array_type = list[item_type]
            return Annotated[array_type, Field(**constraints)] if constraints else array_type
        scalar_types = {"string": str, "integer": int, "number": float, "boolean": bool, "null": type(None)}
        if schema_type in scalar_types:
            if schema_type == "string" and definition.get("format") == "date-time":
                return datetime
            return scalar_types[schema_type]
        raise RuntimeError(f"Unsupported JSON Schema construct in capability model {nested_name}: {definition}")

    if resolved.get("type") != "object":
        raise RuntimeError("Capability tool input schemas must be JSON Schema objects")
    return annotation_for(resolved, model_name)


def validate_application_context(payload: Any) -> Any:
    """Validate read-only browser context before it enters the agent graph.

    Cross-field checks ensure a selected account is represented in the supplied
    account catalogue and that its display name matches that catalogue. This is
    still presentation context only: MCP and the Banking Kernel independently
    establish identity, ownership and authorization.
    """
    contract = get_registry()["application_context"]
    validate_schema("application_context", "context", contract["schema"], payload)
    for rule in contract.get("semantic_rules", []):
        evaluator = rule["evaluator"]
        if evaluator == "unique_collection_property":
            values = [item[rule["property"]] for item in payload[rule["collection"]]]
            if len(values) != len(set(values)):
                raise ContractViolation("application_context", "context", f"semantic rule failed: {rule['id']}")
        elif evaluator == "selected_record_matches":
            selected_value = payload[rule["selector"]]
            if selected_value is None:
                continue
            selected = next((item for item in payload[rule["collection"]] if item[rule["record_key"]] == selected_value), None)
            if selected is None or any(payload[context_field] != selected[item_field] for context_field, item_field in rule["field_mappings"].items()):
                raise ContractViolation("application_context", "context", f"semantic rule failed: {rule['id']}")
        else:  # The registry validator should make this unreachable; fail closed if cached state is corrupted.
            raise ContractViolation("application_context", "context", "unsupported semantic rule")
    return payload


def validate_capability_input(contract_id: str, payload: Any) -> Any:
    """Validate input data for a registered capability."""
    contract = get_capability(contract_id)
    return validate_schema(contract_id, "input", contract["input_schema"], payload)


def validate_capability_output(contract_id: str, payload: Any) -> Any:
    """Validate output data for a registered capability."""
    contract = get_capability(contract_id)
    return validate_schema(contract_id, "output", contract["output_schema"], payload)


def validate_presentation_props(presentation_id: str, payload: Any) -> Any:
    """Validate properties supplied to a registered presentation."""
    contract = get_presentation(presentation_id)
    return validate_schema(presentation_id, "presentation props", contract["props_schema"], payload)


def validate_kernel_input(operation_id: str, payload: Any) -> Any:
    """Validate input for a registered Banking Kernel operation."""
    contract = get_kernel_operation(operation_id)
    return validate_schema(operation_id, "input", contract["input_schema"], payload)


def validate_kernel_output(operation_id: str, payload: Any) -> Any:
    """Validate output from a registered Banking Kernel operation."""
    contract = get_kernel_operation(operation_id)
    return validate_schema(operation_id, "output", contract["output_schema"], payload)


def create_result_envelope(
    capability_id: str,
    outcome: str,
    *,
    data: Any = None,
    evidence: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None,
    trace_id: str = "-",
) -> dict[str, Any]:
    """Create a common, self-validating result envelope for any capability."""
    contract = get_capability(capability_id)
    allowed = set(get_registry()["execution_lifecycle"]["terminal_outcomes"])
    if outcome not in allowed:
        raise ValueError(f"Unsupported capability outcome: {outcome}")
    if outcome in {"SUCCESS", "EMPTY", "PARTIAL"}:
        validate_capability_output(capability_id, data)
        base_evidence = contract.get("evidence", {})
        resolved_evidence = {
            "source": base_evidence.get("source", contract.get("kind", "unknown")),
            "scope": base_evidence.get("scope", "declared_capability_scope"),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "completeness": "EMPTY" if outcome == "EMPTY" else "PARTIAL" if outcome == "PARTIAL" else base_evidence.get("completeness", "COMPLETE"),
            "trace_id": trace_id,
        }
        if evidence:
            resolved_evidence.update(dict(evidence))
        resolved_error = None
    else:
        resolved_evidence = None
        resolved_error = {
            "code": str((error or {}).get("code", outcome)),
            "retryable": bool((error or {}).get("retryable", outcome == "UNAVAILABLE")),
        }
        if error and error.get("message"):
            resolved_error["message"] = str(error["message"])[:300]
    envelope = {
        "schema_version": "1.0.0",
        "capability": capability_id,
        "outcome": outcome,
        "data": data if outcome in {"SUCCESS", "EMPTY", "PARTIAL"} else None,
        "evidence": resolved_evidence,
        "error": resolved_error,
        "trace_id": trace_id,
        # Tool output is data, never an instruction source. This label is not
        # an authorization decision and does not replace source validation.
        "trust_class": "untrusted_banking_data" if outcome in {"SUCCESS", "EMPTY", "PARTIAL"} else "connector_error",
    }
    return validate_result_envelope(capability_id, envelope)


def validate_result_envelope(capability_id: str, envelope: Any) -> dict[str, Any]:
    """Validate envelope shape, payload, evidence provenance and outcome invariants."""
    if not isinstance(envelope, dict):
        raise ContractViolation(capability_id, "result envelope", "expected object")
    if envelope.get("schema_version") != "1.0.0" or envelope.get("capability") != capability_id:
        raise ContractViolation(capability_id, "result envelope", "version or capability mismatch")
    validate_schema(capability_id, "result envelope", get_registry()["result_envelope_schema"], envelope)
    outcome = envelope.get("outcome")
    if outcome in {"SUCCESS", "EMPTY", "PARTIAL"}:
        validate_capability_output(capability_id, envelope.get("data"))
        if envelope.get("trust_class") != "untrusted_banking_data":
            raise ContractViolation(capability_id, "result envelope", "successful data must retain its untrusted-data classification")
        if envelope.get("error") is not None:
            raise ContractViolation(capability_id, "result envelope", "successful outcomes cannot contain an error")
        evidence = envelope.get("evidence")
        declared = get_capability(capability_id).get("evidence", {})
        expected_source = declared.get("source", get_capability(capability_id).get("kind", "unknown"))
        expected_scope = declared.get("scope", "declared_capability_scope")
        if evidence.get("source") != expected_source or evidence.get("scope") != expected_scope:
            raise ContractViolation(capability_id, "evidence", "source or scope does not match the capability declaration")
        if evidence.get("trace_id") != envelope.get("trace_id"):
            raise ContractViolation(capability_id, "evidence", "trace identifier mismatch")
        data = envelope.get("data")
        is_empty_data = isinstance(data, (list, dict)) and len(data) == 0
        if outcome == "EMPTY":
            if not is_empty_data or evidence.get("completeness") != "EMPTY":
                raise ContractViolation(capability_id, "result envelope", "EMPTY requires empty data and EMPTY evidence completeness")
        elif outcome == "PARTIAL":
            if evidence.get("completeness") != "PARTIAL":
                raise ContractViolation(capability_id, "result envelope", "PARTIAL requires PARTIAL evidence completeness")
            if is_empty_data:
                raise ContractViolation(capability_id, "result envelope", "PARTIAL cannot contain an empty data collection")
        else:
            if is_empty_data or evidence.get("completeness") in {"EMPTY", "PARTIAL"}:
                raise ContractViolation(capability_id, "result envelope", "SUCCESS requires non-empty data and non-partial completeness")
    elif outcome in {"INVALID_INPUT", "REJECTED", "UNAVAILABLE", "INVALID_RESPONSE"}:
        if envelope.get("trust_class") != "connector_error":
            raise ContractViolation(capability_id, "result envelope", "failed outcomes must be classified as connector errors")
        if envelope.get("data") is not None or envelope.get("evidence") is not None:
            raise ContractViolation(capability_id, "result envelope", "failed outcomes cannot contain data or success evidence")
        if not isinstance(envelope.get("error"), dict) or not envelope["error"].get("code"):
            raise ContractViolation(capability_id, "result envelope", "failed outcomes require a normalized error")
    else:
        raise ContractViolation(capability_id, "result envelope", "unknown outcome")
    return envelope


def parse_result_envelope(capability_id: str, raw: str | bytes) -> dict[str, Any]:
    """Parse and validate a capability result envelope without trusting unvalidated payload fields."""
    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ContractViolation(capability_id, "result envelope", "response is not valid JSON") from exc
    return validate_result_envelope(capability_id, envelope)
