"""Compile the human routing policy and OCCF into one Agent decision map.

The JSON policy owns route order and handling strategy. OCCF remains the only
source of truth for intent/capability schemas, resources and authority limits.
"""

from __future__ import annotations

from typing import Any

from shared.contracts.decision_routing import (
    DecisionRoutingPolicy,
    load_decision_routing_policy,
    ordered_pre_assessment_routes,
    task_dispatch_for_class,
)
from shared.contracts.registry import get_registry


def _intent_capability_ids(intent: dict[str, Any]) -> list[str]:
    plan = intent.get("execution_plan", {}) if isinstance(intent, dict) else {}
    capability_ids = [plan.get("contact_resolution"), plan.get("capability")]
    capability_ids.extend(plan.get("supporting_capabilities", []) or [])
    presentation_id = plan.get("presentation")
    return list(dict.fromkeys(str(value) for value in capability_ids if value))


def classify_intent_task_class(
    intent_id: str,
    registry: dict[str, Any] | None = None,
) -> str:
    """Derive the task class from its registered execution/resource contract."""
    registry = registry or get_registry()
    intent = registry.get("intents", {}).get(intent_id)
    if not isinstance(intent, dict):
        return "unsupported"
    plan = intent.get("execution_plan", {})
    response_mode = str(plan.get("response_mode", ""))
    if plan.get("resource_id") or response_mode in {"registered_catalogue", "registered_resource"}:
        return "resource"
    if response_mode in {"editable_transfer_draft", "registered_workflow"} or plan.get(
        "workflow_id"
    ):
        return "workflow"
    if plan.get("presentation"):
        return "presentation"

    capability_ids = _intent_capability_ids(intent)
    capabilities = registry.get("capabilities", {})
    declared = [capabilities.get(value, {}) for value in capability_ids]
    if any(item.get("kind") == "frontend" for item in declared):
        return "frontend_action"
    if any(item.get("kind") == "mcp" for item in declared):
        return "mcp_read"
    return "unsupported"


def _capability_catalogue(registry: dict[str, Any]) -> list[dict[str, Any]]:
    """Project safe descriptive capability metadata directly from OCCF."""
    uses: dict[str, list[str]] = {}
    for intent_id, intent in registry.get("intents", {}).items():
        for capability_id in _intent_capability_ids(intent):
            uses.setdefault(capability_id, []).append(str(intent_id))

    result: list[dict[str, Any]] = []
    for capability_id, capability in registry.get("capabilities", {}).items():
        authorization = capability.get("authorization", {})
        if authorization.get("agent_invocable") is not True:
            continue
        schema = capability.get("input_schema", {})
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        execution = capability.get("execution", {})
        effect = str(execution.get("effect", ""))
        kind = str(capability.get("kind", ""))
        if kind == "mcp" and effect not in {"read", "read_only"}:
            # No writable MCP capability is ever advertised to the interpreter.
            continue
        result.append(
            {
                "capability_id": str(capability_id),
                "name": str(capability.get("name", "")),
                "kind": kind,
                "purpose": str(capability.get("description", ""))[:240],
                "effect": effect,
                "required_inputs": (
                    list(schema.get("required", [])) if isinstance(schema, dict) else []
                ),
                "available_inputs": {
                    str(key): str(value.get("description") or value.get("title") or key)[:100]
                    for key, value in properties.items()
                    if isinstance(value, dict)
                },
                "serves_intents": sorted(uses.get(str(capability_id), [])),
                "execution_boundary": (
                    "Authenticated read; runtime maps intent and validates arguments; MCP checks customer scope."
                    if kind == "mcp"
                    else "Registered UI-state action; runtime validates context and browser receipt."
                ),
            }
        )
    return result


def build_agent_decision_map(
    registry: dict[str, Any] | None = None,
    policy: DecisionRoutingPolicy | None = None,
) -> dict[str, Any]:
    """Build the prompt-ready map by joining policy routes to canonical OCCF.

    The editable policy never repeats capability IDs or input schemas. Those
    are projected from the current OCCF registry at runtime.
    """
    registry = registry or get_registry()
    policy = policy or load_decision_routing_policy()
    application = registry.get("application_policy", {})

    from services.agent_runtime.task_interpretation import task_prompt_catalogue

    # Keep the prompt map deliberately compact. The LLM needs to understand the
    # application decision surface, not receive every UI description, phrase
    # example or JSON Schema. OCCF remains available to the runtime for exact
    # validation and execution; full resource content is retrieved only when
    # the selected resource task is served.
    intents = []
    for item in task_prompt_catalogue(registry):
        intent_id = item["intent_id"]
        task_class = classify_intent_task_class(intent_id, registry)
        dispatch = task_dispatch_for_class(task_class, policy)
        semantic_inputs = item.get("semantic_inputs") or {}
        semantic_fields = set(semantic_inputs)
        # Avoid repeating semantic input requirements in legacy slot lists.
        # The model sees meaning/allowed values/defaults, but never backend
        # argument_mappings or executable capability schemas.
        projected_intent = {
            "intent_id": intent_id,
            "purpose": item["description"][:120],
            "task_class": task_class,
            "execution_owner": dispatch.handler if dispatch else "explain_supported_scope",
            "resource_id": item.get("resource_id"),
            "required_slots": [
                value for value in item["required_slots"] if value not in semantic_fields
            ],
            "optional_slots": [
                value for value in item["optional_slots"] if value not in semantic_fields
            ],
            "slot_descriptions": {
                key: value
                for key, value in item["slot_descriptions"].items()
                if key not in semantic_fields
            },
            "slot_values": {
                key: value
                for key, value in item["slot_values"].items()
                if key not in semantic_fields
            },
            "required_entities": item["required_entities"],
            "response_mode": item["response_mode"],
            "requires_unique_contact": item["requires_unique_contact"],
        }
        if semantic_inputs:
            projected_intent["semantic_inputs"] = semantic_inputs
        if item.get("entity_descriptions"):
            projected_intent["entity_descriptions"] = item["entity_descriptions"]
        if item.get("outcome_semantics"):
            projected_intent["outcome_semantics"] = item["outcome_semantics"][:420]
        intents.append(projected_intent)

    capability_catalogue = []
    for item in _capability_catalogue(registry):
        capability_catalogue.append(
            {
                "capability_id": item["capability_id"],
                "kind": item["kind"],
                "effect": item["effect"],
                "required_inputs": item["required_inputs"],
                "serves_intents": item["serves_intents"],
            }
        )

    workspaces = application.get("workspaces", {})
    workspace_items = [
        {
            "workspace_id": str(workspace_id),
            "purpose": str(value.get("purpose", ""))[:105],
        }
        for workspace_id, value in workspaces.items()
        if isinstance(value, dict)
    ]
    academy_topics = []
    for topic in application.get("academy_topics", []):
        if not isinstance(topic, dict):
            continue
        academy_topics.append(
            {
                "topic_id": str(topic.get("topic_id", "")),
                "title": str(topic.get("title", "")),
                "purpose": str(topic.get("description", ""))[:105],
                "personal_data_intent_id": topic.get("personal_data_intent_id"),
            }
        )

    assistant_capabilities = [
        {
            "capability_id": str(item.get("capability_id", "")),
            "title": str(item.get("title", "")),
            "workspace": str(item.get("workspace", "")),
            "intent_ids": list(item.get("intent_ids", [])),
            "topic_ids": list(item.get("topic_ids", [])),
        }
        for item in application.get("assistant_capabilities", [])
        if isinstance(item, dict)
    ]

    # Fail fast on policy/registry drift instead of silently advertising a
    # route that has no registered owner or references a missing resource.
    resource_ids = {"academy_topics", "workspace_catalogue", "assistant_capabilities"}
    for route in policy.pre_assessment_routes:
        if route.handler not in {
            "academy_personal_read",
            "workspace_navigation",
            "workspace_knowledge",
            "academy_education",
            "connector_failure",
            "canonical_read",
            "assistant_capabilities",
            "simple_social",
        }:
            raise ValueError(f"Decision policy route has no runtime owner: {route.handler}")
    for intent in intents:
        if intent["task_class"] == "resource" and intent["resource_id"] not in resource_ids:
            raise ValueError(f"Intent {intent['intent_id']} references an unsupported resource")
        if not intent["execution_owner"]:
            raise ValueError(f"Intent {intent['intent_id']} has no task dispatch owner")

    return {
        "policy": {
            "schema_version": policy.schema_version,
            "registry_version": str(registry.get("registry_version", "unknown")),
            "policy_id": policy.policy_id,
            "revision": policy.revision,
            "pre_assessment_routes": [
                {
                    "id": route.id,
                    "priority": route.priority,
                    "handler": route.handler,
                    "purpose": route.purpose[:110],
                }
                for route in ordered_pre_assessment_routes(policy)
            ],
            "task_dispatch": {
                route.task_class: {
                    "handler": route.handler,
                    "applicable_confidence_dimensions": route.applicable_confidence_dimensions,
                    "low_confidence_behavior": route.low_confidence_behavior,
                }
                for route in policy.task_dispatch
            },
            "assessment_calls_per_new_turn": policy.interpreter.assessment_calls_per_new_turn,
            "format_recovery_calls": policy.interpreter.format_recovery_calls,
            "confidence_mode_source": policy.confidence.configuration_source,
        },
        "entry_sequence": [
            "Restore pending workflow/task state and reconcile receipts first.",
            "Run enabled deterministic/resource fast routes in configured priority order.",
            "If unresolved, perform one semantic assessment using this map.",
            "Runtime validates the intent, derives the OCCF capability and executes the registered task class.",
            "Workflow stages continue deterministically; reassess only when a new customer message changes the task.",
        ],
        "intents": intents,
        "capabilities": capability_catalogue,
        "resources": {
            "workspaces": workspace_items,
            "academy_topics": academy_topics,
            "assistant_capabilities": assistant_capabilities,
            "navigation_pages": list(application.get("navigation", {}).get("pages", [])),
        },
        "workflow_rule": "Registered workflows own deterministic stages; reassess only when a new message changes or clarifies the task.",
        "authority_rules": [
            "Select registered intent IDs only; never return capability IDs or tool names.",
            "The runtime maps validated intent to the OCCF capability and arguments.",
            "Resource tasks may use the registered catalogue without MCP.",
            "Personalized explanations require their declared authenticated reads.",
            "Registered workflows own deterministic stages; do not re-select tools.",
            "An empty candidate shortlist is not proof of unsupported intent; use the full semantic map.",
            "Preserve explicit fields, entities, context references and supported sibling tasks.",
            "Confidence informs interpretation only; it never authorizes actions.",
        ],
    }


def task_class_for_intent(intent_id: str, registry: dict[str, Any] | None = None) -> str:
    """Resolve the registered task class for an intent identifier."""
    return classify_intent_task_class(intent_id, registry)


def pre_assessment_routes(policy: DecisionRoutingPolicy | None = None):
    """Return the ordered pre-assessment routes declared by the routing policy."""
    return ordered_pre_assessment_routes(policy)


def decision_policy_identity(policy: DecisionRoutingPolicy | None = None) -> dict[str, Any]:
    """Return non-sensitive policy identity for structured turn observability."""
    policy = policy or load_decision_routing_policy()
    return {"policy_id": policy.policy_id, "revision": policy.revision}


def render_registered_resource_response(
    resource_id: str,
    references: dict[str, Any],
    current_text: str,
) -> str | None:
    """Render a validated catalogue resource task without an MCP/model loop."""
    references = references if isinstance(references, dict) else {}
    if resource_id == "academy_topics":
        from services.agent_runtime.academy import academy_catalogue_explanation

        topic_ids = references.get("topic_ids")
        if not isinstance(topic_ids, list):
            topic_id = references.get("topic_id")
            topic_ids = [topic_id] if topic_id else None
        result = academy_catalogue_explanation(
            current_text,
            topic_ids=topic_ids,
            require_customer_explanation=False,
        )
        if result:
            return result
        # A broad Academy question is still answerable from the registered
        # catalogue; do not turn an unmatched keyword into an out-of-scope task.
        registry = get_registry()
        topics = registry.get("application_policy", {}).get("academy_topics", [])
        titles = [
            str(item.get("title", ""))
            for item in topics
            if isinstance(item, dict) and item.get("title")
        ]
        if titles:
            return (
                "**Academy**\nA guided financial-literacy workspace where you can learn about "
                "everyday money decisions and ask ORAIflow to explain concepts in plain language.\n\n"
                "**Learning paths**\n" + "\n".join(f"• {item}" for item in titles)
            )
        return None

    if resource_id == "workspace_catalogue":
        from services.agent_runtime.workspace_knowledge import (
            workspace_catalogue_overview_message,
            workspace_explanation_message,
        )

        workspace = references.get("workspace")
        return (
            workspace_explanation_message(str(workspace))
            if workspace
            else workspace_catalogue_overview_message()
        )

    if resource_id == "assistant_capabilities":
        from services.agent_runtime.assistant_capabilities import (
            assistant_capability_catalogue_message,
        )

        return assistant_capability_catalogue_message()
    return None
