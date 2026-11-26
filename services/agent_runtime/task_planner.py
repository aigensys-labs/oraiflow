"""Deterministic interpretation-to-OCCF planner.

This module owns the mapping from an interpreted, registered intent to the
canonical capability declared by OCCF. The LLM never supplies capability IDs,
tool names, input schemas, execution effects or authorization decisions.
"""

from __future__ import annotations

import re
import uuid
from typing import Any


from services.agent_runtime.interpretation import (
    validate_interpretation_assessment,
)
from services.agent_runtime.task_interpretation import (
    InterpretedTask,
    TaskEvidence,
    intent_validation_capability,
    normalise_task_payload,
)
from shared.contracts.decision_routing import load_decision_routing_policy, task_dispatch_for_class
from services.agent_runtime.decision_router import classify_intent_task_class
from shared.contracts.registry import (
    ContractViolation,
    get_registry,
    validate_capability_input,
)


def _normalise(value: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def _strip_registered_grouping_suffix(
    candidate: str,
    execution_plan: dict[str, Any],
) -> str:
    """Remove a recognized trailing grouping phrase from a merchant filter.

    Customers often place the requested breakdown after the merchant name
    (for example, "coffee by months"). The grouping vocabulary is owned by
    OCCF; this keeps that presentation instruction out of the literal
    transaction-description filter without maintaining a second phrase list.
    """
    definitions = execution_plan.get("semantic_inputs", {})
    group_spec = definitions.get("group_by", {}) if isinstance(definitions, dict) else {}
    phrase_map = group_spec.get("evidence_phrases", {}) if isinstance(group_spec, dict) else {}
    month_phrases = phrase_map.get("month", []) if isinstance(phrase_map, dict) else []
    if not isinstance(month_phrases, list):
        return candidate
    for phrase in sorted(
        (str(item).strip() for item in month_phrases if str(item).strip()), key=len, reverse=True
    ):
        words = re.split(r"\s+", phrase)
        suffix = r"\s+" + r"\s+".join(re.escape(word) for word in words) + r"\s*$"
        match = re.search(suffix, candidate, flags=re.IGNORECASE)
        if match:
            cleaned = candidate[: match.start()].strip(" \t\r\n.,;:!?\"'")
            if cleaned:
                return cleaned
    return candidate


def _normalise_account_reference(value: Any) -> str:
    """Remove generic account wording while preserving the named account.

    This is applied only when matching a model-proposed source-account value
    against the validated UI account catalogue; it cannot select an account
    that is not present there.
    """
    generic = {"account", "accounts", "my", "the", "from", "using", "source"}
    return " ".join(token for token in _normalise(value).split() if token not in generic)


def _evidence_payload(task: InterpretedTask) -> list[str]:
    return [f"{item.field}|{item.source}|{item.quote}" for item in task.evidence]


def _assessment_for_capability(task: InterpretedTask, capability_id: str) -> dict[str, Any]:
    payload = normalise_task_payload(task)
    payload["capability_id"] = capability_id
    # Preserve the model's provisional resolution through evidence validation.
    # The planner opts into validating it provisionally and only emits READY
    # after semantic/default resolution and the final OCCF schema check succeed.
    return payload


def _semantic_value_from_customer_text(spec: dict[str, Any], customer_text: str) -> str | None:
    """Resolve one unambiguous semantic value from phrases registered in OCCF.

    Longer registered phrases suppress their contained shorter phrases (for
    example, "income versus expenses" must remain "all", not "inflows").
    Conflicting semantic categories are deliberately left unresolved here.
    """
    phrase_map = spec.get("evidence_phrases", {})
    if not isinstance(phrase_map, dict) or not customer_text:
        return None
    normalized_text = f" {_normalise(customer_text)} "
    matches: list[tuple[int, str, str]] = []
    for value, phrases in phrase_map.items():
        if not isinstance(value, str) or not isinstance(phrases, list):
            continue
        for phrase in phrases:
            normalized_phrase = _normalise(phrase)
            if normalized_phrase and f" {normalized_phrase} " in normalized_text:
                matches.append((len(normalized_phrase), normalized_phrase, value))
    if not matches:
        return None
    specific = [
        item
        for item in matches
        if not any(other[0] > item[0] and f" {item[1]} " in f" {other[1]} " for other in matches)
    ]
    values = {item[2] for item in specific}
    return next(iter(values)) if len(values) == 1 else None


def _resolve_semantic_inputs(
    task: InterpretedTask,
    execution_plan: dict[str, Any],
    *,
    customer_text: str = "",
) -> tuple[dict[str, str], list[str], list[str]]:
    """Resolve OCCF semantic values, customer wording and safe defaults.

    Explicit phrase evidence fills omitted or unevidenced fallback values. A
    field-level model proposal with contradictory evidence is rejected by the
    interpretation gate rather than silently rewritten. Only contract-declared
    values, phrases, defaults and mappings are consumed.
    """
    effective: dict[str, str] = {}
    missing: list[str] = []
    unsupported: list[str] = []
    definitions = execution_plan.get("semantic_inputs", {})
    if not isinstance(definitions, dict):
        return effective, missing, ["semantic_inputs"]

    def supplied_value(name: str) -> Any:
        definition = definitions.get(name, {})
        if not isinstance(definition, dict):
            return None
        inferred = _semantic_value_from_customer_text(definition, customer_text)
        if inferred is not None:
            return inferred
        location = definition.get("location", "slots")
        values = (
            task.slots if location == "slots" else task.entities if location == "entities" else {}
        )
        candidate = values.get(name)
        if candidate not in (None, ""):
            return candidate
        if definition.get("default_policy") == "when_unspecified":
            return definition.get("default")
        return None

    for field_name, spec in definitions.items():
        if not isinstance(field_name, str) or not isinstance(spec, dict):
            unsupported.append(str(field_name))
            continue
        location = spec.get("location", "slots")
        values = (
            task.slots if location == "slots" else task.entities if location == "entities" else {}
        )
        # A registered current-turn phrase fills a missing/unevidenced value.
        # Explicit field-level evidence has already passed the interpretation
        # gate, which rejects a proposal whose evidence supports another value.
        value = _semantic_value_from_customer_text(spec, customer_text) or values.get(field_name)
        if value in (None, ""):
            for conditional in spec.get("conditional_defaults", []):
                when = conditional.get("when", {}) if isinstance(conditional, dict) else {}
                if (
                    isinstance(when, dict)
                    and when
                    and all(
                        str(supplied_value(str(dependency)) or "") == str(expected)
                        for dependency, expected in when.items()
                    )
                ):
                    value = conditional.get("value")
                    break
            if (
                value in (None, "")
                and spec.get("default_policy") == "when_unspecified"
                and spec.get("default") is not None
            ):
                value = str(spec["default"])
        if value in (None, ""):
            if spec.get("required") is True:
                missing.append(field_name)
            continue
        allowed_values = spec.get("allowed_values", [])
        if not isinstance(allowed_values, list) or str(value) not in allowed_values:
            unsupported.append(field_name)
            continue
        effective[field_name] = str(value)
    return effective, missing, unsupported


def _apply_registered_semantic_evidence(
    task: InterpretedTask,
    execution_plan: dict[str, Any],
    *,
    customer_text: str,
) -> InterpretedTask:
    """Reconcile model semantic values with unambiguous registered wording.

    Phrase-derived selections are anchored to the complete current-turn quote
    before interpretation validation. This lets a deterministic contract
    correct an omitted or contradictory model fallback without weakening the
    normal evidence gate. Ambiguous phrases leave the model value untouched.
    """
    definitions = execution_plan.get("semantic_inputs", {})
    if not isinstance(definitions, dict) or not customer_text:
        return task
    slots = dict(task.slots)
    entities = dict(task.entities)
    evidence = list(task.evidence)
    changed = False
    for field_name, spec in definitions.items():
        if not isinstance(field_name, str) or not isinstance(spec, dict):
            continue
        inferred = _semantic_value_from_customer_text(spec, customer_text)
        if inferred is None:
            continue
        location = spec.get("location", "slots")
        target = slots if location == "slots" else entities if location == "entities" else None
        if target is None:
            continue
        field_evidence = [item for item in evidence if item.field == field_name]
        existing = target.get(field_name)
        # Do not silently rewrite an explicitly evidenced model selection.
        # The interpretation gate must reject a value whose cited evidence
        # does not support it. Recovery is for omitted fields and bare model
        # fallbacks that have no field-level evidence.
        if existing not in (None, "") and existing != inferred and field_evidence:
            continue
        if existing == inferred and any(
            item.source == "current_turn" and _normalise(customer_text) in _normalise(item.quote)
            for item in field_evidence
        ):
            continue
        target[field_name] = inferred
        evidence = [item for item in evidence if item.field != field_name]
        evidence.append(
            TaskEvidence(
                field=field_name,
                source="current_turn",
                quote=customer_text,
            )
        )
        changed = True
    if not changed:
        return task
    return task.model_copy(
        update={
            "slots": slots,
            "entities": entities,
            "evidence": evidence,
        }
    )


def _extract_registered_entities(
    task: InterpretedTask,
    execution_plan: dict[str, Any],
    *,
    customer_text: str,
) -> tuple[InterpretedTask, set[str]]:
    """Fill a missing optional entity only through its registered extractor.

    The extracted value must be an exact substring of the current customer
    turn. This is intentionally a small, contract-driven parser rather than a
    model-generated or backend-inferred merchant identity.
    """
    extractors = execution_plan.get("entity_extractors", {})
    if not isinstance(extractors, dict) or not customer_text:
        return task, set()
    entities = dict(task.entities)
    evidence = list(task.evidence)
    extracted: set[str] = set()

    # Correct a model-supplied merchant string when it accidentally includes a
    # registered trailing month-grouping instruction. The shortened value must
    # still be an exact substring of this customer turn before it is accepted.
    supplied_merchant = entities.get("merchant_query")
    if isinstance(supplied_merchant, str):
        cleaned_merchant = _strip_registered_grouping_suffix(supplied_merchant, execution_plan)
        if (
            cleaned_merchant != supplied_merchant
            and cleaned_merchant
            and cleaned_merchant.casefold() in customer_text.casefold()
        ):
            entities["merchant_query"] = cleaned_merchant
            evidence = [item for item in evidence if item.field != "merchant_query"]
            evidence.append(
                TaskEvidence(
                    field="merchant_query",
                    source="current_turn",
                    quote=cleaned_merchant,
                )
            )
            extracted.add("merchant_query")

    # Category terms are declared by OCCF so a spending type is not mistaken
    # for a merchant description. Preserve the customer's exact spelling in
    # the filter/evidence; MCP applies it only to the recorded category field.
    category_spec = extractors.get("category_query")
    if "category_query" not in entities and isinstance(category_spec, dict):
        terms = category_spec.get("category_terms", [])
        maximum = category_spec.get("max_length", 80)
        matches: list[tuple[int, str]] = []
        if isinstance(terms, list) and isinstance(maximum, int):
            for term in terms:
                term_text = str(term or "").strip()
                if not term_text or len(term_text) > maximum:
                    continue
                pattern = (
                    r"(?<!\w)"
                    + r"\s+".join(re.escape(part) for part in term_text.split())
                    + r"(?!\w)"
                )
                match = re.search(pattern, customer_text, re.IGNORECASE)
                if match:
                    matches.append((len(term_text.split()), match.group(0)))
        if matches:
            longest = max(item[0] for item in matches)
            values = {item[1].casefold() for item in matches if item[0] == longest}
            if len(values) == 1:
                candidate = next(item[1] for item in matches if item[0] == longest)
                entities["category_query"] = candidate
                evidence = [item for item in evidence if item.field != "category_query"]
                evidence.append(
                    TaskEvidence(field="category_query", source="current_turn", quote=candidate)
                )
                extracted.add("category_query")
                # If the model redundantly called the same registered category
                # a merchant, retain only the more precise category selector.
                merchant = entities.get("merchant_query")
                if merchant and _normalise(merchant) == _normalise(candidate):
                    entities.pop("merchant_query", None)
                    evidence = [item for item in evidence if item.field != "merchant_query"]

    for field_name, spec in extractors.items():
        if field_name in entities or not isinstance(spec, dict):
            continue
        if field_name == "merchant_query" and "category_query" in entities:
            continue
        patterns = spec.get("patterns", [])
        maximum = spec.get("max_length", 80)
        if not isinstance(patterns, list) or not isinstance(maximum, int):
            continue
        candidate = None
        for pattern in patterns:
            try:
                match = re.search(str(pattern), customer_text, re.IGNORECASE)
            except re.error:
                match = None
            if match and match.lastindex:
                candidate = match.group(1).strip(" \t\r\n.,;:!?\"'")
                break
        if (
            not candidate
            or len(candidate) > maximum
            or candidate.casefold() in {"it", "that", "them", "everything", "all"}
        ):
            continue
        # Avoid carrying possessive/determiner noise into the literal substring
        # filter while retaining the exact captured customer text as evidence.
        candidate = re.sub(r"^(?:my|the|a|an)\s+", "", candidate, flags=re.IGNORECASE).strip()
        if field_name == "merchant_query":
            candidate = _strip_registered_grouping_suffix(candidate, execution_plan)
        if not candidate or customer_text.casefold().find(candidate.casefold()) < 0:
            continue
        entities[str(field_name)] = candidate
        evidence.append(TaskEvidence(field=str(field_name), source="current_turn", quote=candidate))
        extracted.add(str(field_name))
    if extracted:
        task = task.model_copy(update={"entities": entities, "evidence": evidence})
    return task, extracted


def _arguments_for_capability(
    task: InterpretedTask,
    capability_id: str,
    *,
    context: dict[str, Any],
    registry: dict[str, Any],
    plan: dict[str, Any],
    semantic_values: dict[str, str] | None = None,
) -> tuple[dict[str, Any], str | None, list[str]]:
    capability = registry.get("capabilities", {}).get(capability_id, {})
    properties = capability.get("input_schema", {}).get("properties", {})
    arguments = dict(plan.get("arguments", {}))

    # Compile semantic values only through mappings declared by this intent's
    # OCCF plan. The model never proposes the destination argument names or
    # values used by the capability request.
    for field_name, value in (semantic_values or {}).items():
        spec = plan.get("semantic_inputs", {}).get(field_name, {})
        mappings = spec.get("argument_mappings", {}) if isinstance(spec, dict) else {}
        mapped = mappings.get(value, {}) if isinstance(mappings, dict) else {}
        if not isinstance(mapped, dict):
            return (
                {},
                "I couldn't safely map that requested selection to the registered operation.",
                [],
            )
        for target_name, target_value in mapped.items():
            if target_name not in properties:
                return (
                    {},
                    "I couldn't safely map that requested selection to the registered operation.",
                    [],
                )
            arguments[target_name] = target_value

    if capability_id == "banking.contacts.search":
        if task.intent_id == "SAVED_CONTACT_LOOKUP":
            contact_name = task.entities.get("contact_name") or task.slots.get("contact_name")
            if not contact_name:
                return (
                    {},
                    "Whose saved contact details would you like me to find?",
                    ["contact_name"],
                )
            arguments["name_query"] = contact_name
        else:
            beneficiary = (
                task.entities.get("beneficiary")
                or task.entities.get("recipient")
                or task.slots.get("beneficiary")
                or task.slots.get("recipient")
            )
            if not beneficiary:
                return (
                    {},
                    "I understood that you want to prepare a transfer, but I still need the saved recipient's name. Who would you like to send it to? No draft was opened.",
                    ["beneficiary"],
                )
            arguments["name_query"] = beneficiary

    elif capability_id == "frontend.workspace.navigate":
        workspace = (
            task.entities.get("workspace")
            or task.entities.get("workspace_name")
            or task.slots.get("workspace")
            or task.slots.get("workspace_name")
        )
        if not workspace:
            return {}, "Which ORAIflow workspace would you like me to open?", ["workspace"]
        aliases = registry.get("application_policy", {}).get("navigation", {}).get("aliases", {})
        resolved = aliases.get(_normalise(workspace), workspace)
        allowed = registry.get("application_policy", {}).get("navigation", {}).get("pages", [])
        if resolved not in allowed:
            return (
                {},
                "I can help open an ORAIflow workspace. Which workspace did you mean?",
                ["workspace"],
            )
        arguments.update(
            {
                "workspace": resolved,
                "action_id": f"nav-{uuid.uuid4().hex}",
                "navigation_epoch": max(0, int(context.get("navigation_epoch", 0) or 0)),
            }
        )

    elif capability_id == "frontend.account.select":
        account_id = task.entities.get("account_id") or task.slots.get("account_id")
        account_name = (
            task.entities.get("account_name")
            or task.entities.get("account")
            or task.slots.get("account_name")
            or task.slots.get("account")
        )
        accounts = [item for item in context.get("accounts", []) if isinstance(item, dict)]
        matches = [
            item
            for item in accounts
            if account_id and str(item.get("account_id", "")) == str(account_id)
        ]
        if not matches and account_name:
            matches = [
                item
                for item in accounts
                if _normalise(item.get("account_name")) == _normalise(account_name)
            ]
        if len(matches) != 1:
            return (
                {},
                "Which of your listed accounts should I select? Please use its account name.",
                ["account_id"],
            )
        arguments["account_id"] = str(matches[0].get("account_id", ""))

    else:
        # Generic schema-driven slot/entity mapping. Only fields explicitly
        # declared by the capability's OCCF input schema can reach execution.
        for source in (task.slots, task.entities):
            for name, value in source.items():
                if name in properties and name not in arguments:
                    arguments[name] = value
        # Common semantic aliases are mapped only when the OCCF schema declares
        # the destination property; there is no per-phrase route.
        aliases = {
            "beneficiary": "name_query",
            "recipient": "name_query",
            "account": "account_name",
        }
        for source in (task.slots, task.entities):
            for source_name, target_name in aliases.items():
                if (
                    source_name in source
                    and target_name in properties
                    and target_name not in arguments
                ):
                    arguments[target_name] = source[source_name]

    # Derive mandatory input fields from the OCCF JSON Schema, not from a
    # hand-maintained list of phrases or prompts. Pydantic/JSON Schema remains
    # the authoritative final validation below.
    required = capability.get("input_schema", {}).get("required", [])
    missing_fields = [
        str(field)
        for field in required
        if field not in arguments or arguments.get(field) in (None, "", [], {})
    ]
    if missing_fields:
        labels = [
            str(properties.get(field, {}).get("title") or field.replace("_", " "))
            for field in missing_fields
        ]
        if len(labels) == 1:
            question = f"To continue, I need {labels[0]}. Could you provide it?"
        else:
            question = (
                "To continue, I need "
                + ", ".join(labels[:-1])
                + " and "
                + labels[-1]
                + ". Could you provide those details?"
            )
        return arguments, question, missing_fields
    try:
        validate_capability_input(capability_id, arguments)
    except ContractViolation:
        return (
            {},
            "I understood what you want to do, but I need one more detail before I can safely continue. Could you clarify the missing information?",
            [],
        )
    return arguments, None, []


def _plan_resource_task(
    task: InterpretedTask,
    *,
    sources: dict[str, str],
    registry: dict[str, Any],
) -> dict[str, Any]:
    """Validate a catalogue resource task without inventing an MCP capability."""
    intent = registry.get("intents", {}).get(task.intent_id, {})
    plan = intent.get("execution_plan", {}) if isinstance(intent, dict) else {}
    resource_id = str(plan.get("resource_id", ""))
    allowed_resources = {"academy_topics", "workspace_catalogue", "assistant_capabilities"}
    if resource_id not in allowed_resources:
        return {
            "task_id": f"task-{uuid.uuid4().hex}",
            "intent_id": task.intent_id,
            "task_class": "resource",
            "status": "REJECTED",
            "reason_code": "RESOURCE_NOT_REGISTERED",
            "clarification_question": "I couldn't resolve that request to a registered ORAIflow information resource.",
        }

    customer_evidence = [
        item
        for item in task.evidence
        if item.field == "intent"
        and item.source in {"current_turn", "conversation"}
        and item.quote.casefold() in str(sources.get(item.source, "")).casefold()
    ]
    if not customer_evidence:
        return {
            "task_id": f"task-{uuid.uuid4().hex}",
            "intent_id": task.intent_id,
            "task_class": "resource",
            "status": "CLARIFICATION_REQUIRED",
            "reason_code": "RESOURCE_INTENT_EVIDENCE_MISSING",
            "clarification_question": "I can help with the ORAIflow information resources. What concept or workspace would you like explained?",
        }

    load_decision_routing_policy()  # Validate the human routing policy before dispatch.
    from services.agent_runtime.interpretation import confidence_gate_configuration

    gate = confidence_gate_configuration()
    if not gate.get("valid"):
        return {
            "task_id": f"task-{uuid.uuid4().hex}",
            "intent_id": task.intent_id,
            "task_class": "resource",
            "status": "REJECTED",
            "reason_code": "CONFIDENCE_GATE_CONFIGURATION_INVALID",
            "clarification_question": "I couldn't validate the interpretation configuration. Please try again later.",
        }
    if gate.get("mode") == "enforce":
        score = task.intent_confidence
        threshold = gate.get("thresholds", {}).get("intent")
        if score is None or threshold is None or score < threshold:
            return {
                "task_id": f"task-{uuid.uuid4().hex}",
                "intent_id": task.intent_id,
                "task_class": "resource",
                "status": "CLARIFICATION_REQUIRED",
                "reason_code": "RESOURCE_INTENT_CONFIDENCE_BELOW_THRESHOLD",
                "clarification_question": task.clarification_question
                or "I found a relevant information area, but I want to make sure I explain the right topic. Which concept did you mean?",
            }

    application = registry.get("application_policy", {})
    references: dict[str, str] = {}
    if resource_id == "academy_topics":
        topic_ids = {
            str(item.get("topic_id"))
            for item in application.get("academy_topics", [])
            if isinstance(item, dict) and item.get("topic_id")
        }
        raw_topic_ids = task.entities.get("topic_ids") or task.slots.get("topic_ids")
        topic_id = task.entities.get("topic_id") or task.slots.get("topic_id")
        selected_topic_ids = (
            [value.strip() for value in str(raw_topic_ids).split(",") if value.strip()]
            if raw_topic_ids
            else ([topic_id] if topic_id else [])
        )
        unknown_topics = [value for value in selected_topic_ids if value not in topic_ids]
        if unknown_topics:
            return {
                "task_id": f"task-{uuid.uuid4().hex}",
                "intent_id": task.intent_id,
                "task_class": "resource",
                "status": "CLARIFICATION_REQUIRED",
                "reason_code": "UNKNOWN_ACADEMY_TOPIC",
                "clarification_question": "I couldn't match that to a current Academy topic. Which financial concept would you like to explore?",
            }
        if len(selected_topic_ids) == 1:
            references["topic_id"] = selected_topic_ids[0]
        elif selected_topic_ids:
            references["topic_ids"] = selected_topic_ids
    elif resource_id == "workspace_catalogue":
        workspace = task.entities.get("workspace") or task.slots.get("workspace")
        if workspace:
            aliases = application.get("navigation", {}).get("aliases", {})
            resolved = next(
                (
                    target
                    for alias, target in aliases.items()
                    if _normalise(alias) == _normalise(workspace)
                ),
                workspace,
            )
            if resolved not in application.get("workspaces", {}):
                return {
                    "task_id": f"task-{uuid.uuid4().hex}",
                    "intent_id": task.intent_id,
                    "task_class": "resource",
                    "status": "CLARIFICATION_REQUIRED",
                    "reason_code": "UNKNOWN_WORKSPACE_RESOURCE",
                    "clarification_question": "Which ORAIflow workspace would you like me to explain?",
                }
            references["workspace"] = str(resolved)

    dispatch = task_dispatch_for_class("resource")
    return {
        "task_id": f"task-{uuid.uuid4().hex}",
        "intent_id": task.intent_id,
        "task_class": "resource",
        "status": "RESOURCE_READY",
        "resource_id": resource_id,
        "resource_references": references,
        "dispatch_handler": dispatch.handler if dispatch else "registered_resource",
        "response_mode": str(plan.get("response_mode", "registered_resource")),
        "execution_plan": plan,
        "assessment": {
            "intent_id": task.intent_id,
            "intent_confidence": task.intent_confidence,
            "evidence": [item.model_dump(mode="json") for item in customer_evidence],
        },
    }


def _canonicalize_semantic_field_locations(
    task: InterpretedTask,
    execution_plan: dict[str, Any],
) -> tuple[InterpretedTask, list[str]]:
    """Move a recognized field to the location declared by its OCCF contract.

    Models occasionally place a named filter (for example ``merchant_query``)
    in ``slots`` even though OCCF declares it as an entity. The field and its
    evidence are still grounded; rejecting it as an undeclared slot is a
    transport-shape error, not a semantic error. This normalization is limited
    to fields explicitly declared on this intent and never admits new names.
    Conflicting values in both locations remain a hard rejection.
    """
    slots = dict(task.slots)
    entities = dict(task.entities)
    plan_slots = set(execution_plan.get("required_slots", []) or []) | set(
        execution_plan.get("optional_slots", []) or []
    )
    plan_entities = set(execution_plan.get("required_entities", []) or []) | set(
        execution_plan.get("optional_entities", []) or []
    )
    definitions = execution_plan.get("semantic_inputs", {})
    if isinstance(definitions, dict):
        for name, spec in definitions.items():
            if not isinstance(spec, dict):
                continue
            location = spec.get("location", "slots")
            if location == "slots":
                plan_slots.add(str(name))
            elif location == "entities":
                plan_entities.add(str(name))

    conflicts: list[str] = []
    changed = False
    moved_slot_to_entity = False
    moved_entity_to_slot = False
    for name in sorted(plan_entities - plan_slots):
        if name in slots:
            if name in entities and str(entities[name]) != str(slots[name]):
                conflicts.append(name)
            else:
                entities.setdefault(name, slots[name])
                slots.pop(name, None)
                changed = True
                moved_slot_to_entity = True
    for name in sorted(plan_slots - plan_entities):
        if name in entities:
            if name in slots and str(slots[name]) != str(entities[name]):
                conflicts.append(name)
            else:
                slots.setdefault(name, entities[name])
                entities.pop(name, None)
                changed = True
                moved_entity_to_slot = True
    if conflicts:
        return task, conflicts
    if changed:
        updates: dict[str, Any] = {"slots": slots, "entities": entities}
        # The value was grounded, but placed in the wrong semantic bucket.
        # Reuse its supplied confidence in the canonical bucket only when the
        # model omitted that bucket's confidence; do not overwrite explicit
        # confidence values or invent a score.
        if (
            moved_slot_to_entity
            and task.entity_confidence is None
            and task.slot_confidence is not None
        ):
            updates["entity_confidence"] = task.slot_confidence
        if (
            moved_entity_to_slot
            and task.slot_confidence is None
            and task.entity_confidence is not None
        ):
            updates["slot_confidence"] = task.entity_confidence
        task = task.model_copy(update=updates)
    return task, []


def plan_interpreted_task(
    task: InterpretedTask,
    *,
    sources: dict[str, str],
    messages: list[Any] | tuple[Any, ...],
    context: dict[str, Any],
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one semantic task and map it to an OCCF execution plan."""
    registry = registry or get_registry()
    task_id = f"task-{uuid.uuid4().hex}"
    if task.intent_id not in registry.get("intents", {}):
        return {
            "task_id": task_id,
            "intent_id": task.intent_id,
            "status": "REJECTED",
            "reason_code": "UNKNOWN_INTENT",
            "clarification_question": "I couldn't match that request to a registered ORAIflow capability. Could you clarify what you would like to accomplish?",
        }

    known_message_ids = {
        str(getattr(item, "id", "")) for item in messages or () if getattr(item, "id", None)
    }
    unresolved_references = [
        ref for ref in task.conversation_references if ref not in known_message_ids
    ]
    if unresolved_references:
        return {
            "task_id": task_id,
            "intent_id": task.intent_id,
            "status": "CLARIFICATION_REQUIRED",
            "reason_code": "CONVERSATION_REFERENCE_UNRESOLVED",
            "clarification_question": "I couldn't reliably identify the earlier item you referred to. Could you point me to it again?",
        }

    if task.resolution == "ABSTAIN":
        return {
            "task_id": task_id,
            "intent_id": task.intent_id,
            "status": "CLARIFICATION_REQUIRED",
            "reason_code": "MODEL_ABSTAIN",
            "clarification_question": task.clarification_question
            or "I'm not certain I understood. Could you describe what you'd like to do?",
        }
    task_class = classify_intent_task_class(task.intent_id, registry)
    dispatch = task_dispatch_for_class(task_class)
    if task_class == "resource":
        planned_resource = _plan_resource_task(task, sources=sources, registry=registry)
        planned_resource["model_resolution"] = task.resolution
        planned_resource["model_clarification_reconciled"] = (
            task.resolution == "NEEDS_CLARIFICATION"
            and planned_resource.get("status") == "RESOURCE_READY"
        )
        return planned_resource

    capability_id = intent_validation_capability(task.intent_id, registry)
    if not capability_id:
        return {
            "task_id": task_id,
            "intent_id": task.intent_id,
            "task_class": task_class,
            "status": "REJECTED",
            "reason_code": "NO_REGISTERED_CAPABILITY_MAPPING",
            "clarification_question": "I couldn't map that request to a registered ORAIflow capability or information resource. Could you clarify what outcome you need?",
        }

    intent = registry["intents"][task.intent_id]
    execution_plan = intent.get("execution_plan", {})
    semantic_inputs = (
        execution_plan.get("semantic_inputs", {}) if isinstance(execution_plan, dict) else {}
    )
    task, location_conflicts = _canonicalize_semantic_field_locations(task, execution_plan)
    if location_conflicts:
        return {
            "task_id": task_id,
            "intent_id": task.intent_id,
            "task_class": task_class,
            "status": "REJECTED",
            "reason_code": "CONFLICTING_SEMANTIC_FIELD_LOCATIONS",
            "missing_fields": location_conflicts,
            "clarification_question": "I found conflicting values for one of the requested details. Could you restate that detail?",
        }

    # Resolve a customer-provided account name that the model placed in the
    # optional preference field. Only a unique exact match in validated UI
    # context is normalized; backend account ownership/eligibility is checked
    # again before a transfer draft can be prepared.
    if task.intent_id == "TRANSFER_PREPARATION":
        normalized_slots = dict(task.slots)
        normalized_evidence = list(task.evidence)
        changed = False
        for supplied_field in ("source_account_preference", "source_account"):
            supplied_value = str(normalized_slots.get(supplied_field) or "").strip()
            if not supplied_value:
                continue
            # Registered balance preferences remain preferences; only a name
            # that uniquely matches a context account is normalized as one.
            if supplied_field == "source_account_preference" and supplied_value in {
                "highest_balance",
                "lowest_balance",
            }:
                continue
            supplied_name = _normalise_account_reference(supplied_value)
            account_matches = [
                item
                for item in context.get("accounts", [])
                if isinstance(item, dict)
                and item.get("account_name")
                and _normalise_account_reference(item.get("account_name")) == supplied_name
                and supplied_name
            ]
            if len(account_matches) != 1:
                continue
            canonical_name = str(account_matches[0].get("account_name", ""))
            (
                normalized_slots.pop("source_account_preference", None)
                if supplied_field == "source_account_preference"
                else None
            )
            normalized_slots["source_account"] = canonical_name
            normalized_evidence = [
                (
                    TaskEvidence(
                        field="source_account",
                        source=item.source,
                        quote=item.quote,
                    )
                    if item.field == supplied_field
                    else item
                )
                for item in normalized_evidence
            ]
            changed = True
            break
        if changed:
            task = task.model_copy(
                update={"slots": normalized_slots, "evidence": normalized_evidence}
            )

    # Optional and required semantic inputs belong to the OCCF intent contract.
    # Do not silently accept model-invented fields or unsupported enum values.
    declared_slots = set(execution_plan.get("required_slots", [])) | set(
        execution_plan.get("optional_slots", [])
    )
    declared_slots.update(
        field_name
        for field_name, spec in semantic_inputs.items()
        if isinstance(spec, dict) and spec.get("location", "slots") == "slots"
    )
    if declared_slots:
        unexpected_slots = sorted(set(task.slots) - declared_slots)
        if unexpected_slots:
            return {
                "task_id": task_id,
                "intent_id": task.intent_id,
                "task_class": task_class,
                "status": "REJECTED",
                "reason_code": "UNDECLARED_INTENT_SLOT",
                "missing_fields": unexpected_slots,
                "clarification_question": "I understood the request, but one of the details does not match the registered workflow inputs. Could you restate that detail?",
            }
    declared_entities = set(execution_plan.get("required_entities", [])) | set(
        execution_plan.get("optional_entities", [])
    )
    declared_entities.update(
        field_name
        for field_name, spec in semantic_inputs.items()
        if isinstance(spec, dict) and spec.get("location") == "entities"
    )
    if declared_entities:
        unexpected_entities = sorted(set(task.entities) - declared_entities)
        if unexpected_entities:
            return {
                "task_id": task_id,
                "intent_id": task.intent_id,
                "task_class": task_class,
                "status": "REJECTED",
                "reason_code": "UNDECLARED_INTENT_ENTITY",
                "missing_fields": unexpected_entities,
                "clarification_question": "I understood the request, but one of the referenced items is not a supported workflow input. Could you clarify that detail?",
            }
    model_entity_fields = set(task.entities)
    task, extracted_entity_fields = _extract_registered_entities(
        task,
        execution_plan,
        customer_text=str(sources.get("current_turn", "")),
    )
    task = _apply_registered_semantic_evidence(
        task,
        execution_plan,
        customer_text=str(sources.get("current_turn", "")),
    )
    for field_name, allowed_values in execution_plan.get("slot_values", {}).items():
        if field_name in task.slots and task.slots[field_name] not in allowed_values:
            return {
                "task_id": task_id,
                "intent_id": task.intent_id,
                "task_class": task_class,
                "status": "CLARIFICATION_REQUIRED",
                "reason_code": "INTENT_SLOT_VALUE_UNSUPPORTED",
                "missing_fields": [field_name],
                "clarification_question": f"I can apply a supported {field_name.replace('_', ' ')} preference, but I couldn't validate the value provided. Could you clarify it?",
            }
    # In an intent-only protocol, capability mapping is runtime-owned. The
    # capability-selection confidence dimension is therefore not applicable.
    assessment_payload = _assessment_for_capability(task, capability_id)
    configured_dimensions = (
        set(dispatch.applicable_confidence_dimensions) if dispatch else {"intent", "slot", "entity"}
    )
    applicable_dimensions = {"intent"} if "intent" in configured_dimensions else set()
    if "slot" in configured_dimensions and (task.slots or execution_plan.get("required_slots")):
        applicable_dimensions.add("slot")
    model_supplied_entities = model_entity_fields | (set(task.entities) - extracted_entity_fields)
    if "entity" in configured_dimensions and (
        model_supplied_entities or execution_plan.get("required_entities")
    ):
        applicable_dimensions.add("entity")
    assessment, decision = validate_interpretation_assessment(
        assessment_payload,
        expected_capability_id=capability_id,
        sources=sources,
        applicable_confidence_dimensions=applicable_dimensions,
        allow_provisional_clarification=True,
    )
    if decision.get("decision") != "PASS":
        missing_task_fields = [
            field for field in execution_plan.get("required_slots", []) if field not in task.slots
        ] + [
            field
            for field in execution_plan.get("required_entities", [])
            if field not in task.entities
        ]
        reported_fields = list(decision.get("fields", []) or decision.get("dimensions", []))
        if not reported_fields:
            reported_fields = missing_task_fields
        return {
            "task_id": task_id,
            "intent_id": task.intent_id,
            "status": (
                "CLARIFICATION_REQUIRED"
                if decision.get("decision") in {"CLARIFY", "ABSTAIN"}
                else "REJECTED"
            ),
            "reason_code": str(decision.get("code", "ASSESSMENT_REJECTED")),
            "missing_fields": reported_fields,
            "clarification_question": task.clarification_question,
            "validation": decision,
        }

    semantic_values, missing_semantic_inputs, unsupported_semantic_inputs = (
        _resolve_semantic_inputs(
            task,
            execution_plan,
            customer_text=str(sources.get("current_turn", "")),
        )
    )
    if unsupported_semantic_inputs:
        return {
            "task_id": task_id,
            "intent_id": task.intent_id,
            "task_class": task_class,
            "status": "CLARIFICATION_REQUIRED",
            "reason_code": "INTENT_SEMANTIC_VALUE_UNSUPPORTED",
            "missing_fields": unsupported_semantic_inputs,
            "clarification_question": "I understood the requested selection, but this operation does not support that option. Could you choose a supported item or selection?",
            "capability_id": capability_id,
        }
    if missing_semantic_inputs:
        labels = [str(field).replace("_", " ") for field in missing_semantic_inputs]
        question = (
            "I need "
            + " and ".join(labels)
            + " before I can continue. Could you provide that detail?"
        )
        return {
            "task_id": task_id,
            "intent_id": task.intent_id,
            "task_class": task_class,
            "status": "CLARIFICATION_REQUIRED",
            "reason_code": "SEMANTIC_REQUIRED_FIELD_MISSING",
            "missing_fields": missing_semantic_inputs,
            "clarification_question": question,
            "capability_id": capability_id,
        }

    # Semantic required fields are declared on the OCCF intent execution plan.
    # They are validated independently from the capability's input schema.
    missing_semantic_fields = [
        field for field in execution_plan.get("required_slots", []) if field not in task.slots
    ] + [
        field for field in execution_plan.get("required_entities", []) if field not in task.entities
    ]
    if missing_semantic_fields:
        labels = [str(field).replace("_", " ") for field in missing_semantic_fields]
        question = (
            "I need "
            + " and ".join(labels)
            + " before I can continue. Could you provide that detail?"
        )
        return {
            "task_id": task_id,
            "intent_id": task.intent_id,
            "status": "CLARIFICATION_REQUIRED",
            "reason_code": "SEMANTIC_REQUIRED_FIELD_MISSING",
            "missing_fields": missing_semantic_fields,
            "clarification_question": question,
            "capability_id": capability_id,
        }

    arguments, clarification, missing_schema_fields = _arguments_for_capability(
        task,
        capability_id,
        context=context,
        registry=registry,
        plan=execution_plan,
        semantic_values=semantic_values,
    )
    if clarification:
        return {
            "task_id": task_id,
            "intent_id": task.intent_id,
            "status": "CLARIFICATION_REQUIRED",
            "reason_code": "REQUIRED_INPUT_MISSING",
            "clarification_question": clarification,
            "missing_fields": missing_schema_fields,
            "capability_id": capability_id,
            "assessment": assessment.model_dump(mode="json") if assessment else None,
        }

    capability = registry.get("capabilities", {}).get(capability_id, {})
    if capability.get("authorization", {}).get("agent_invocable") is not True:
        return {
            "task_id": task_id,
            "intent_id": task.intent_id,
            "status": "REJECTED",
            "reason_code": "CAPABILITY_NOT_AGENT_INVOKABLE",
            "clarification_question": "That request is not available through the ORAIflow assistant.",
        }
    kind = capability.get("kind")
    if kind == "mcp" and capability.get("execution", {}).get("effect") not in {"read", "read_only"}:
        return {
            "task_id": task_id,
            "intent_id": task.intent_id,
            "status": "REJECTED",
            "reason_code": "NON_READ_MCP_CAPABILITY",
            "clarification_question": "I can't execute that financial operation. I can help prepare supported actions through their separately approved workflow.",
        }
    if kind == "frontend" and capability.get("execution", {}).get("model_invocable") is not True:
        return {
            "task_id": task_id,
            "intent_id": task.intent_id,
            "status": "REJECTED",
            "reason_code": "FRONTEND_ACTION_NOT_MODEL_INVOKABLE",
            "clarification_question": "That action must be completed through its separately guarded workflow.",
        }

    # Only now has the planner proven that the proposed meaning, registered
    # defaults, compiled arguments and exact capability schema form a complete
    # executable plan. Normalize the assessment passed to the final OCCF/tool
    # boundary; the original model resolution remains available for diagnostics.
    if assessment is not None and task.resolution == "NEEDS_CLARIFICATION":
        assessment = assessment.model_copy(update={"resolution": "READY"})

    return {
        "task_id": task_id,
        "intent_id": task.intent_id,
        "task_class": task_class,
        "dispatch_handler": dispatch.handler if dispatch else None,
        "status": "PLANNED",
        "capability_id": capability_id,
        "tool_name": str(capability.get("name", "")),
        "kind": kind,
        "arguments": arguments,
        "semantic_values": semantic_values,
        "assessment": assessment.model_dump(mode="json") if assessment else assessment_payload,
        "presentation_id": execution_plan.get("presentation"),
        "response_mode": execution_plan.get("response_mode")
        or ("presentation" if execution_plan.get("presentation") else "grounded_explanation"),
        "execution_plan": execution_plan,
        "applicable_confidence_dimensions": sorted(applicable_dimensions),
        "model_resolution": task.resolution,
        "model_clarification_reconciled": task.resolution == "NEEDS_CLARIFICATION",
    }


def assessment_tool_arguments(task: dict[str, Any]) -> dict[str, Any]:
    """Project a runtime-validated assessment into the OCCF tool adapter.

    The OCCF tool validator remains the final argument and provenance gate.
    This projection preserves that final check while the planner owns semantic
    resolution and executable argument compilation.
    """
    assessment = task.get("assessment") or {}
    evidence = assessment.get("evidence", []) if isinstance(assessment, dict) else []
    return {
        "orai_intent_id": task.get("intent_id"),
        "orai_intent_confidence": assessment.get("intent_confidence"),
        "orai_slot_confidence": assessment.get("slot_confidence"),
        "orai_entity_confidence": assessment.get("entity_confidence"),
        "orai_capability_selection_confidence": None,
        "orai_resolution": assessment.get("resolution", "READY"),
        "orai_slots": assessment.get("slots", {}),
        "orai_entities": assessment.get("entities", {}),
        "orai_evidence": [
            f"{item.get('field')}|{item.get('source')}|{item.get('quote')}"
            for item in evidence
            if isinstance(item, dict)
        ],
        "orai_clarification_question": assessment.get("clarification_question"),
    }
