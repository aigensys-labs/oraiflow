"""Registry-driven customer intent discovery and safe canonical read routing.

The registry owns semantic phrases, keywords, capability relationships and the
fast-path policy. This module contains one generic ranking algorithm; it does
not accumulate customer-phrase branches. A discovery result is only a routing
hint. OCCF validation, InterpretationAssessment and banking authorization remain
independent mandatory gates.
"""
from __future__ import annotations

import re
from typing import Any

from shared.contracts.registry import get_registry


def _normalise(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def _word_variants(token: str) -> set[str]:
    """Return conservative singular/plural variants for discovery only."""
    value = str(token or "").casefold()
    variants = {value}
    if len(value) > 4 and value.endswith("ies"):
        variants.add(value[:-3] + "y")
    if len(value) > 4 and value.endswith("s") and not value.endswith("ss"):
        variants.add(value[:-1])
    return variants


def _edit_distance_at_most(left: str, right: str, limit: int) -> int | None:
    """Calculate bounded Levenshtein distance without a third-party package."""
    if abs(len(left) - len(right)) > limit:
        return None
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        row_min = row
        for column, right_char in enumerate(right, start=1):
            value = min(
                current[column - 1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_char != right_char),
            )
            current.append(value)
            row_min = min(row_min, value)
        if row_min > limit:
            return None
        previous = current
    distance = previous[-1]
    return distance if distance <= limit else None


def _typo_hint_score(
    query_tokens: set[str], phrases: list[Any], keywords: list[Any], description: str,
) -> tuple[int, str | None]:
    """Return a weak intent hint for a likely typo anchored by another word.

    The match is deliberately advisory. It is not eligible for canonical
    auto-resolution, even when the registered intent normally supports a fast
    path. Requiring an exact content-word anchor alongside the typo reduces
    accidental matches between unrelated financial concepts.
    """
    vocabulary_phrases = [
        str(value) for value in [*phrases, *keywords, description]
        if isinstance(value, str) and value.strip()
    ]
    stopwords = {
        "about", "after", "again", "also", "any", "are", "can", "could", "does",
        "for", "from", "have", "into", "just", "like", "mine", "more", "most",
        "much", "need", "please", "show", "some", "that", "them", "there", "this",
        "what", "when", "where", "which", "with", "would", "your",
    }
    best_score = 0
    best_target: str | None = None
    meaningful_query = {token for token in query_tokens if len(token) >= 4 and token not in stopwords}
    for phrase in vocabulary_phrases:
        target_tokens = {
            token for token in _normalise(phrase).split()
            if len(token) >= 4 and token not in stopwords
        }
        if not target_tokens:
            continue
        exact_anchors = 0
        fuzzy_targets: list[tuple[str, str]] = []
        for query_token in meaningful_query:
            query_forms = _word_variants(query_token)
            if any(query_forms & _word_variants(target) for target in target_tokens):
                exact_anchors += 1
                continue
            if len(query_token) < 5:
                continue
            nearest: tuple[int, str] | None = None
            for target in target_tokens:
                if len(target) < 5:
                    continue
                for query_form in query_forms:
                    for target_form in _word_variants(target):
                        if query_form == target_form:
                            continue
                        max_distance = 1 if max(len(query_form), len(target_form)) <= 8 else 2
                        distance = _edit_distance_at_most(query_form, target_form, max_distance)
                        if distance is not None and (nearest is None or distance < nearest[0]):
                            nearest = (distance, target)
            if nearest is not None:
                fuzzy_targets.append((nearest[1], query_token))
        # A single typo is meaningful only when the same registry phrase also
        # contains a word the customer wrote correctly.
        if fuzzy_targets and exact_anchors:
            score = min(84, 36 + 20 * min(len({target for target, _ in fuzzy_targets}), 2) + 4 * min(exact_anchors, 2))
            # Preserve the singular/plural distinction between a single latest
            # transaction and a recent-transaction list when both are plausible.
            if all(target.endswith("s") and not query_token.endswith("s") for target, query_token in fuzzy_targets):
                score = max(1, score - 8)
            if score > best_score:
                best_score = score
                best_target = fuzzy_targets[0][0]
    return best_score, best_target


def discover_intent_candidates(text: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Rank registered intents against registry-owned semantic discovery data.

    Exact semantic phrases are high-confidence candidate evidence. Keyword
    overlap can surface a possible capability for the model to assess, but is
    deliberately insufficient to trigger a financial read by itself.
    """
    query = _normalise(text)
    if not query:
        return []
    padded_query = f" {query} "
    pattern_query = str(text or "").casefold()
    query_tokens = set(query.split())
    candidates: list[dict[str, Any]] = []

    for intent_id, contract in get_registry().get("intents", {}).items():
        recognition = contract.get("recognition", {}) if isinstance(contract, dict) else {}
        discovery = contract.get("semantic_discovery", {}) if isinstance(contract, dict) else {}
        if not isinstance(discovery, dict):
            discovery = {}

        excluded = any(
            re.search(pattern, query, re.IGNORECASE)
            for pattern in recognition.get("exclusions", [])
            if isinstance(pattern, str)
        )
        if excluded:
            continue

        score = 0
        match_kind = "keyword_overlap"
        matched_phrase = None
        phrases = discovery.get("phrases", [])
        if isinstance(phrases, list):
            matching_phrases = [
                _normalise(phrase) for phrase in phrases
                if _normalise(phrase) and f" {_normalise(phrase)} " in padded_query
            ]
            if matching_phrases:
                matched_phrase = max(matching_phrases, key=lambda value: (len(value.split()), len(value)))
                # A full customer phrase is strong semantic evidence. Longer
                # phrases carry a little more specificity, not more authority.
                # A declared full phrase outranks broad regex patterns from a
                # neighbouring intent. This is registry semantics, not a
                # customer-phrase exception in the runtime.
                score = 1100 + min(80, len(matched_phrase.split()) * 4)
                match_kind = "semantic_phrase"

        if score == 0:
            keywords = discovery.get("keywords", [])
            if isinstance(keywords, list):
                seen: set[str] = set()
                for keyword in keywords:
                    phrase = _normalise(keyword)
                    if not phrase or phrase in seen:
                        continue
                    seen.add(phrase)
                    words = set(phrase.split())
                    if f" {phrase} " in padded_query:
                        score += 3 if len(words) > 1 else 2
                    elif words and words.issubset(query_tokens):
                        score += 1

        fuzzy_token = None
        if score == 0:
            fuzzy_score, fuzzy_token = _typo_hint_score(
                query_tokens,
                phrases if isinstance(phrases, list) else [],
                discovery.get("keywords", []) if isinstance(discovery.get("keywords", []), list) else [],
                str(contract.get("description", "")),
            )
            if fuzzy_score:
                score = fuzzy_score
                match_kind = "typo_tolerant_hint"

        # Existing deterministic recognizers remain first-class registry
        # contracts; their match gets a higher score in the same candidate set.
        deterministic_match = False
        if recognition.get("mode") == "deterministic":
            patterns = recognition.get("patterns", [])
            deterministic_match = any(
                re.search(pattern, pattern_query, re.IGNORECASE)
                for pattern in patterns if isinstance(pattern, str)
            )
            if deterministic_match:
                score = max(score, 1000)
                match_kind = "deterministic_pattern"

        if score <= 0:
            continue

        plan = contract.get("execution_plan", {}) if isinstance(contract, dict) else {}
        capability_id = plan.get("capability")
        supporting = plan.get("supporting_capabilities", [])
        if not capability_id and supporting:
            capability_id = supporting[0]
        capability = get_registry().get("capabilities", {}).get(capability_id, {}) if capability_id else {}
        candidates.append({
            "intent_id": intent_id,
            "description": str(contract.get("description", "")),
            "capability_id": capability_id,
            "capability_name": str(capability.get("name", "")),
            "presentation": plan.get("presentation"),
            "response_mode": plan.get("response_mode"),
            "score": score,
            "match_kind": match_kind,
            "matched_phrase": matched_phrase,
            "matched_typo_token": fuzzy_token,
            # Only exact registry phrases and deterministic patterns may feed
            # the canonical fast path. Fuzzy discovery is always LLM-only.
            "auto_resolve": (
                match_kind in {"semantic_phrase", "deterministic_pattern"}
                and (bool(discovery.get("auto_resolve", False)) or deterministic_match)
            ),
            "minimum_score": int(discovery.get("minimum_score", 1100 if match_kind == "semantic_phrase" else 1000)),
        })

    # Deterministic registry order provides stable tie-breaking; an ambiguous
    # result remains ambiguous and is never silently converted into an action.
    candidates.sort(key=lambda item: (-int(item["score"]), str(item["intent_id"])))
    return candidates[:max(1, int(limit))]


def classify_canonical_read_intent(text: str) -> str | None:
    """Return only an unambiguous, registry-authorized canonical read intent."""
    candidates = discover_intent_candidates(text, limit=5)
    eligible = [
        item for item in candidates
        if item["auto_resolve"] and item["score"] >= item["minimum_score"]
    ]
    if not eligible:
        return None

    highest = eligible[0]
    if len(eligible) > 1:
        second = eligible[1]
        # Two equally plausible intents require model assessment/clarification.
        if int(highest["score"]) - int(second["score"]) < 20:
            return None

    intent = get_registry().get("intents", {}).get(highest["intent_id"], {})
    plan = intent.get("execution_plan", {})
    capability_id = plan.get("capability")
    supporting = plan.get("supporting_capabilities", [])
    read_capabilities = [item for item in [capability_id, *supporting] if item]
    if not read_capabilities:
        return None

    # Only read-only, agent-invocable MCP capabilities can be selected by this
    # fast path. Frontend actions, drafts and Kernel operations need the normal
    # model/assessment/workflow path.
    registry_capabilities = get_registry().get("capabilities", {})
    for item_id in read_capabilities:
        item = registry_capabilities.get(item_id, {})
        if item.get("kind") != "mcp" or item.get("authorization", {}).get("agent_invocable") is not True:
            return None
        if item.get("execution", {}).get("effect") not in {"read", "read_only"}:
            return None
    return str(highest["intent_id"])


def intent_discovery_context(text: str, *, limit: int = 4) -> list[dict[str, Any]]:
    """Return a compact, customer-turn-scoped resource shortlist for the LLM."""
    registry = get_registry()
    intents = registry.get("intents", {})
    result: list[dict[str, Any]] = []
    for item in discover_intent_candidates(text, limit=limit):
        intent = intents.get(item["intent_id"], {}) if isinstance(intents, dict) else {}
        discovery = intent.get("semantic_discovery", {}) if isinstance(intent, dict) else {}
        if not isinstance(discovery, dict):
            discovery = {}
        phrases = discovery.get("phrases", [])
        keywords = discovery.get("keywords", [])
        result.append({
            "intent_id": item["intent_id"],
            "description": item["description"],
            "capability_id": item["capability_id"],
            "capability_name": item["capability_name"],
            "response_mode": item["response_mode"],
            "match_kind": item["match_kind"],
            "matched_phrase": item.get("matched_phrase"),
            "matched_typo_token": item.get("matched_typo_token"),
            # Keep semantic vocabulary scoped to the candidates relevant to this
            # customer turn. The registry remains the authoritative source for
            # application and capability decisions.
            "registered_phrases": [str(value)[:100] for value in phrases[:5]] if isinstance(phrases, list) else [],
            "registered_keywords": [str(value)[:70] for value in keywords[:8]] if isinstance(keywords, list) else [],
            "score": item["score"],
        })
    return result
