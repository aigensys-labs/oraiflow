"""Shared assistant capability catalogue used by API guidance and UI templates."""
from __future__ import annotations

from typing import Any

from shared.contracts.registry import get_registry


def assistant_capability_catalogue() -> list[dict[str, Any]]:
    """Return stable, presentation-safe capability cards from the OCCF registry."""
    items = get_registry().get("application_policy", {}).get("assistant_capabilities", [])
    valid = [item for item in items if isinstance(item, dict) and item.get("capability_id")]
    return sorted(valid, key=lambda item: (int(item.get("sort_order", 0)), str(item.get("capability_id"))))


def assistant_capability_catalogue_message() -> str:
    """Build customer-facing capability help from the same catalogue as the UI."""
    parts = ["Here are the main ways I can help in ORAIflow:"]
    for item in assistant_capability_catalogue():
        title = str(item.get("title", "ORAIflow assistance"))
        description = str(item.get("description", ""))
        parts.append(f"**{title}**\n{description}")
    parts.append(
        "I use registered, authenticated read capabilities for customer-specific financial facts. "
        "I can prepare an editable transfer draft, but payment preview, customer approval, "
        "strong customer authentication and submission remain separate controlled steps."
    )
    return "\n\n".join(parts)


def assistant_capability_help_for_request(text: str) -> str | None:
    """Resolve capability-guide questions through the registered resource intent.

    This is a resource-backed semantic fast path, not a separate phrase parser:
    the canonical candidate discovery reads the OCCF intent's phrases/keywords.
    """
    from services.agent_runtime.intent_contracts import discover_intent_candidates

    for candidate in discover_intent_candidates(text, limit=20):
        if candidate.get("intent_id") != "ASSISTANT_CAPABILITY_HELP":
            continue
        if candidate.get("match_kind") != "semantic_phrase":
            continue
        if int(candidate.get("score", 0)) < int(candidate.get("minimum_score", 1100)):
            continue
        return assistant_capability_catalogue_message()
    return None
