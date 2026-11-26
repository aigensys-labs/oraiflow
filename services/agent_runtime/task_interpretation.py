"""Interpretation-only turn contracts and OCCF task planning primitives.

The model may describe customer intent, slots, entities and source quotations.
It cannot select or invoke an executable capability. The runtime maps each
validated intent to its canonical OCCF execution plan.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from shared.contracts.registry import get_registry


class TaskEvidence(BaseModel):
    """Evidence and provenance supplied to the deterministic task interpretation stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str = Field(
        min_length=1,
        max_length=80,
        description="The exact registered slot/entity field supported by this quotation, or 'intent' for task selection.",
    )
    source: Literal[
        "current_turn",
        "conversation",
        "assistant_context",
        "application_context",
        "capability_result",
    ]
    quote: str = Field(
        min_length=1,
        max_length=500,
        description="A short exact quotation from the current turn, prior customer message, or supplied validated context. Never paraphrase evidence.",
    )


class InterpretedTask(BaseModel):
    """One semantic task proposed by the model, without a capability choice."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str = Field(
        min_length=1,
        max_length=80,
        description="One exact intent_id from the registered ORAIflow decision map; never a capability or tool name.",
    )
    requested_outcome: Literal[
        "INFORMATION", "EXPLANATION", "EDUCATION", "NAVIGATION", "ACTION", "UNKNOWN"
    ] = Field(
        default="INFORMATION",
        description="What the customer wants: information, explanation, education, navigation, or an action workflow.",
    )
    resolution: Literal["READY", "NEEDS_CLARIFICATION", "ABSTAIN"] = Field(
        default="READY",
        description="Your provisional assessment only. The planner decides whether OCCF defaults and required inputs make the contract executable.",
    )
    slots: dict[str, str] = Field(
        default_factory=dict,
        max_length=40,
        description="Registered intent input fields and customer-supported scalar values. Keep scope, selectors, amount, currency and source-account choice distinct.",
    )
    entities: dict[str, str] = Field(
        default_factory=dict,
        max_length=40,
        description="Named people, accounts, workspaces, resources or other registered entities; do not put them into unrelated preference fields.",
    )
    evidence: list[TaskEvidence] = Field(
        default_factory=list,
        max_length=40,
        description="Field-specific exact source quotations for every proposed slot/entity and for the intent.",
    )
    intent_confidence: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Confidence that this registered intent matches the customer's goal.",
    )
    slot_confidence: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Confidence in the extracted slot values; null when no slots are proposed.",
    )
    entity_confidence: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Confidence in the extracted entities; null when no entities are proposed.",
    )
    clarification_question: str | None = Field(
        default=None,
        max_length=500,
        description="Ask only for genuinely required information absent from the conversation and validated application context.",
    )
    conversation_references: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Exact prior message IDs referenced by this task, when needed.",
    )


class TurnInterpretationAssessment(BaseModel):
    """Strict model-to-runtime interpretation envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    resolution: Literal["RESOLVED", "NEEDS_CLARIFICATION", "OUT_OF_SCOPE", "SOCIAL"] = "RESOLVED"
    tasks: list[InterpretedTask] = Field(default_factory=list, max_length=5)
    clarification_message: str | None = Field(default=None, max_length=500)


class AssessmentToolArguments(TurnInterpretationAssessment):
    """Provider-native structured assessment arguments.

    Keep the assessment fields at the top level. Wrapping the entire assessment
    in an ``assessment_json`` string made model output unnecessarily fragile and
    introduced a second JSON parsing boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


def parse_assessment_json(raw: Any) -> tuple[TurnInterpretationAssessment | None, dict[str, Any]]:
    """Parse direct structured arguments (and legacy JSON strings) strictly.

    Provider outputs are normalized only for primitive slot/entity values.
    Unknown fields, booleans, nested objects and arrays remain rejected.
    """
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return None, {"code": "ASSESSMENT_JSON_INVALID", "detail_type": type(exc).__name__}
    elif isinstance(raw, dict):
        payload = dict(raw)
        # Read compatibility only; newly generated tool schemas never use this
        # nested transport.
        if set(payload) == {"assessment_json"} and isinstance(payload.get("assessment_json"), str):
            return parse_assessment_json(payload["assessment_json"])
    else:
        return None, {"code": "ASSESSMENT_PAYLOAD_INVALID", "detail_type": type(raw).__name__}

    try:
        if isinstance(payload, dict) and isinstance(payload.get("tasks"), list):
            normalized_tasks = []
            for item in payload["tasks"]:
                if not isinstance(item, dict):
                    normalized_tasks.append(item)
                    continue
                item = dict(item)
                for field_name in ("slots", "entities"):
                    values = item.get(field_name)
                    if isinstance(values, dict):
                        normalized = {}
                        for key, value in values.items():
                            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                                raise ValueError(
                                    f"{field_name}.{key} must be a scalar string/number"
                                )
                            normalized[str(key)] = str(value)
                        item[field_name] = normalized
                normalized_tasks.append(item)
            payload = dict(payload)
            payload["tasks"] = normalized_tasks
        assessment = TurnInterpretationAssessment.model_validate(payload)
    except (ValidationError, TypeError, ValueError) as exc:
        details = []
        if isinstance(exc, ValidationError):
            details = [
                f"{'.'.join(str(part) for part in error.get('loc', ())) or 'assessment'}:{error.get('type', 'invalid')}"
                for error in exc.errors(include_url=False)[:12]
            ]
        return None, {
            "code": "ASSESSMENT_SCHEMA_INVALID",
            "detail_type": type(exc).__name__,
            "issues": details,
        }
    if assessment.resolution == "RESOLVED" and not assessment.tasks:
        return None, {"code": "ASSESSMENT_EMPTY_TASKS", "detail_type": "empty_resolved_assessment"}
    return assessment, {"code": "ASSESSMENT_VALID", "task_count": len(assessment.tasks)}


def _exact_source_phrase(source_text: str, phrase: str) -> str | None:
    """Return the verbatim source span matching a registered phrase."""
    phrase = str(phrase or "").strip()
    if not source_text or not phrase:
        return None
    words = re.split(r"\s+", phrase)
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(word) for word in words) + r"(?!\w)"
    match = re.search(pattern, source_text, flags=re.IGNORECASE)
    return match.group(0) if match else None


def _registered_semantic_match(spec: dict[str, Any], current_text: str) -> tuple[str, str] | None:
    """Resolve only a unique longest phrase declared by the OCCF field contract."""
    phrase_map = spec.get("evidence_phrases", {})
    if not isinstance(phrase_map, dict):
        return None
    matches: list[tuple[int, int, str, str]] = []
    for value, phrases in phrase_map.items():
        if not isinstance(phrases, list):
            continue
        for phrase in phrases:
            quote = _exact_source_phrase(current_text, str(phrase))
            if quote:
                matches.append((len(str(phrase).split()), len(str(phrase)), str(value), quote))
    if not matches:
        return None
    best_rank = max((item[0], item[1]) for item in matches)
    best = [item for item in matches if (item[0], item[1]) == best_rank]
    values = {item[2] for item in best}
    if len(values) != 1:
        return None
    selected_value = next(iter(values))
    quote = max((item[3] for item in best if item[2] == selected_value), key=len)
    return selected_value, quote


def _has_registered_selector_marker(spec: dict[str, Any], current_text: str) -> bool:
    markers = spec.get("evidence_markers", [])
    if not isinstance(markers, list):
        return False
    return any(_exact_source_phrase(current_text, str(marker)) for marker in markers)


def repair_registered_semantic_evidence(
    assessment: TurnInterpretationAssessment,
    current_text: str,
    registry: dict[str, Any] | None = None,
) -> tuple[TurnInterpretationAssessment, dict[str, Any]]:
    """Repair omitted evidence only from exact, registry-declared current-turn spans.

    The interpreter still proposes intent, semantic values and entities. This
    pass does not infer an intent or invent customer wording. It grounds bounded
    enum values only when the selected value is supported by an exact phrase in
    the OCCF contract. Unsupported or ambiguous explicit selectors remain
    unresolved. Unmentioned optional semantic fields are removed from the
    model proposal so the planner can apply their registered defaults.
    """
    registry = registry or get_registry()
    repaired_tasks: list[InterpretedTask] = []
    repaired_fields: list[str] = []
    defaulted_fields: list[str] = []
    for task in assessment.tasks:
        intent = registry.get("intents", {}).get(task.intent_id, {})
        plan = intent.get("execution_plan", {}) if isinstance(intent, dict) else {}
        semantic_specs = plan.get("semantic_inputs", {}) if isinstance(plan, dict) else {}
        semantic_specs = semantic_specs if isinstance(semantic_specs, dict) else {}
        slots = dict(task.slots)
        entities = dict(task.entities)
        evidence = list(task.evidence)
        for field_name, value in list(slots.items()):
            spec = semantic_specs.get(field_name)
            if not isinstance(spec, dict):
                continue
            # OCCF phrase maps are authoritative for bounded semantic fields.
            # Reconcile even when the model supplied a quotation: a real quote
            # such as "spend" must not validate an incompatible value like
            # flow=all or flow=inflows.
            match = _registered_semantic_match(spec, current_text)
            if match is not None:
                matched_value, quote = match
                allowed = spec.get("allowed_values", [])
                if matched_value in allowed:
                    if value != matched_value:
                        repaired_fields.append(field_name)
                    slots[field_name] = matched_value
                    evidence = [item for item in evidence if item.field != field_name]
                    evidence.append(
                        TaskEvidence(field=field_name, source="current_turn", quote=quote)
                    )
                continue

            # An explicit but unsupported selector must not silently become a
            # default. Keep it so the normal validator fails closed and asks.
            if _has_registered_selector_marker(spec, current_text):
                continue

            # If the current turn does not mention this selector, preserve only
            # a field that has an existing customer-authored evidence quote.
            # Otherwise remove the model's guessed/default value so OCCF can
            # apply the registered default after evidence validation.
            field_evidence = [item for item in evidence if item.field == field_name]
            customer_evidence = [
                item for item in field_evidence if item.source in {"current_turn", "conversation"}
            ]
            if (
                not customer_evidence
                and spec.get("required") is not True
                and spec.get("default_policy") == "when_unspecified"
            ):
                slots.pop(field_name, None)
                evidence = [item for item in evidence if item.field != field_name]
                defaulted_fields.append(field_name)

        # Free-text entities are accepted only when the exact proposed value
        # occurs in the current customer turn; repair a bad model quotation
        # with that exact substring rather than treating mere presence as proof.
        for field_name, value in list(entities.items()):
            quote = _exact_source_phrase(current_text, value)
            if quote:
                evidence = [item for item in evidence if item.field != field_name]
                evidence.append(TaskEvidence(field=field_name, source="current_turn", quote=quote))
                if field_name not in {item.field for item in task.evidence} or any(
                    item.field == field_name and item.quote != quote for item in task.evidence
                ):
                    repaired_fields.append(field_name)

        repaired_tasks.append(
            task.model_copy(
                update={
                    "slots": slots,
                    "entities": entities,
                    "evidence": evidence,
                }
            )
        )

    updated = assessment.model_copy(update={"tasks": repaired_tasks})
    return updated, {
        "repaired_fields": sorted(set(repaired_fields)),
        "defaulted_fields": sorted(set(defaulted_fields)),
    }


def safe_customer_clarification(
    clarification_message: str | None,
    candidates: list[dict[str, Any]] | None = None,
) -> str:
    """Return customer-safe clarification copy without echoing registry metadata.

    A model-generated top-level clarification can accidentally quote the
    candidate catalogue's internal capability description. When a registered
    candidate is known, prefer a bounded customer-facing prompt owned by the
    runtime. Preserve a model clarification only when no candidate metadata was
    supplied to the interpreter.
    """
    candidate_ids = {
        str(item.get("intent_id", ""))
        for item in (candidates or [])
        if isinstance(item, dict) and item.get("intent_id")
    }
    if "TRANSACTION_ANALYSIS" in candidate_ids:
        return (
            "I can analyze your recorded spending or incoming payments, but I "
            "couldn't validate the request yet. Could you restate what you'd "
            "like to measure or review? I haven't retrieved transaction data yet."
        )
    if "WORKSPACE_NAVIGATION" in candidate_ids:
        return "Which ORAIflow workspace would you like me to open?"
    if "SAVED_CONTACT_LOOKUP" in candidate_ids:
        return "Which saved contact would you like me to look up?"
    if candidate_ids:
        return (
            "I may be able to help with that in ORAIflow, but I couldn't "
            "validate the request yet. Could you describe the information or "
            "outcome you're looking for? I haven't retrieved or changed anything."
        )
    value = str(clarification_message or "").strip()
    return (
        value[:500]
        if value
        else "Could you clarify what you would like to understand or do in ORAIflow?"
    )


def missing_task_field_evidence(assessment: TurnInterpretationAssessment) -> list[dict[str, Any]]:
    """List model-proposed fields that lack their own evidence quotation.

    Intent evidence alone is not enough to ground a proposed slot/entity. This
    check is deliberately separate from source validation: it detects omissions
    early so the Agent Runtime can perform one bounded evidence-repair attempt.
    """
    missing: list[dict[str, Any]] = []
    for index, task in enumerate(assessment.tasks):
        cited = {item.field for item in task.evidence}
        fields = sorted((set(task.slots) | set(task.entities)) - cited)
        if fields:
            missing.append({"task_index": index, "intent_id": task.intent_id, "fields": fields})
    return missing


def parse_assessment_payload(
    raw: Any,
) -> tuple[TurnInterpretationAssessment | None, dict[str, Any]]:
    """Preferred provider-boundary parser; retained name clarifies direct payloads."""
    return parse_assessment_json(raw)


def intent_validation_capability(
    intent_id: str, registry: dict[str, Any] | None = None
) -> str | None:
    """Choose a validation relationship from OCCF; never accept one from the model."""
    registry = registry or get_registry()
    intent = registry.get("intents", {}).get(intent_id)
    if not isinstance(intent, dict):
        return None
    plan = intent.get("execution_plan", {})
    if not isinstance(plan, dict):
        return None
    # Transfer preparation starts with authenticated contact candidate search,
    # not with the frontend draft action declared as its eventual outcome.
    candidates = [
        plan.get("contact_resolution"),
        plan.get("capability"),
        *(plan.get("supporting_capabilities") or []),
    ]
    for capability_id in candidates:
        if capability_id and capability_id in registry.get("capabilities", {}):
            return str(capability_id)
    presentation_id = plan.get("presentation")
    presentation = registry.get("presentations", {}).get(presentation_id, {})
    for capability_id in presentation.get("binding", {}).get("source_capabilities", []):
        if capability_id in registry.get("capabilities", {}):
            return str(capability_id)
    return None


def bind_structured_assessment_model(model: Any) -> tuple[Any, str]:
    """Prefer the provider's native Pydantic structured-output adapter.

    LangChain normalizes provider-native JSON-schema/function-call modes into
    a Pydantic result. This avoids making every provider reproduce an identical
    optional tool-call envelope. The caller still performs strict validation
    and may fall back to the intercepted assessment tool where unsupported.
    """
    method = getattr(model, "with_structured_output", None)
    if not callable(method):
        raise NotImplementedError("Provider does not expose structured output")
    try:
        return method(AssessmentToolArguments), "native_structured_output"
    except (TypeError, ValueError, NotImplementedError, AttributeError):
        # Some provider integrations require an explicit method name. Keep the
        # preferred mode provider-neutral, then let the caller use the strict
        # tool-call adapter as the compatibility path.
        return method(AssessmentToolArguments, method="json_schema"), "native_json_schema"


def bind_required_assessment_model(
    model: Any, assessment_tool: Any, provider: str
) -> tuple[Any, str]:
    """Bind the assessment channel in the provider's required-tool mode where supported.

    Anthropic calls its forced tool mode ``any``; OpenAI-compatible and Ollama
    chat integrations use ``required``. Unsupported provider options fall back
    to ordinary tool binding, but callers must still validate and perform one
    bounded repair before deciding that interpretation failed.
    """
    provider_name = str(provider or "").casefold()
    choices = ["any", "required"] if provider_name == "anthropic" else ["required", "any"]
    errors: list[Exception] = []
    for choice in choices:
        try:
            return model.bind_tools([assessment_tool], tool_choice=choice), choice
        except (TypeError, ValueError, NotImplementedError) as exc:
            errors.append(exc)
    try:
        return model.bind_tools([assessment_tool]), "provider_default"
    except Exception:
        if errors:
            raise errors[-1]
        raise


def safe_explicit_transfer_assessment(
    current_text: str,
    context: dict[str, Any] | None = None,
) -> TurnInterpretationAssessment | None:
    """Recover only explicit canonical transfer slots after model-format failure.

    This reuses the existing bounded transfer slot parser. It is not a general
    natural-language intent classifier and never opens a draft or authorizes a
    payment. The returned task still passes the same evidence, OCCF, identity,
    contact-search and workflow validation as a model-produced assessment.
    Currency may remain absent; the transfer workflow asks for it after the
    authenticated saved recipient has been resolved.
    """
    from shared.contact_resolution import extract_contact_transfer_request

    parsed = extract_contact_transfer_request(
        current_text,
        context or {},
        allow_currency_missing=True,
    )
    if not parsed:
        return None
    amount = str(parsed.get("amount") or "").strip()
    beneficiary = str(parsed.get("beneficiary_query") or "").strip()
    currency = str(parsed.get("currency") or "").strip().upper()
    if not amount or not beneficiary:
        return None

    slots = {"amount": amount}
    if currency:
        slots["currency"] = currency
    # Never turn the parser's inferred account ID into an instruction. Named
    # account preferences are handled by the existing transfer workflow.
    evidence = [
        TaskEvidence(field="intent", source="current_turn", quote=str(current_text)[:500]),
        TaskEvidence(field="amount", source="current_turn", quote=amount),
        TaskEvidence(field="beneficiary", source="current_turn", quote=beneficiary),
    ]
    if currency:
        evidence.append(TaskEvidence(field="currency", source="current_turn", quote=currency))
    return TurnInterpretationAssessment(
        resolution="RESOLVED",
        tasks=[
            InterpretedTask(
                intent_id="TRANSFER_PREPARATION",
                requested_outcome="ACTION",
                resolution="READY",
                slots=slots,
                entities={"beneficiary": beneficiary},
                evidence=evidence,
                intent_confidence=0.98,
                slot_confidence=0.98,
                entity_confidence=0.98,
            )
        ],
    )


def safe_canonical_read_assessment(
    current_text: str,
    candidates: list[dict[str, Any]],
    registry: dict[str, Any] | None = None,
) -> TurnInterpretationAssessment | None:
    """Build a restricted fallback only for explicit, auto-resolvable reads.

    This is not a fuzzy language parser. It requires an exact registry-declared
    semantic phrase or deterministic recognition match, an explicit OCCF
    auto_resolve declaration (or an exact phrase with a fully defaulted semantic
    contract), a read-only MCP capability and no required semantic/input fields.
    This is a last-resort recovery after LLM failure/empty clarification, not the
    normal interpretation path. Financial writes, transfers and UI actions are
    never eligible.
    """
    registry = registry or get_registry()
    eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for candidate in candidates or []:
        if candidate.get("match_kind") not in {"semantic_phrase", "deterministic_pattern"}:
            continue
        if int(candidate.get("score", 0) or 0) < 1000:
            continue
        intent_id = str(candidate.get("intent_id", ""))
        intent = registry.get("intents", {}).get(intent_id, {})
        discovery = intent.get("semantic_discovery", {}) if isinstance(intent, dict) else {}
        plan = intent.get("execution_plan", {}) if isinstance(intent, dict) else {}
        semantic_inputs = plan.get("semantic_inputs", {}) if isinstance(plan, dict) else {}
        default_complete = bool(semantic_inputs) and all(
            isinstance(spec, dict)
            and spec.get("default_policy") == "when_unspecified"
            and spec.get("default") is not None
            and bool(spec.get("allowed_values"))
            for spec in semantic_inputs.values()
        )
        # Explicit registry phrase + read-only capability + a fully defaulted
        # semantic contract is safe as a recovery after an LLM no-task response.
        # It is not a general bypass for fuzzy or merely keyword candidates.
        if discovery.get("auto_resolve") is not True and not (
            candidate.get("match_kind") == "semantic_phrase" and default_complete
        ):
            continue
        if intent_id == "TRANSFER_PREPARATION":
            continue
        if plan.get("required_slots") or plan.get("required_entities"):
            continue
        capability_id = intent_validation_capability(intent_id, registry)
        capability = (
            registry.get("capabilities", {}).get(capability_id, {}) if capability_id else {}
        )
        if capability.get("kind") != "mcp":
            continue
        if capability.get("authorization", {}).get("agent_invocable") is not True:
            continue
        if capability.get("execution", {}).get("effect") not in {"read", "read_only"}:
            continue
        if capability.get("input_schema", {}).get("required", []):
            continue
        eligible.append((candidate, intent))

    if not eligible:
        return None
    eligible.sort(key=lambda item: int(item[0].get("score", 0) or 0), reverse=True)
    if len(eligible) > 1:
        top_score = int(eligible[0][0].get("score", 0) or 0)
        second_score = int(eligible[1][0].get("score", 0) or 0)
        if top_score - second_score < 40:
            return None
    candidate, intent = eligible[0]
    response_mode = intent.get("execution_plan", {}).get("response_mode")
    requested_outcome = "EXPLANATION" if response_mode == "grounded_explanation" else "INFORMATION"
    return TurnInterpretationAssessment(
        resolution="RESOLVED",
        tasks=[
            InterpretedTask(
                intent_id=str(candidate["intent_id"]),
                requested_outcome=requested_outcome,
                resolution="READY",
                evidence=[
                    TaskEvidence(
                        field="intent", source="current_turn", quote=str(current_text)[:500]
                    )
                ],
                intent_confidence=0.97,
            )
        ],
    )


def task_capability_plan(
    intent_id: str, registry: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Return the registered plan for an intent, excluding model-supplied routing."""
    registry = registry or get_registry()
    intent = registry.get("intents", {}).get(intent_id)
    if not isinstance(intent, dict):
        return None
    plan = intent.get("execution_plan")
    if not isinstance(plan, dict):
        return None
    capability_id = intent_validation_capability(intent_id, registry)
    return {
        "intent_id": intent_id,
        "capability_id": capability_id,
        "capability": (
            registry.get("capabilities", {}).get(capability_id, {}) if capability_id else {}
        ),
        "execution_plan": plan,
        "description": str(intent.get("description", "")),
    }


def allowed_conversation_references(
    task: InterpretedTask, messages: list[Any] | tuple[Any, ...]
) -> bool:
    """References must resolve to message IDs in the supplied conversation."""
    known = {
        str(getattr(message, "id", ""))
        for message in messages or ()
        if getattr(message, "id", None)
    }
    return all(reference in known for reference in task.conversation_references)


def task_prompt_catalogue(registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Compact semantic index derived from the canonical OCCF registry.

    The registry stores customer-language examples under ``phrases`` (not
    ``examples``). Keep this adapter tolerant of either key for future schema
    evolution, but never silently drop registered examples.
    """
    registry = registry or get_registry()
    catalogue: list[dict[str, Any]] = []
    for intent_id, intent in registry.get("intents", {}).items():
        if not isinstance(intent, dict):
            continue
        plan = (
            intent.get("execution_plan", {})
            if isinstance(intent.get("execution_plan"), dict)
            else {}
        )
        discovery = (
            intent.get("semantic_discovery", {})
            if isinstance(intent.get("semantic_discovery"), dict)
            else {}
        )
        phrases = discovery.get("phrases", discovery.get("examples", []))
        keywords = discovery.get("keywords", [])
        recognition = (
            intent.get("recognition", {}) if isinstance(intent.get("recognition"), dict) else {}
        )
        aliases = (
            recognition.get("aliases", []) if isinstance(recognition.get("aliases"), list) else []
        )
        catalogue.append(
            {
                "intent_id": str(intent_id),
                "description": str(intent.get("description", ""))[:420],
                "response_mode": str(
                    plan.get("response_mode")
                    or ("presentation" if plan.get("presentation") else "read")
                ),
                "resource_id": str(plan.get("resource_id", "")) or None,
                "examples": (
                    [str(value)[:120] for value in phrases[:8]] if isinstance(phrases, list) else []
                ),
                "keywords": (
                    [str(value)[:80] for value in keywords[:12]]
                    if isinstance(keywords, list)
                    else []
                ),
                "required_slots": [
                    str(value) for value in plan.get("required_slots", []) if isinstance(value, str)
                ],
                "optional_slots": [
                    str(value) for value in plan.get("optional_slots", []) if isinstance(value, str)
                ],
                "slot_descriptions": {
                    str(key): str(value)[:180]
                    for key, value in plan.get("slot_descriptions", {}).items()
                    if isinstance(key, str) and isinstance(value, str)
                },
                "slot_values": {
                    str(key): [str(value) for value in values]
                    for key, values in plan.get("slot_values", {}).items()
                    if isinstance(key, str) and isinstance(values, list)
                },
                "required_entities": [
                    str(value)
                    for value in plan.get("required_entities", [])
                    if isinstance(value, str)
                ],
                "entity_descriptions": {
                    str(key): str(value)[:220]
                    for key, value in plan.get("entity_descriptions", {}).items()
                    if isinstance(key, str) and isinstance(value, str)
                },
                "requires_unique_contact": bool(plan.get("requires_unique_contact")),
                "aliases": [str(value)[:80] for value in aliases[:8]],
                # Publish semantic meaning to the interpreter, not backend
                # argument mappings or capability schemas. The planner retains
                # those execution details and compiles them after interpretation.
                "semantic_inputs": {
                    str(field_name): {
                        "location": str(spec.get("location", "slots")),
                        "description": str(spec.get("description", ""))[:190],
                        "allowed_values": [
                            str(value)
                            for value in spec.get("allowed_values", [])
                            if isinstance(value, str)
                        ],
                        "default": (
                            str(spec["default"]) if spec.get("default") is not None else None
                        ),
                        "default_policy": (
                            str(spec.get("default_policy"))
                            if spec.get("default_policy") is not None
                            else None
                        ),
                        "conditional_defaults": [
                            {
                                "when": {
                                    str(key): str(value)
                                    for key, value in rule.get("when", {}).items()
                                },
                                "value": str(rule.get("value")),
                                "description": str(rule.get("description", ""))[:180],
                            }
                            for rule in spec.get("conditional_defaults", [])
                            if isinstance(rule, dict) and isinstance(rule.get("when"), dict)
                        ],
                        "required": bool(spec.get("required", False)),
                    }
                    for field_name, spec in plan.get("semantic_inputs", {}).items()
                    if isinstance(field_name, str) and isinstance(spec, dict)
                },
                "outcome_semantics": str(plan.get("outcome_semantics", ""))[:520],
            }
        )
    return catalogue


def workspace_prompt_catalogue(
    registry: dict[str, Any] | None = None,
    *,
    query: str = "",
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Return turn-relevant descriptive workspace knowledge, never action schemas.

    Generic domain nouns (for example ``loan`` inside ``loan-to-equity``) are
    not treated as a workspace request. They must be framed as a page/tab or
    match a workspace-specific plural/name alias.
    """
    registry = registry or get_registry()
    workspaces = registry.get("application_policy", {}).get("workspaces", {})
    aliases = registry.get("application_policy", {}).get("navigation", {}).get("aliases", {})
    result = []
    normalised_query = " ".join(re.sub(r"[^a-z0-9]+", " ", str(query).casefold()).split())
    for workspace_id, value in workspaces.items() if isinstance(workspaces, dict) else []:
        if not isinstance(value, dict):
            continue
        names = [alias for alias, target in aliases.items() if target == workspace_id]
        matched_names = []
        for alias in names:
            normalised_alias = " ".join(re.sub(r"[^a-z0-9]+", " ", str(alias).casefold()).split())
            if not normalised_alias or f" {normalised_alias} " not in f" {normalised_query} ":
                continue
            # A single generic product noun is not enough to infer a question
            # about the workspace itself. Preserve explicit plural aliases and
            # aliases that are clearly page/workspace labels.
            if normalised_alias in {
                "account",
                "card",
                "payment",
                "transaction",
                "contact",
                "loan",
                "credit",
            }:
                framed = re.search(
                    rf"(?<![a-z0-9]){re.escape(normalised_alias)}\s+(?:tab|workspace|page|section)(?![a-z0-9])",
                    normalised_query,
                )
                if not framed:
                    continue
            matched_names.append(alias)
        if normalised_query and not matched_names:
            continue
        result.append(
            {
                "workspace_id": str(workspace_id),
                "aliases": names[:12],
                "purpose": str(value.get("purpose", ""))[:300],
                "value": str(value.get("value", ""))[:300],
                "features": [str(item)[:140] for item in value.get("features", [])[:8]],
                "limitations": [str(item)[:140] for item in value.get("limitations", [])[:4]],
            }
        )
    return result[: max(1, int(limit))]


def _semantic_tokens(value: str) -> set[str]:
    """Normalize content words for lightweight catalogue relevance scoring."""
    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "please",
        "the",
        "to",
        "what",
        "when",
        "which",
        "with",
        "you",
    }
    tokens = set(re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).split())
    # A small morphology normalization handles common plural forms without
    # introducing an external NLP dependency or a phrase-specific parser.
    normalized = set(tokens)
    for token in tokens:
        if len(token) > 4 and token.endswith("ies"):
            normalized.add(token[:-3] + "y")
        elif len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            normalized.add(token[:-1])
    return normalized - stopwords


def academy_prompt_catalogue(
    registry: dict[str, Any] | None = None,
    *,
    query: str = "",
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Return stable, relevant maintained learning topics for the current turn.

    Exact multi-word concepts outrank single-word overlaps. Stop words are
    excluded, so a transfer phrase such as "send 50 EUR to John" does not
    accidentally select a topic merely because its keywords contain "to".
    """
    registry = registry or get_registry()
    topics = registry.get("application_policy", {}).get("academy_topics", [])
    normalized_query = " ".join(re.sub(r"[^a-z0-9]+", " ", str(query).casefold()).split())
    padded_query = f" {normalized_query} "
    query_tokens = _semantic_tokens(query)
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for order, topic in enumerate(topics if isinstance(topics, list) else []):
        if not isinstance(topic, dict) or not topic.get("topic_id"):
            continue
        terms = [str(topic.get("title", "")), *[str(item) for item in topic.get("keywords", [])]]
        exact_score = 0
        token_matches: set[str] = set()
        for term in terms:
            normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", term.casefold()).split())
            if not normalized:
                continue
            term_tokens = _semantic_tokens(normalized)
            if len(term_tokens) >= 2 and f" {normalized} " in padded_query:
                exact_score += 6 + min(2, len(term_tokens))
            token_matches.update(term_tokens & query_tokens)
        # A single distinctive topic word (for example "savings" or
        # "instalments") may be enough to add relevant learning context, but
        # short generic words such as "loan" or "debt" cannot pull in a topic
        # on their own. Multi-word exact concepts always score higher.
        token_score = (
            len(token_matches) * 2
            if len(token_matches) >= 2
            else 3 if len(token_matches) == 1 and len(next(iter(token_matches))) >= 7 else 0
        )
        score = max(exact_score, token_score)
        if score <= 0:
            continue
        scored.append(
            (
                score,
                -order,
                {
                    "topic_id": str(topic["topic_id"]),
                    "title": str(topic.get("title", ""))[:160],
                    "description": str(topic.get("description", ""))[:260],
                    "starter_prompt": str(topic.get("starter_prompt", ""))[:220],
                    "keywords": [str(item)[:80] for item in topic.get("keywords", [])[:12]],
                    "customer_explanation": str(topic.get("customer_explanation", ""))[:420],
                    "personal_data_required": bool(topic.get("personal_data_intent_id")),
                },
            )
        )
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not scored:
        return []
    # Keep every topic with an exact multi-word concept match. Those matches
    # are independently meaningful in comparative questions even when one
    # concept is mentioned more often than another (credit score vs badge).
    exact_matches = [item for score, _order, item in scored if score >= 8]
    if exact_matches:
        return exact_matches[: max(1, int(limit))]
    best = scored[0][0]
    return [item for score, _order, item in scored if score >= max(3, best - 1)][
        : max(1, int(limit))
    ]


def rank_saved_contacts_for_context(
    contacts: list[dict[str, Any]] | None,
    customer_text: str,
    *,
    limit: int = 20,
) -> list[dict[str, str]]:
    """Prioritize customer-named saved contacts in the read-only LLM context.

    This does not resolve a contact, select an intent or authorize a search. It
    only prevents a long contact catalogue from being truncated before a name
    mentioned in the current utterance reaches the interpreter. Actual matching
    and disclosure still occur through the authenticated contacts capability.
    """
    stop = {
        "a",
        "an",
        "and",
        "are",
        "do",
        "find",
        "for",
        "from",
        "in",
        "is",
        "look",
        "me",
        "my",
        "of",
        "please",
        "saved",
        "search",
        "the",
        "to",
        "up",
        "contact",
        "contacts",
    }
    query = str(customer_text or "").casefold()
    query_tokens = {token for token in re.findall(r"[a-z0-9]+", query) if token not in stop}
    ranked: list[tuple[int, int, dict[str, str]]] = []
    for index, item in enumerate(contacts or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("full_name") or item.get("name") or "").strip()
        if not name:
            continue
        name_tokens = {token for token in re.findall(r"[a-z0-9]+", name.casefold()) if token}
        overlap = len(query_tokens & name_tokens)
        exact_name = int(bool(name.casefold() and name.casefold() in query))
        score = exact_name * 100 + overlap * 10
        ranked.append(
            (
                score,
                index,
                {
                    "name": name[:160],
                    "status": str(item.get("status") or "ACTIVE")[:24],
                },
            )
        )
    # Unrelated questions must not inherit an arbitrary prefix of the saved
    # contact catalogue. Besides wasting context tokens, this adds irrelevant
    # personal names to every interpretation request. Keep only contacts that
    # actually match words in the customer's current utterance.
    ranked = [item for item in ranked if item[0] > 0]
    ranked.sort(key=lambda value: (-value[0], value[1]))
    return [item for _score, _index, item in ranked[: max(1, min(40, int(limit)))]]


def normalise_task_payload(task: InterpretedTask) -> dict[str, Any]:
    """Convert the strict task to the established evidence-validation shape.

    Capability identity is intentionally added later by the runtime from OCCF.
    The returned dictionary does not contain any executable capability choice.
    """
    return {
        "intent_id": task.intent_id,
        "intent_confidence": task.intent_confidence,
        "slot_confidence": task.slot_confidence,
        "entity_confidence": task.entity_confidence,
        "capability_selection_confidence": None,
        "resolution": task.resolution,
        "slots": dict(task.slots),
        "entities": dict(task.entities),
        "evidence": [item.model_dump(mode="json") for item in task.evidence],
        "clarification_question": task.clarification_question,
    }


def build_assessment_prompt(
    *,
    intent_candidates: list[dict[str, Any]],
    catalogue: list[dict[str, Any]],
    conversation_excerpt: str = "",
    application_context: dict[str, Any] | None = None,
    workspace_catalogue: list[dict[str, Any]] | None = None,
    academy_catalogue: list[dict[str, Any]] | None = None,
    decision_map: dict[str, Any] | None = None,
) -> str:
    """Create the small, interpretation-only prompt for one customer turn."""
    import json as _json

    # The full decision map is useful to runtime diagnostics and policy tests,
    # but its capability projection is executable routing metadata. The model
    # needs intent meaning and resource descriptions only; the runtime alone
    # maps the validated intent to capability IDs, tool names and arguments.
    # Candidate discovery is advisory and may carry runtime-only capability
    # projections for diagnostics/turn contracts. Keep semantic hints, but never
    # leak capability identifiers, tool names or executable arguments to the
    # interpretation model through this secondary prompt channel.
    prompt_candidates = [
        {
            key: value
            for key, value in item.items()
            if key
            not in {"capability_id", "capability_name", "tool_name", "arguments", "input_schema"}
        }
        for item in (intent_candidates or [])
        if isinstance(item, dict)
    ]

    prompt_decision_map = None
    if isinstance(decision_map, dict):
        prompt_decision_map = dict(decision_map)
        # Route ordering and the capability projection are runtime execution
        # policy, not semantic interpretation input. Omitting them keeps the
        # prompt focused while the full immutable map remains in the runtime.
        prompt_decision_map.pop("policy", None)
        prompt_decision_map.pop("entry_sequence", None)
        prompt_decision_map.pop("capabilities", None)
        prompt_resources = dict(prompt_decision_map.get("resources") or {})
        assistant_capabilities = prompt_resources.get("assistant_capabilities")
        if isinstance(assistant_capabilities, list):
            prompt_resources["assistant_capabilities"] = [
                {key: value for key, value in item.items() if key != "capability_id"}
                for item in assistant_capabilities
                if isinstance(item, dict)
            ]
        prompt_decision_map["resources"] = prompt_resources

    return (
        "You are ORAIflow's semantic intent interpreter, not the customer-facing assistant. "
        "Return one structured oraiflow_assess_turn assessment. The runtime requires this structure; "
        "if the request is unclear, still return a valid assessment with a focused clarification. "
        "Never call or select MCP tools, frontend actions, renderers, APIs or capabilities. "
        "You identify customer intent only. The decision map describes registered intent semantics, "
        "application resources and workflow owners; do not ask the application which tools or skills exist. "
        "Choose a registered intent and express the customer's meaning. The runtime—not you—selects capabilities.\n\n"
        "Interpret meaning rather than requiring an exact registered sentence or matching a phrase list. Customers may use ordinary typos, "
        "missing punctuation, grammatical variation, plurals, shorthand or an unfamiliar way of describing a "
        "registered concept. Use the decision map's semantic descriptions, allowed values, defaults and outcome semantics "
        "to understand the task. Candidate hints and customer-language examples are supporting context, not an exhaustive "
        "phrase list, whitelist or substitute for reasoning. Generalize beyond the examples when the meaning is clear. "
        "When a likely spelling correction is strongly supported by the surrounding words, interpret the intended "
        "concept while preserving the customer's original wording in evidence. Never autocorrect names, account "
        "identifiers, references, dates or financial amounts into different values.\n\n"
        "Distinguish resource questions from execution requests: general financial education uses ACADEMY_EDUCATION; "
        "workspace purpose/explanation uses WORKSPACE_INFORMATION; assistant capability discovery uses "
        "ASSISTANT_CAPABILITY_HELP; opening a page uses WORKSPACE_NAVIGATION; personal financial facts use "
        "their registered read intent; transfer preparation uses TRANSFER_PREPARATION. For Academy education, "
        "identify the relevant registered topic_id when clear, but do not turn a general concept question into a "
        "personal-data read unless the customer asks about their own recorded position and the topic declares it. "
        "A request about what a savings account is useful for, emergency reserves, savings goals, liquidity or balancing savings with everyday spending is general ACADEMY_EDUCATION (topic SAVINGS_WITH_PURPOSE), not a backend banking lookup unless the customer explicitly asks for their own balance, rate or product terms.\n\n"
        "For transaction screening and analysis, use TRANSACTION_ANALYSIS for questions about the customer's own income, receipts, spending, merchants, averages, grouped activity or patterns. Interpret the objective rather than matching phrases: spending/purchases/costs are outflows; received money/pay/payroll/income are inflows. A salary question does NOT require the customer to name an employer before searching: inspect incoming transactions for the requested period and report only what their recorded descriptions and repeated history support. Never ask for an employer merely to begin. Use analysis_mode=recurring_inflows and flow=inflows for general salary/pay-pattern screening; when the customer specifies a period such as last month, preserve that exact period and return the matching incoming-payment evidence even if that short window cannot establish recurrence. When no period is specified, the registered conditional default supplies a 365-day screening window. Use merchant_query only when the customer explicitly names a merchant, payer or transaction description; a named spending type that matches a registered category belongs in category_query, not merchant_query. Questions such as whether the customer has any spending/expenses in a named category are transaction-analysis requests and normally use flow=outflows. A request for 'by month', 'by months', 'each month', 'every month', 'monthly', 'month by month' or 'month-by-month' is a month grouping request (group_by=month); unless the customer gives a different date range, the registered 90-day comparison default applies. Mean/average means measure=average; total/how much means sum; number means count; a requested breakdown fills group_by. Coffee/merchant spending without a monthly comparison or explicit period uses the registered ordinary-activity default; do not ask for a timeframe when the contract supplies one. Use only registered time-window values; the backend calculates local date boundaries. Do not classify one-off credits as salary, claim employer identity, or treat category-labelled transfers as earned income.\n\n"
        "Extract the customer's objective, subject, scope, selector, requested outcome and references separately. "
        "Respect clear semantic selectors and scope, including ordinary paraphrases not listed as examples. "
        "Use the semantic_inputs declared for the selected intent: canonicalize the customer's words to one of its "
        "allowed_values, and quote the exact customer text that supports each extracted semantic value. "
        "When a registered semantic input declares a default, do not ask the customer for that field when it is "
        "omitted or expressed indirectly; the planner applies the registered default and verifies the final contract. "
        "For example, a request to explain the latest, newest, most recent or just-made transaction means selector "
        "latest when that intent supports it. If that is the registered default, leave the optional selector absent or set it to latest; never ask the customer to identify the transaction again. "
        "Do not ask for a date, amount, merchant or transaction reference merely "
        "to select a latest-record explanation. Do not invent unsupported selector values.\n\n"
        "Interpret the entire relevant conversation, including short follow-ups and references. "
        "When READ-ONLY APPLICATION CONTEXT contains pending_tasks, treat each as an unfinished registered task, not as a new unrelated phrase. "
        "If the current customer message plausibly supplies one or more missing fields, continue that same intent and merge its previously interpreted slots/entities with the new values. "
        "Preserve prior values only when their original customer evidence is included in the conversation or pending-task evidence; cite that earlier customer wording with source=conversation. "
        "Use source=current_turn for newly supplied values. Do not turn a short continuation such as 'Lucas from Everyday account' into a new intent, and do not ask the customer to repeat amount/currency already provided. "
        "If the current message is unrelated, do not revive the pending task. "
        "The validated application context may include saved_contacts as name/status pairs: use it to resolve a clearly named saved-contact lookup or beneficiary, but never expose or invent contact IDs, phone numbers, bank details or account references. "
        "Use only intent_id values in the registered intent catalogue. One message may contain "
        "multiple independent tasks; return each independently (maximum five). Do not discard a "
        "supported task because another task is unclear.\n\n"
        "CRITICAL TASK-FORM RULE: when the current message plausibly expresses a registered intent, "
        "always return an InterpretedTask for that intent even when one or more required fields are "
        "missing. Do not put a task-specific missing-field question only in the top-level assessment "
        "with tasks=[]; that loses the task and prevents a coherent follow-up. Return the best-supported "
        "intent, every slot/entity already supplied, their exact evidence, requested_outcome and "
        "task resolution NEEDS_CLARIFICATION. The runtime planner will determine the actual missing "
        "fields, retain the task context and ask the customer for only what is still required. Use a "
        "top-level clarification with no tasks only when no registered intent is plausible or the "
        "customer's objective itself cannot be identified.\n\n"
        "Extract slots/entities only when supported by the current customer message, prior customer "
        "messages or verified application/capability evidence. Encode every slot/entity value as a string, "
        "including numeric amounts such as amount=50. If you extract any slots, provide slot_confidence; "
        "if you extract any entities, provide entity_confidence. Use null only when that dimension is not applicable. "
        "Assistant prose can resolve a clear "
        "non-financial reference, but cannot establish financial instructions or facts. Quote exact "
        "evidence. Use evidence source current_turn, conversation, assistant_context, "
        "application_context or capability_result. Conversation references must use exact message IDs "
        "from the supplied excerpt.\n\n"
        "Do not invent capability IDs, tool names or executable arguments. Do not treat confidence as authorization. "
        "Task resolution NEEDS_CLARIFICATION is a provisional assessment, not a command to stop the runtime: "
        "the planner will apply registered semantic defaults and check the actual OCCF requirements. Prefer extracting "
        "the best-supported meaning and setting READY when the registered contract appears satisfiable. Set "
        "NEEDS_CLARIFICATION only when a genuinely required semantic value is absent or the request is materially "
        "ambiguous; still return all supported slots/entities and evidence so the planner can determine what is missing. "
        "Never ask for optional information or a field with a registered default. If a request may be supported but cannot be resolved, do not mark "
        "OUT_OF_SCOPE; request clarification. Use OUT_OF_SCOPE only when no registered ORAIflow "
        "intent or application resource plausibly helps. Brief social messages may use SOCIAL. "
        "A broad check-in that does not name a financial area is not out of scope: ask a helpful choice "
        "between an overall recorded account overview, Financial Health, recent activity or cash-flow guidance "
        "rather than asking the customer to rephrase.\n\n"
        "Requested outcome values: INFORMATION, EXPLANATION, EDUCATION, NAVIGATION, ACTION, UNKNOWN.\n"
        "A request such as 'Find John in my saved contacts' is a complete SAVED_CONTACT_LOOKUP "
        "read-only request: select that intent, extract contact_name='John', and return READY. "
        "When John or another first name uniquely identifies a saved contact in application context, that is sufficient to search; do not request a surname, phone or email merely because the current turn omitted them. "
        "Do not ask what the customer wants to do with John, do not infer a transfer, and do not require "
        "an amount or currency. Ask for a name only when no person is identified. A transfer intent "
        "requires an actual transfer-preparation objective; merely finding or viewing a saved contact "
        "is not a transfer.\n\n"
        "For transfer preparation, preserve an explicitly stated amount, currency, beneficiary name, "
        "source account name or source-account preference as separate string slots/entities. "
        "A named account that matches an account in validated application context is source_account, not source_account_preference. "
        "Reserve source_account_preference for declared values such as highest_balance or lowest_balance only. "
        "If currency is omitted, do not invent one; the runtime may resolve it from the verified saved contact. "
        "For a request such as 'send 50 EUR to John', identify TRANSFER_PREPARATION, amount='50', "
        "currency='EUR', beneficiary='John'. For 'send 50 to John', omit currency but retain amount and beneficiary. "
        "Short follow-ups must use the pending workflow and prior validated slots, not start a new intent.\n\n"
        "For every model-proposed slot/entity, include a field-specific exact evidence quotation from the current turn "
        "or customer conversation. Semantic input values are bounded by the contract's allowed_values; the runtime "
        "checks that the evidence quotation is real and that the value is permitted. Contract defaults do not need to "
        "be emitted as model-extracted fields or fabricated as customer evidence.\n\n"
        "The assessment JSON shape is: {schema_version:'1.0.0', resolution:'RESOLVED', tasks:[{"
        "intent_id:'REGISTERED_INTENT_ID', requested_outcome:'INFORMATION', resolution:'READY', "
        "slots:{}, entities:{}, evidence:[{field:'intent',source:'current_turn',quote:'exact customer words'}], "
        "intent_confidence:0.9, slot_confidence:null, entity_confidence:null, clarification_question:null, "
        "conversation_references:[]}]}. Do not include capability_id, tool_name or arguments.\n\n"
        + (
            "\n\nAUTHORITATIVE ORAIflow DECISION MAP (use this once to understand the "
            "application's registered intent semantics, resources and workflow owners):\n"
            + _json.dumps(prompt_decision_map, ensure_ascii=False, separators=(",", ":"))
            if prompt_decision_map is not None
            else "\n\nREGISTERED INTENTS (semantic descriptions, phrases, keywords and required slots; "
            "the runtime owns capability mapping):\n"
            + _json.dumps(catalogue, ensure_ascii=False, separators=(",", ":"))
            + "\n\nREGISTERED WORKSPACES (explanatory application knowledge, not actions):\n"
            + _json.dumps(workspace_catalogue or [], ensure_ascii=False, separators=(",", ":"))
            + "\n\nACADEMY TOPICS (general learning knowledge; personal facts still require a registered read):\n"
            + _json.dumps(academy_catalogue or [], ensure_ascii=False, separators=(",", ":"))
        )
        + "\n\nCURRENT TURN CANDIDATES (ranked hints, not authority; an empty list does not mean unsupported):\n"
        + _json.dumps(prompt_candidates, ensure_ascii=False, separators=(",", ":"))
        + "\n\nREAD-ONLY APPLICATION CONTEXT (not authorization):\n"
        + _json.dumps(application_context or {}, ensure_ascii=False, separators=(",", ":"))[:8000]
        + "\n\nRECENT CONVERSATION:\n"
        + str(conversation_excerpt or "")[:8000]
        # The current message is sent separately as the HumanMessage. Do not
        # duplicate it in the system prompt or conversation excerpt.
    )
