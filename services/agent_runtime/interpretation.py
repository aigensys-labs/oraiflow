"""Provider-neutral interpretation validation and deterministic decision policy.

The current Agent Runtime prefers provider-native Pydantic/JSON-schema structured
output and retains ``oraiflow_assess_turn`` as a bounded compatibility transport.
This module validates the model's proposed meaning, evidence, confidence policy
and registered capability boundary. It does not execute capabilities or grant
authority. The ``orai_*`` projection below remains an internal compatibility
adapter into the existing OCCF validation/ToolNode path; it is not model-visible.
"""
from __future__ import annotations

import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, create_model

from shared.contracts.registry import (
    capability_by_name, get_registry, validate_result_envelope,
)

logger = logging.getLogger("oraiflow.agent_runtime.interpretation")


class EvidenceQuote(BaseModel):
    """Evidence excerpt tying an interpreted field to an authorized source of text."""

    model_config = ConfigDict(extra="ignore")

    field: str = Field(min_length=1, max_length=80)
    source: Literal["current_turn", "conversation", "assistant_context", "application_context", "capability_result"]
    quote: str = Field(min_length=1, max_length=500)


class InterpretationAssessment(BaseModel):
    """Canonical, provider-independent assessment validated by the runtime.

    Confidence scores are nullable in observe mode so a provider that omits a
    score does not make an otherwise grounded request unusable. Enforce mode
    requires all four scores. No score is fabricated or defaulted.
    """

    model_config = ConfigDict(extra="ignore")

    intent_id: str = Field(min_length=1, max_length=80)
    intent_confidence: float | None = Field(default=None, ge=0, le=1)
    slot_confidence: float | None = Field(default=None, ge=0, le=1)
    entity_confidence: float | None = Field(default=None, ge=0, le=1)
    capability_id: str = Field(min_length=1, max_length=120)
    capability_selection_confidence: float | None = Field(default=None, ge=0, le=1)
    resolution: Literal["READY", "NEEDS_CLARIFICATION", "ABSTAIN"] = "READY"
    slots: dict[str, str] = Field(default_factory=dict)
    entities: dict[str, str] = Field(default_factory=dict)
    evidence: list[EvidenceQuote] = Field(default_factory=list, max_length=40)
    clarification_question: str | None = Field(default=None, max_length=500)


# Tool-call providers, especially OpenAI-compatible local models, are markedly
# more reliable with a flat set of primitive parameters than with a nested
# Pydantic object containing $defs and arrays of nested objects. This is only a
# transport projection: the runtime reconstructs the same canonical assessment
# above before validation. OCCF capability input schemas are not changed.
_ASSESSMENT_TRANSPORT_PREFIX = "orai_"
_ASSESSMENT_TOOL_FIELDS: dict[str, tuple[Any, Any]] = {
    "orai_intent_id": (str | None, Field(default=None, description="Registered intent ID selected by the model.")),
    "orai_intent_confidence": (float | None, Field(default=None, description="Intent confidence from 0 to 1; omit only if the model cannot assess it.")),
    "orai_slot_confidence": (float | None, Field(default=None, description="Slot extraction confidence from 0 to 1.")),
    "orai_entity_confidence": (float | None, Field(default=None, description="Entity resolution confidence from 0 to 1.")),
    "orai_capability_selection_confidence": (float | None, Field(default=None, description="Registered capability selection confidence from 0 to 1.")),
    "orai_resolution": (Literal["READY", "NEEDS_CLARIFICATION", "ABSTAIN"], Field(default="READY", description="READY only when the proposed operation has enough information; otherwise NEEDS_CLARIFICATION or ABSTAIN.")),
    # Accept primitive JSON values at the provider boundary (models often emit
    # numeric amounts as JSON numbers). assessment_from_tool_arguments()
    # normalizes them to the canonical string-valued maps before validation.
    "orai_slots": (dict[str, Any], Field(default_factory=dict, description="Extracted named slot values, e.g. amount, currency, source_account_preference. Values may be JSON strings or numbers; the runtime normalizes them.")),
    "orai_entities": (dict[str, Any], Field(default_factory=dict, description="Resolved or unresolved entity labels, e.g. beneficiary or workspace. The runtime normalizes primitive values.")),
    "orai_evidence": (list[str], Field(default_factory=list, description="Evidence entries as plain strings: field|source|exact quote. Sources: current_turn, conversation, assistant_context, application_context, capability_result.")),
    "orai_clarification_question": (str | None, Field(default=None, description="Natural-language clarification question when resolution is NEEDS_CLARIFICATION.")),
}


def _assessment_transport_schema() -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, (annotation, field_info) in _ASSESSMENT_TOOL_FIELDS.items():
        schema = TypeAdapter(annotation).json_schema()
        schema["description"] = field_info.description or ""
        if name.endswith("_confidence"):
            schema["minimum"] = 0
            schema["maximum"] = 1
        if field_info.default_factory is not None:
            schema["default"] = [] if schema.get("type") == "array" else {}
        elif field_info.default is not ...:
            schema["default"] = field_info.default
        properties[name] = schema
        if field_info.is_required():
            required.append(name)
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def add_assessment_parameter(action: dict[str, Any]) -> dict[str, Any]:
    """Enrich a copied frontend action schema with flat assessment fields.

    The browser-facing contract remains unchanged. These fields exist only in
    the model-to-runtime tool-call contract and are stripped before the action
    is returned to the browser.
    """
    if not isinstance(action, dict):
        return action
    enriched = json.loads(json.dumps(action))
    parameters = enriched.get("parameters")
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        return enriched
    properties = parameters.setdefault("properties", {})
    if not isinstance(properties, dict):
        return enriched
    assessment_schema = _assessment_transport_schema()
    properties.update(assessment_schema["properties"])
    required = parameters.setdefault("required", [])
    for name in assessment_schema["required"]:
        if name not in required:
            required.append(name)
    return enriched


def with_required_interpretation_assessment(base_args_model: type[BaseModel], model_name: str) -> type[BaseModel]:
    """Add a flat provider-friendly assessment transport to a model-facing tool.

    This does not change the OCCF input model. The runtime later reconstructs
    and validates the universal InterpretationAssessment before execution.
    """
    return create_model(model_name, __base__=base_args_model, **_ASSESSMENT_TOOL_FIELDS)


def _parse_transport_evidence(raw: Any) -> list[dict[str, str]]:
    """Parse primitive evidence transport and drop malformed model annotations.

    Dropping a malformed quotation does not make a value trusted: the runtime
    still has to independently anchor every extracted value to an allowed source.
    """
    allowed_sources = {
        "current_turn", "conversation", "assistant_context",
        "application_context", "capability_result",
    }
    parsed: list[dict[str, str]] = []
    if isinstance(raw, dict):
        for field_name, quote in raw.items():
            if isinstance(quote, str) and quote.strip() and len(str(field_name)) <= 80:
                parsed.append({"field": str(field_name), "source": "current_turn", "quote": quote[:500]})
        return parsed
    if not isinstance(raw, list):
        return parsed
    for item in raw:
        candidate: dict[str, Any] | None = None
        if isinstance(item, str):
            parts = item.split("|", 2)
            if len(parts) == 3:
                field_name, source, quote = parts
                candidate = {"field": field_name.strip(), "source": source.strip(), "quote": quote.strip()}
        elif isinstance(item, dict):
            candidate = item
        if not candidate:
            continue
        field_name = candidate.get("field")
        source = candidate.get("source")
        quote = candidate.get("quote")
        if (isinstance(field_name, str) and 0 < len(field_name) <= 80
                and source in allowed_sources and isinstance(quote, str) and quote.strip()):
            parsed.append({"field": field_name, "source": source, "quote": quote[:500]})
    return parsed


def _canonical_intent_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    registry_intents = get_registry().get("intents", {})
    normalized = re.sub(r"[\s-]+", "_", value.strip()).casefold()
    return next((item for item in registry_intents if item.casefold() == normalized), value.strip())


def unique_intent_for_capability(capability_id: str) -> str | None:
    """Return an intent only when the registry declares exactly one relationship."""
    candidates = [
        intent_id for intent_id in get_registry().get("intents", {})
        if _intent_supports_capability(intent_id, capability_id)
    ]
    return candidates[0] if len(candidates) == 1 else None


def assessment_from_tool_arguments(
    raw_args: dict[str, Any], *, expected_capability_id: str, inferred_intent_id: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Reconstruct the canonical assessment from a flat tool-call payload.

    Legacy nested assessments remain accepted at the transport boundary for
    compatibility. New calls use flat `orai_*` fields; the registered tool name
    is authoritative for the capability ID.
    """
    if not isinstance(raw_args, dict):
        return None, {"code": "ASSESSMENT_ARGUMENTS_INVALID", "fields": ["arguments"]}
    legacy = raw_args.get("interpretation_assessment")
    if isinstance(legacy, str):
        try:
            legacy = json.loads(legacy)
        except (TypeError, ValueError):
            legacy = None
    if isinstance(legacy, dict):
        result = dict(legacy)
        result.setdefault("capability_id", expected_capability_id)
        return result, {"code": "LEGACY_NESTED_ASSESSMENT"}

    def take(*names: str, default: Any = None) -> Any:
        for name in names:
            if name in raw_args and raw_args[name] is not None:
                return raw_args[name]
        return default

    intent_id = take("orai_intent_id", "intent_id", default=inferred_intent_id)
    declared_capability = take("orai_capability_id", "capability_id", default=expected_capability_id)
    confidence_object = take("orai_confidence", "confidence", default={})
    confidence_object = confidence_object if isinstance(confidence_object, dict) else {}
    raw_scores = {
        "intent_confidence": take("orai_intent_confidence", "intent_confidence", default=confidence_object.get("intent")),
        "slot_confidence": take("orai_slot_confidence", "slot_confidence", default=confidence_object.get("slot", confidence_object.get("slots"))),
        "entity_confidence": take("orai_entity_confidence", "entity_confidence", default=confidence_object.get("entity", confidence_object.get("entities"))),
        "capability_selection_confidence": take(
            "orai_capability_selection_confidence", "capability_selection_confidence", "capability_confidence",
            default=confidence_object.get("capability", confidence_object.get("capability_selection")),
        ),
    }
    def string_map(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, str] = {}
        for key, item in value.items():
            if isinstance(item, (str, int, float)) and not isinstance(item, bool):
                result[str(key)] = str(item)
        return result

    resolution = take("orai_resolution", "resolution", default="READY")
    if isinstance(resolution, str):
        resolution = re.sub(r"[\s-]+", "_", resolution.strip().upper())
        if resolution in {"CLARIFY", "NOT_READY", "INCOMPLETE"}:
            resolution = "NEEDS_CLARIFICATION"
    assessment = {
        "intent_id": _canonical_intent_id(intent_id),
        **raw_scores,
        "capability_id": str(declared_capability or "").strip(),
        "resolution": resolution,
        "slots": string_map(take("orai_slots", "slots", default={})),
        "entities": string_map(take("orai_entities", "entities", default={})),
        "evidence": _parse_transport_evidence(take("orai_evidence", "evidence", default=[])),
        "clarification_question": take("orai_clarification_question", "clarification_question"),
    }
    missing = ["intent_id"] if not intent_id else []
    return assessment, {"code": "FLAT_ASSESSMENT_TRANSPORT", "fields": missing}


def strip_assessment_tool_arguments(raw_args: dict[str, Any]) -> dict[str, Any]:
    """Remove runtime-only assessment transport before capability validation."""
    return {
        key: value for key, value in raw_args.items()
        if key != "interpretation_assessment" and not key.startswith(_ASSESSMENT_TRANSPORT_PREFIX)
    }


THRESHOLD_ENV = {
    "intent": "ORAI_INTENT_CONFIDENCE_THRESHOLD",
    "slot": "ORAI_SLOT_CONFIDENCE_THRESHOLD",
    "entity": "ORAI_ENTITY_CONFIDENCE_THRESHOLD",
    "capability": "ORAI_CAPABILITY_CONFIDENCE_THRESHOLD",
}


class AssessmentConfigurationError(ValueError):
    """Raised when required interpretation-assessment configuration is invalid."""

    pass


def _normalise_evidence_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def confidence_gate_configuration(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Return confidence-gate configuration without inventing thresholds.

    `observe` is the safe calibration mode: scores are recorded and structural /
    evidence checks remain mandatory, but uncalibrated self-scores do not block
    ordinary agent work. `enforce` requires all four explicit thresholds.
    """
    source = env if env is not None else os.environ
    mode = str(source.get("ORAI_CONFIDENCE_GATE_MODE", "observe")).strip().lower()
    if mode not in {"observe", "enforce"}:
        return {
            "mode": mode,
            "ready": False,
            "valid": False,
            "thresholds": {},
            "missing": list(THRESHOLD_ENV.values()),
            "error": "ORAI_CONFIDENCE_GATE_MODE must be observe or enforce",
        }

    thresholds: dict[str, float] = {}
    missing: list[str] = []
    invalid: list[str] = []
    for dimension, env_name in THRESHOLD_ENV.items():
        raw = str(source.get(env_name, "")).strip()
        if not raw:
            missing.append(env_name)
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            invalid.append(env_name)
            continue
        if not 0 <= value <= 1:
            invalid.append(env_name)
            continue
        thresholds[dimension] = value

    all_present = not missing and not invalid and len(thresholds) == 4
    valid = mode == "observe" or (mode == "enforce" and all_present)
    return {
        "mode": mode,
        "ready": all_present,
        "valid": valid,
        "thresholds": thresholds,
        "missing": missing,
        "invalid": invalid,
        "error": None if valid else "Enforce mode requires all four valid confidence thresholds",
    }


def _intent_supports_capability(intent_id: str, capability_id: str) -> bool:
    registry = get_registry()
    intent = registry.get("intents", {}).get(intent_id)
    capability = registry.get("capabilities", {}).get(capability_id)
    if not intent or not capability:
        return False
    plan = intent.get("execution_plan", {})
    allowed = {
        plan.get("capability"),
        plan.get("contact_resolution"),
        *(plan.get("supporting_capabilities") or []),
    }
    return capability_id in allowed


def _evidence_is_grounded(evidence: EvidenceQuote, sources: dict[str, str]) -> bool:
    haystack = _normalise_evidence_text(sources.get(evidence.source, ""))
    needle = _normalise_evidence_text(evidence.quote)
    return bool(haystack and needle and needle in haystack)


def _value_is_supported(value: str, quote: str) -> bool:
    """Check direct textual support without confusing prefixes or numeric substrings."""
    normalized_value = _normalise_evidence_text(value)
    normalized_quote = _normalise_evidence_text(quote)
    if not normalized_value or not normalized_quote:
        return False
    if re.fullmatch(r"\d+(?:[.,]\d+)?", normalized_value):
        return re.search(rf"(?<![\d.,]){re.escape(normalized_value)}(?![\d.,])", normalized_quote) is not None
    return normalized_value in normalized_quote


_CUSTOMER_SOURCES = {"current_turn", "conversation"}
_TRUSTED_RECORD_SOURCES = {"application_context", "capability_result"}
_UI_REFERENCE_SOURCES = _CUSTOMER_SOURCES | {"assistant_context"}
_FINANCIAL_VALUE_FIELDS = {
    "amount", "amount_minor", "currency", "beneficiary", "recipient", "name_query",
    "merchant", "merchant_query", "date", "from_date", "to_date",
}
_TRUSTED_IDENTIFIER_FIELDS = {
    "account_id", "source_account_id", "beneficiary_contact_id", "contact_id",
    "transaction_id", "transaction_reference", "record_id",
}
_SEMANTIC_ENUMS = {
    "source_account_preference": {"highest_balance", "lowest_balance"},
    "transaction_selector": {"latest", "last", "last_item", "first", "first_item", "previous", "previous_item"},
    "balance_scope": {"total", "available", "all_accounts", "selected_account", "current_account"},
    "account_scope": {"all_accounts", "selected_account", "current_account"},
}
_CURRENCY_ALIASES = {
    "EUR": {"eur", "€", "euro", "euros"},
    "USD": {"usd", "$", "us dollar", "us dollars", "dollar", "dollars"},
    "GBP": {"gbp", "£", "pound", "pounds", "pound sterling"},
}
_ACCOUNT_NAME_FIELDS = {"account", "account_name", "source_account", "source_account_name"}
_WORKSPACE_FIELDS = {"workspace", "workspace_name", "navigation_target"}


def _source_allowed_for_field(
    field_name: str,
    source: str,
    semantic_inputs: dict[str, Any] | None = None,
) -> bool:
    field = field_name.casefold()
    if field in (semantic_inputs or {}):
        # Semantic enums express the model's interpretation of customer
        # language. They must be quoted from customer-authored text, but are
        # not reinterpreted by a phrase/regex catalogue. The OCCF contract
        # constrains the permitted values; the planner decides whether the
        # resulting task can be compiled and safely executed.
        return source in _CUSTOMER_SOURCES
    if field in _FINANCIAL_VALUE_FIELDS:
        return source in _CUSTOMER_SOURCES
    if field in _TRUSTED_IDENTIFIER_FIELDS:
        return source in _TRUSTED_RECORD_SOURCES
    if field in _WORKSPACE_FIELDS:
        return source in _UI_REFERENCE_SOURCES
    if field in _SEMANTIC_ENUMS:
        return source in _CUSTOMER_SOURCES
    if field in _ACCOUNT_NAME_FIELDS:
        return source in (_CUSTOMER_SOURCES | _TRUSTED_RECORD_SOURCES)
    # Assistant prose is never a source of financial values or instructions.
    return source in (_CUSTOMER_SOURCES | _TRUSTED_RECORD_SOURCES)


def _json_context(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _catalogue_names(sources: dict[str, str], *, key: str) -> list[str]:
    """Extract canonical labels from already validated context/result JSON."""
    names: list[str] = []
    for source_name in ("application_context", "capability_result"):
        parsed = _json_context(sources.get(source_name, ""))
        if isinstance(parsed, dict):
            stack = [parsed]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    value = node.get(key)
                    if isinstance(value, str) and value.strip():
                        names.append(value.strip())
                    stack.extend(item for item in node.values() if isinstance(item, (dict, list)))
                elif isinstance(node, list):
                    stack.extend(item for item in node if isinstance(item, (dict, list)))
    return list(dict.fromkeys(names))


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", _normalise_evidence_text(value)) if token not in {"the", "a", "an", "my", "me", "to", "from", "account", "workspace", "please", "show", "open", "go", "take", "there"}}


def _workspace_value_supported(value: str, quote: str) -> bool:
    pages = get_registry().get("application_policy", {}).get("navigation", {}).get("pages", [])
    canonical = next((str(page) for page in pages if _normalise_evidence_text(page) == _normalise_evidence_text(value)), None)
    if not canonical:
        return False
    quote_tokens = _tokens(quote)
    if not quote_tokens:
        return False
    # Exact canonical labels are unambiguous. A short alias is accepted only
    # when it identifies exactly one registered page, never by first-match.
    if _normalise_evidence_text(canonical) in _normalise_evidence_text(quote):
        return True
    matches = [
        str(page) for page in pages
        if quote_tokens <= _tokens(str(page))
    ]
    return len(matches) == 1 and _normalise_evidence_text(matches[0]) == _normalise_evidence_text(canonical)


def _reference_language_present(text: str, *, kind: str) -> bool:
    normalized = _normalise_evidence_text(text)
    if kind == "workspace":
        return bool(re.search(
            r"\b(?:there|here|it|that|that workspace|that page|the workspace|the page)\b",
            normalized,
        ))
    if kind == "transaction":
        return bool(re.search(
            r"\b(?:last|latest|most recent|previous|prior|final|first|latter|former|bottom)\b",
            normalized,
        ))
    return False


def _semantic_enum_supported(field_name: str, value: str, quote: str) -> bool:
    """Constrain semantic enum outputs to language expressing the same concept."""
    text = _normalise_evidence_text(quote)
    if field_name == "source_account_preference":
        high = bool(re.search(r"\b(?:highest|higher|largest|biggest|maximum|max|most)\b", text))
        low = bool(re.search(r"\b(?:lowest|lower|smallest|smaller|least|minimum|min)\b", text))
        money = bool(re.search(r"\b(?:balance|amount|money|funds|value)\b", text))
        return money and ((value == "highest_balance" and high and not low)
                          or (value == "lowest_balance" and low and not high))
    if field_name == "transaction_selector":
        if value in {"latest", "last"}:
            return bool(re.search(r"\b(?:latest|recent|last|most recent)\b", text))
        if value in {"last_item", "previous_item"}:
            return _reference_language_present(text, kind="transaction") and bool(
                re.search(r"\b(?:last|previous|prior|final|latter|bottom)\b", text)
            )
        if value in {"first", "first_item"}:
            return bool(re.search(r"\b(?:first|earliest|top|former)\b", text))
        return False
    if field_name == "balance_scope":
        if value == "total":
            return bool(re.search(r"\b(?:sum|total|overall|combined|altogether)\b", text))
        if value == "available":
            return bool(re.search(r"\b(?:available|spendable)\b", text))
        if value in {"all_accounts", "current_account", "selected_account"}:
            return _semantic_enum_supported("account_scope", value, quote)
    if field_name == "account_scope":
        if value == "all_accounts":
            return bool(re.search(r"\b(?:all|every|across)\b.*\baccounts?\b|\ball accounts?\b", text))
        if value in {"selected_account", "current_account"}:
            return bool(re.search(r"\b(?:selected|current|this|that)\b.*\baccount\b", text))
    return False


def _account_alias_supported(value: str, quote: str, sources: dict[str, str]) -> bool:
    catalogue = _catalogue_names(sources, key="account_name")
    canonical = next((name for name in catalogue if _normalise_evidence_text(name) == _normalise_evidence_text(value)), None)
    if not canonical:
        return False
    quote_tokens = _tokens(quote)
    account_tokens = _tokens(canonical)
    # The customer's quote may include the complete transfer request; require
    # every meaningful canonical account token to appear in that quote. The
    # previous inverse subset check only accepted a quote consisting entirely
    # of account-name tokens and therefore rejected natural requests such as
    # "Send 100 to Lucas from everyday".
    return bool(account_tokens and account_tokens <= quote_tokens)


def _decimal_value_supported(value: str, quote: str) -> bool:
    try:
        target = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return False
    for token in re.findall(r"(?<![\d.,])\d+(?:[.,]\d{1,2})?(?![\d.,])", str(quote)):
        try:
            if Decimal(token.replace(",", ".")) == target:
                return True
        except InvalidOperation:
            continue
    return False


def _registered_semantic_value_for_quote(spec: dict[str, Any], quote: str) -> str | None:
    """Return a unique longest OCCF value evidenced by this exact quotation."""
    phrase_map = spec.get("evidence_phrases", {})
    if not isinstance(phrase_map, dict) or not phrase_map:
        return None
    matches: list[tuple[int, int, str]] = []
    for candidate_value, phrases in phrase_map.items():
        if not isinstance(phrases, list):
            continue
        for phrase in phrases:
            phrase = str(phrase or "").strip()
            if not phrase:
                continue
            words = re.split(r"\s+", phrase)
            pattern = r"(?<!\w)" + r"\s+".join(re.escape(word) for word in words) + r"(?!\w)"
            match = re.search(pattern, quote, flags=re.IGNORECASE)
            if match:
                matches.append((len(words), len(phrase), str(candidate_value)))
    if not matches:
        return None
    best_rank = max((item[0], item[1]) for item in matches)
    values = {item[2] for item in matches if (item[0], item[1]) == best_rank}
    return next(iter(values)) if len(values) == 1 else None


def _field_value_supported(
    field_name: str,
    value: str,
    evidence_items: list[EvidenceQuote],
    sources: dict[str, str],
    semantic_inputs: dict[str, Any] | None = None,
) -> bool:
    """Validate a value according to its provenance role, not only substring equality.

    Exact quotes are always required. Interpretation may be semantic for bounded
    enums and allowlisted UI/account labels, but assistant prose cannot ground
    amounts, currency, beneficiaries or financial identifiers.
    """
    field = field_name.casefold()
    semantic_spec = (semantic_inputs or {}).get(field)
    allowed_items = [
        item for item in evidence_items
        if _source_allowed_for_field(field, item.source, semantic_inputs)
    ]
    if not allowed_items:
        return False

    if isinstance(semantic_spec, dict):
        # Bounded values must be not only allowed but supported by a phrase
        # declared for that exact value in the OCCF contract. A valid quotation
        # about spending cannot be used as evidence for an inflow selector.
        allowed_values = semantic_spec.get("allowed_values", [])
        if value not in allowed_values:
            return False
        customer_quotes = [
            item.quote for item in allowed_items
            if item.source in _CUSTOMER_SOURCES and item.quote.strip()
        ]
        phrase_map = semantic_spec.get("evidence_phrases", {})
        if isinstance(phrase_map, dict) and phrase_map:
            return any(
                _registered_semantic_value_for_quote(semantic_spec, quote) == value
                for quote in customer_quotes
            )
        # Backward-compatible behavior for older semantic fields which have
        # not yet declared registry-owned value evidence phrases.
        return bool(customer_quotes)

    if field == "amount":
        return any(_decimal_value_supported(value, item.quote) for item in allowed_items)

    if field in _SEMANTIC_ENUMS:
        if value not in _SEMANTIC_ENUMS[field]:
            return False
        customer_quotes = [item.quote for item in allowed_items if item.source in _CUSTOMER_SOURCES]
        if not any(_semantic_enum_supported(field, value, quote) for quote in customer_quotes):
            return False
        if field == "transaction_selector" and value in {"last_item", "previous_item", "first_item"}:
            # Referential transaction choices need both a customer reference
            # and a validated prior MCP result containing the referenced item.
            return any(item.source == "capability_result" for item in evidence_items)
        return True

    if field == "currency":
        aliases = _CURRENCY_ALIASES.get(value.upper(), {value.casefold()})
        return any(
            _value_is_supported(value, item.quote)
            or _normalise_evidence_text(item.quote) in aliases
            or any(alias in _normalise_evidence_text(item.quote) for alias in aliases)
            for item in allowed_items
        )

    if field in _WORKSPACE_FIELDS:
        current_turn = sources.get("current_turn", "")
        for item in allowed_items:
            if item.source == "assistant_context":
                # Assistant context may resolve "there/it/that workspace", but
                # only when the customer actually makes a deictic reference.
                if not _reference_language_present(current_turn, kind="workspace"):
                    continue
                if _workspace_value_supported(value, item.quote):
                    return True
            elif _value_is_supported(value, item.quote) or _workspace_value_supported(value, item.quote):
                return True
        return False

    if field in _ACCOUNT_NAME_FIELDS:
        return any(
            _value_is_supported(value, item.quote)
            or (item.source in _CUSTOMER_SOURCES and _account_alias_supported(value, item.quote, sources))
            for item in allowed_items
        )

    if field in _TRUSTED_IDENTIFIER_FIELDS:
        # IDs must originate in trusted context or successful capability data;
        # repeating an ID in customer or assistant prose is not sufficient.
        return any(
            item.source in _TRUSTED_RECORD_SOURCES and _value_is_supported(value, item.quote)
            for item in allowed_items
        )

    return any(_value_is_supported(value, item.quote) for item in allowed_items)

def _quote_for_supported_value(value: str, source_text: str) -> str | None:
    """Return an exact source span supporting a value, without inventing text."""
    if not value or not source_text or not _value_is_supported(value, source_text):
        return None
    match = re.search(re.escape(value), source_text, flags=re.IGNORECASE)
    if match:
        start = max(0, match.start() - 80)
        end = min(len(source_text), match.end() + 80)
        return source_text[start:end][:500]
    # Normalized matching covers whitespace and non-breaking-space variants;
    # in that case retain an exact source prefix as the auditable quote.
    return source_text[:500]


def _anchor_field_evidence(
    field_name: str,
    value: str,
    sources: dict[str, str],
    semantic_inputs: dict[str, Any] | None = None,
) -> list[EvidenceQuote]:
    field = field_name.casefold()
    anchored: list[EvidenceQuote] = []
    if field in (semantic_inputs or {}):
        # Do not manufacture a semantic interpretation or attach a generic
        # whole-turn quote to it. The model must provide a field-specific
        # exact customer quotation for every semantic value it proposes.
        return anchored
    source_priority = ["current_turn", "conversation", "application_context", "capability_result", "assistant_context"]

    if field == "currency":
        aliases = _CURRENCY_ALIASES.get(value.upper(), {value.casefold()})
        for source_name in ("current_turn", "conversation"):
            source_text = sources.get(source_name, "")
            normalized = _normalise_evidence_text(source_text)
            if source_text and any(alias in normalized for alias in aliases):
                return [EvidenceQuote(field=field_name, source=source_name, quote=source_text[:500])]

    if field == "amount":
        for source_name in ("current_turn", "conversation"):
            source_text = sources.get(source_name, "")
            if source_text and _decimal_value_supported(value, source_text):
                return [EvidenceQuote(field=field_name, source=source_name, quote=source_text[:500])]

    if field in _SEMANTIC_ENUMS:
        for source_name in ("current_turn", "conversation"):
            quote = sources.get(source_name, "")
            if quote and _semantic_enum_supported(field, value, quote):
                anchored.append(EvidenceQuote(field=field_name, source=source_name, quote=quote[:500]))
                if field == "transaction_selector" and sources.get("capability_result"):
                    anchored.append(EvidenceQuote(
                        field=field_name, source="capability_result",
                        quote=sources["capability_result"][:500],
                    ))
                return anchored

    if field in _WORKSPACE_FIELDS:
        current_turn = sources.get("current_turn", "")
        if _reference_language_present(current_turn, kind="workspace"):
            assistant_quote = sources.get("assistant_context", "")
            if assistant_quote and _workspace_value_supported(value, assistant_quote):
                return [EvidenceQuote(
                    field=field_name, source="assistant_context", quote=assistant_quote[:500],
                )]

    for source_name in source_priority:
        if not _source_allowed_for_field(field, source_name, semantic_inputs):
            continue
        source_text = sources.get(source_name, "")
        if not source_text:
            continue
        quote = _quote_for_supported_value(value, source_text)
        if quote:
            anchored.append(EvidenceQuote(field=field_name, source=source_name, quote=quote))
            return anchored
        if field in _ACCOUNT_NAME_FIELDS and source_name in _CUSTOMER_SOURCES:
            if _account_alias_supported(value, source_text, sources):
                anchored.append(EvidenceQuote(field=field_name, source=source_name, quote=source_text[:500]))
                return anchored
        if field in _WORKSPACE_FIELDS and source_name in _CUSTOMER_SOURCES:
            if _workspace_value_supported(value, source_text):
                anchored.append(EvidenceQuote(field=field_name, source=source_name, quote=source_text[:500]))
                return anchored
    return anchored


def anchor_assessment_evidence(
    assessment: InterpretationAssessment,
    sources: dict[str, str],
    semantic_inputs: dict[str, Any] | None = None,
) -> tuple[InterpretationAssessment, dict[str, Any]]:
    """Attach provenance from exact trusted source spans after model interpretation.

    The model proposes semantic values; the runtime anchors those values to
    source text/records. Assistant prose is considered only for non-financial
    deictic UI references. Unverifiable values remain ungrounded and fail the
    deterministic gate.
    """
    valid_items: list[EvidenceQuote] = []
    rejected_model_items = 0
    for item in assessment.evidence:
        if _evidence_is_grounded(item, sources):
            valid_items.append(item)
        else:
            rejected_model_items += 1

    by_field: dict[str, list[EvidenceQuote]] = {}
    for item in valid_items:
        by_field.setdefault(item.field, []).append(item)

    # The current customer turn is deterministically the source of the action
    # proposal. This records provenance; it does not infer an intent or value.
    if not any(item.field == "intent" and item.source in _CUSTOMER_SOURCES for item in valid_items):
        current_turn = sources.get("current_turn", "")
        if current_turn:
            valid_items.append(EvidenceQuote(field="intent", source="current_turn", quote=current_turn[:500]))

    for field_name, value in {**assessment.slots, **assessment.entities}.items():
        existing = by_field.get(field_name, [])
        if any(
            _source_allowed_for_field(field_name, item.source, semantic_inputs)
            and _field_value_supported(field_name, value, [item], sources, semantic_inputs)
            for item in existing
        ):
            continue
        additions = _anchor_field_evidence(field_name, value, sources, semantic_inputs)
        for item in additions:
            if item not in valid_items:
                valid_items.append(item)

    # A relative transaction selector must be backed by both the customer's
    # reference and a previously validated capability result. The semantic
    # anchor helper adds that second source when one is available.
    anchored = assessment.model_copy(update={"evidence": valid_items})
    return anchored, {
        "model_evidence_rejected": rejected_model_items,
        "evidence_items": len(valid_items),
    }


def validate_interpretation_assessment(
    raw: Any,
    *,
    expected_capability_id: str,
    sources: dict[str, str],
    applicable_confidence_dimensions: set[str] | None = None,
    allow_provisional_clarification: bool = False,
) -> tuple[InterpretationAssessment | None, dict[str, Any]]:
    """Validate schema, registry relationship and source-grounded evidence.

    Numeric confidence thresholds are applied only in explicit `enforce` mode
    after calibration. Structural, registry and evidence checks always apply.
    """
    try:
        assessment = raw if isinstance(raw, InterpretationAssessment) else InterpretationAssessment.model_validate(raw)
    except (ValidationError, TypeError, ValueError) as exc:
        issues: list[str] = []
        if isinstance(exc, ValidationError):
            for item in exc.errors(include_url=False)[:12]:
                path = ".".join(str(part) for part in item.get("loc", ())) or "assessment"
                issue_type = str(item.get("type", "invalid"))
                issues.append(f"{path}:{issue_type}")
        return None, {
            "decision": "CLARIFY", "code": "ASSESSMENT_SCHEMA_INVALID",
            "detail": type(exc).__name__, "issues": issues,
        }

    registry = get_registry()
    intent_contract = registry.get("intents", {}).get(assessment.intent_id, {})
    execution_plan = intent_contract.get("execution_plan", {}) if isinstance(intent_contract, dict) else {}
    semantic_inputs = execution_plan.get("semantic_inputs", {}) if isinstance(execution_plan, dict) else {}
    semantic_inputs = semantic_inputs if isinstance(semantic_inputs, dict) else {}
    assessment, evidence_anchor_info = anchor_assessment_evidence(
        assessment, sources, semantic_inputs=semantic_inputs,
    )
    if assessment.intent_id not in registry.get("intents", {}):
        return assessment, {"decision": "REJECT", "code": "UNKNOWN_INTENT"}
    if assessment.capability_id not in registry.get("capabilities", {}):
        return assessment, {"decision": "REJECT", "code": "UNKNOWN_CAPABILITY"}
    if assessment.capability_id != expected_capability_id:
        return assessment, {"decision": "REJECT", "code": "CAPABILITY_ASSESSMENT_MISMATCH"}
    if not _intent_supports_capability(assessment.intent_id, assessment.capability_id):
        return assessment, {"decision": "REJECT", "code": "INTENT_CAPABILITY_MISMATCH"}

    if assessment.resolution == "ABSTAIN":
        return assessment, {"decision": "ABSTAIN", "code": "MODEL_ABSTAIN"}
    if assessment.resolution == "NEEDS_CLARIFICATION" and not allow_provisional_clarification:
        return assessment, {"decision": "CLARIFY", "code": "MODEL_REQUESTED_CLARIFICATION"}

    grounded: dict[str, list[EvidenceQuote]] = {}
    invalid_evidence: list[str] = []
    for item in assessment.evidence:
        if not _evidence_is_grounded(item, sources):
            invalid_evidence.append(item.field)
            continue
        grounded.setdefault(item.field, []).append(item)
    if invalid_evidence:
        return assessment, {"decision": "CLARIFY", "code": "EVIDENCE_NOT_IN_SOURCE", "fields": sorted(set(invalid_evidence))}

    # The customer instruction must always be grounded in customer-authored
    # text. Assistant text is separately available for resolving deictic UI
    # references (for example, "take me there" after a navigation offer), but
    # it cannot establish a financial instruction or financial fact.
    intent_evidence = grounded.get("intent", [])
    if not any(item.source in _CUSTOMER_SOURCES for item in intent_evidence):
        return assessment, {"decision": "CLARIFY", "code": "CUSTOMER_INSTRUCTION_EVIDENCE_MISSING"}

    unsupported: list[str] = []
    unsupported_semantic_values: list[str] = []
    for field_name, value in {**assessment.slots, **assessment.entities}.items():
        candidates = grounded.get(field_name, [])
        semantic_spec = semantic_inputs.get(field_name)
        if candidates and isinstance(semantic_spec, dict) and value not in semantic_spec.get("allowed_values", []):
            unsupported_semantic_values.append(field_name)
            continue
        if not candidates or not _field_value_supported(
            field_name, value, candidates, sources, semantic_inputs,
        ):
            unsupported.append(field_name)
    if unsupported_semantic_values:
        return assessment, {
            "decision": "CLARIFY", "code": "INTENT_SEMANTIC_VALUE_UNSUPPORTED",
            "fields": sorted(set(unsupported_semantic_values)),
        }
    if unsupported:
        return assessment, {"decision": "CLARIFY", "code": "VALUE_EVIDENCE_MISSING", "fields": sorted(set(unsupported))}

    preference = assessment.slots.get("source_account_preference")
    if preference is not None and preference not in {"highest_balance", "lowest_balance"}:
        return assessment, {"decision": "CLARIFY", "code": "SOURCE_ACCOUNT_PREFERENCE_INVALID"}

    if expected_capability_id == "banking.contacts.search":
        if assessment.intent_id == "SAVED_CONTACT_LOOKUP":
            contact_name = assessment.entities.get("contact_name") or assessment.slots.get("contact_name")
            if not contact_name:
                return assessment, {"decision": "CLARIFY", "code": "CONTACT_NAME_NOT_EXTRACTED"}
        else:
            beneficiary = assessment.entities.get("beneficiary") or assessment.slots.get("beneficiary") or assessment.slots.get("recipient")
            if not beneficiary:
                return assessment, {"decision": "CLARIFY", "code": "BENEFICIARY_NOT_EXTRACTED"}
            amount_value = assessment.slots.get("amount")
            if amount_value is not None:
                try:
                    if Decimal(amount_value.replace(",", ".")) <= 0:
                        return assessment, {"decision": "CLARIFY", "code": "AMOUNT_NOT_POSITIVE"}
                except InvalidOperation:
                    return assessment, {"decision": "CLARIFY", "code": "AMOUNT_INVALID"}
            currency = assessment.slots.get("currency")
            if currency and currency.upper() not in {"EUR", "USD", "GBP"}:
                return assessment, {"decision": "CLARIFY", "code": "CURRENCY_UNSUPPORTED"}

    configuration = confidence_gate_configuration()
    if not configuration["valid"]:
        return assessment, {"decision": "REJECT", "code": "CONFIDENCE_GATE_CONFIGURATION_INVALID"}
    scores = {
        "intent": assessment.intent_confidence,
        "slot": assessment.slot_confidence,
        "entity": assessment.entity_confidence,
        "capability": assessment.capability_selection_confidence,
    }
    applicable = set(applicable_confidence_dimensions or scores.keys())
    unknown_dimensions = applicable - set(scores)
    if unknown_dimensions:
        return assessment, {"decision": "REJECT", "code": "CONFIDENCE_DIMENSION_UNKNOWN"}
    missing_scores = [name for name, score in scores.items() if name in applicable and score is None]
    # Contact search is candidate discovery. Final entity confidence is
    # evaluated only after the runtime has authenticated candidate records.
    deferred_dimensions = (
        {"entity"} if expected_capability_id == "banking.contacts.search"
        and assessment.intent_id == "TRANSFER_PREPARATION" else set()
    )
    if configuration["mode"] == "enforce":
        required_missing = [name for name in missing_scores if name not in deferred_dimensions]
        if required_missing:
            return assessment, {
                "decision": "CLARIFY", "code": "CONFIDENCE_MISSING",
                "dimensions": required_missing,
            }
        below = [
            name for name, score in scores.items()
            if name in applicable and name not in deferred_dimensions and score is not None
            and score < configuration["thresholds"][name]
        ]
        if below:
            return assessment, {"decision": "CLARIFY", "code": "CONFIDENCE_BELOW_THRESHOLD", "dimensions": below}
    else:
        deferred_dimensions = set()

    return assessment, {
        "decision": "PASS",
        "code": "ASSESSMENT_VALIDATED",
        "confidence_mode": configuration["mode"],
        "deferred_dimensions": sorted(deferred_dimensions),
        "missing_dimensions": missing_scores,
        "evidence_anchor": evidence_anchor_info,
        "scores": scores,
    }


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
        return "\n".join(chunks).strip()
    return ""


def assessment_sources(messages: list[Any], current_text: str, application_context: dict[str, Any]) -> dict[str, str]:
    """Build role-separated, provenance-aware interpretation evidence sources.

    Assistant messages support conversational/UI reference resolution only.
    Tool evidence is included only after the OCCF result envelope validates as a
    successful, partial or empty result; raw/unvalidated tool text is excluded.
    """
    human_texts: list[str] = []
    assistant_texts: list[str] = []
    tool_texts: list[str] = []
    for message in messages or []:
        message_type = getattr(message, "type", "")
        content = _message_text(getattr(message, "content", ""))
        if not content:
            continue
        if message_type == "human":
            human_texts.append(content[:4000])
        elif message_type == "ai":
            # No tool-call arguments or model metadata are admitted as assistant
            # context; only customer-visible prose can resolve references.
            if not (getattr(message, "tool_calls", None) or []):
                assistant_texts.append(content[:2000])
        elif message_type == "tool":
            registered = capability_by_name(getattr(message, "name", ""), kind="mcp")
            if not registered:
                continue
            capability_id = registered[0]
            try:
                envelope = json.loads(content)
                validate_result_envelope(capability_id, envelope)
            except Exception:
                continue
            if envelope.get("outcome") not in {"SUCCESS", "EMPTY", "PARTIAL"}:
                continue
            if envelope.get("trust_class") != "untrusted_banking_data":
                continue
            tool_texts.append(json.dumps({
                "capability_id": capability_id,
                "outcome": envelope.get("outcome"),
                "data": envelope.get("data"),
            }, ensure_ascii=False, sort_keys=True))

    conversation = "\n".join(human_texts[-12:])
    if current_text and _normalise_evidence_text(current_text) not in _normalise_evidence_text(conversation):
        conversation = "\n".join([conversation, current_text[:4000]])
    return {
        "current_turn": current_text[:4000] if current_text else "",
        "conversation": conversation,
        "assistant_context": "\n".join(assistant_texts[-8:]),
        "application_context": json.dumps(application_context or {}, ensure_ascii=False, sort_keys=True),
        "capability_result": "\n".join(tool_texts[-8:]),
    }


def registered_capability_vocabulary() -> str:
    """Render every agent-invocable MCP/frontend capability with its exact ID.

    The LLM needs the same canonical vocabulary for backend reads and frontend
    actions; hiding frontend IDs made otherwise valid UI assessments impossible.
    """
    registry = get_registry()
    rows: list[str] = []
    for capability_id, contract in registry.get("capabilities", {}).items():
        if contract.get("authorization", {}).get("agent_invocable") is not True:
            continue
        if contract.get("kind") not in {"mcp", "frontend"}:
            continue
        # Draft creation is a runtime-owned frontend side effect, not a model
        # callable capability. It is intentionally omitted from the LLM catalog.
        if capability_id == "frontend.transfer.prepare_draft":
            continue
        schema = contract.get("input_schema", {})
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = set(schema.get("required", []) if isinstance(schema, dict) else [])
        args = []
        for name, definition in properties.items():
            type_name = definition.get("type", "any") if isinstance(definition, dict) else "any"
            suffix = " required" if name in required else ""
            enum = definition.get("enum") if isinstance(definition, dict) else None
            enum_text = f" enum={enum}" if enum else ""
            args.append(f"{name}:{type_name}{suffix}{enum_text}")
        rows.append(
            f"{capability_id} -> {contract.get('name')} [{contract.get('kind')}]: "
            f"{contract.get('description', '')} Inputs: {', '.join(args) if args else 'none'}"
        )
    return "\n".join(rows)

def registered_presentation_vocabulary() -> str:
    """Render presentation names and their verified-data dependencies for the LLM."""
    rows: list[str] = []
    for name, contract in get_registry().get("presentations", {}).items():
        binding = contract.get("binding", {})
        properties = contract.get("props_schema", {}).get("properties", {})
        required = set(contract.get("props_schema", {}).get("required", []))
        prop_names = [f"{field}{' required' if field in required else ''}" for field in properties]
        sources = ", ".join(binding.get("source_capabilities", [])) or "none"
        rows.append(
            f"{name}: {contract.get('description', '')}; verified source capabilities={sources}; "
            f"props={', '.join(prop_names) if prop_names else 'none'}"
        )
    return "\n".join(rows)


def sanitize_customer_text(text: str) -> str:
    """Remove internal registry identifiers/tool names from customer-facing prose."""
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    registry = get_registry()
    tokens = set(registry.get("intents", {}).keys())
    for capability_id, contract in registry.get("capabilities", {}).items():
        tokens.add(capability_id)
        if contract.get("kind") in {"mcp", "frontend"}:
            tokens.add(str(contract.get("name", "")))
    cleaned = text
    for token in sorted((item for item in tokens if item), key=len, reverse=True):
        cleaned = re.sub(
            rf"(?<![A-Za-z0-9_])`?{re.escape(token)}`?(?![A-Za-z0-9_])",
            "", cleaned, flags=re.IGNORECASE,
        )
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", cleaned)
    return cleaned.strip()


def interpretation_assessment_schema() -> dict[str, Any]:
    """Return the canonical provider-neutral assessment schema for tooling/docs."""
    return InterpretationAssessment.model_json_schema()
