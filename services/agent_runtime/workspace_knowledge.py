"""Registry-backed workspace guidance for customer-facing explanations.

This module provides explanatory text only. Navigation remains a separate
registered frontend capability, and data access remains behind authenticated
banking capabilities.
"""
from __future__ import annotations

import re
from typing import Any

from shared.contracts.registry import get_registry


def workspace_catalogue() -> dict[str, dict[str, Any]]:
    """Return the canonical workspace knowledge catalogue from OCCF."""
    value = get_registry().get("application_policy", {}).get("workspaces", {})
    return value if isinstance(value, dict) else {}


def workspace_explanation_message(workspace_id: str) -> str | None:
    """Build one workspace explanation exclusively from registered metadata."""
    workspace = workspace_catalogue().get(str(workspace_id or "").strip())
    if not isinstance(workspace, dict):
        return None
    features = workspace.get("features", [])
    limitations = workspace.get("limitations", [])
    feature_text = "\n".join(f"• {item}" for item in features if str(item).strip())
    limitation_text = "\n".join(f"• {item}" for item in limitations if str(item).strip())
    sections = [
        f"**{workspace_id}**",
        str(workspace.get("purpose", "")).strip(),
        "**How it helps**\n" + str(workspace.get("value", "")).strip(),
    ]
    if feature_text:
        sections.append("**What you can find there**\n" + feature_text)
    if limitation_text:
        sections.append("**A few boundaries**\n" + limitation_text)
    return "\n\n".join(section for section in sections if section.strip())


def workspace_catalogue_overview_message() -> str:
    """Build a concise guide to every registered workspace/tab."""
    workspaces = workspace_catalogue()
    lines = ["Here’s a guide to the ORAIflow workspaces:"]
    for workspace_id, workspace in workspaces.items():
        if not isinstance(workspace, dict):
            continue
        purpose = str(workspace.get("purpose", "")).strip()
        value = str(workspace.get("value", "")).strip()
        if purpose or value:
            lines.append(f"**{workspace_id}** — {purpose} {value}".strip())
    lines.append(
        "I can explain any workspace in more detail. When you ask about your own balances, "
        "transactions or financial indicators, I use the relevant authenticated, read-only data "
        "capability. Explanations do not navigate or move money; payment preparation and approval "
        "remain separate controlled steps."
    )
    return "\n\n".join(lines)


_EXPLANATION_FORM = re.compile(
    r"\b(?:what\s+(?:is|are|does|can)|what(?:'|’)s|is\s+for\s+what|is\s+for|for\s+what|explain|describe|tell\s+me\s+(?:more\s+)?about|"
    r"when\s+should\s+i\s+use|how\s+do\s+i\s+use)\b",
    re.IGNORECASE,
)
_EXPLANATION_CONTEXT = re.compile(
    r"\b(?:about|workspace|tab|section|page|purpose|useful\s+for|used\s+for)\b",
    re.IGNORECASE,
)
_ALL_WORKSPACES_REQUEST = re.compile(
    r"\b(?:all|each|every)\s+(?:the\s+)?(?:workspaces?|tabs?|sections?|pages?)\b|"
    r"\b(?:walk|guide)\s+me\s+through\s+(?:the\s+)?(?:app|workspace|workspaces|tabs|ORAIflow)\b|"
    r"\bwhat\s+(?:can|do)\s+i\s+do\s+(?:across|in)\s+(?:the\s+)?(?:app|workspaces?|tabs?|ORAIflow)\b|"
    r"\bwhat\s+is\s+ORAIflow\s+(?:for|about)\b",
    re.IGNORECASE,
)


def _workspace_aliases() -> dict[str, str]:
    aliases = get_registry().get("application_policy", {}).get("navigation", {}).get("aliases", {})
    return {str(alias): str(target) for alias, target in aliases.items() if alias and target}


def _registered_explanation_cue_matches(value: str) -> bool:
    cues = get_registry().get("application_policy", {}).get("navigation", {}).get("explanation_cues", [])
    normalised = " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())
    for cue in cues:
        cue_value = " ".join(re.sub(r"[^a-z0-9]+", " ", str(cue).casefold()).split())
        if not cue_value:
            continue
        # Generic interrogatives identify a question, not a workspace-purpose
        # question by themselves ("what are my accounts?" asks for data).
        if cue_value in {"what is", "what are", "what does"}:
            continue
        if "..." in str(cue):
            # Registry cues may declare a bounded linguistic template such as
            # "what is ... for"; the workspace noun is resolved separately
            # through the same canonical alias catalogue.
            prefix, suffix = str(cue).split("...", 1)
            prefix = " ".join(re.sub(r"[^a-z0-9]+", " ", prefix.casefold()).split())
            suffix = " ".join(re.sub(r"[^a-z0-9]+", " ", suffix.casefold()).split())
            if prefix and suffix:
                pattern = rf"(?<![a-z0-9]){re.escape(prefix)}\s+[a-z0-9 ]{{1,80}}\s+{re.escape(suffix)}(?![a-z0-9])"
                if re.search(pattern, normalised):
                    return True
        elif " " in cue_value and cue_value in normalised:
            return True
        elif cue_value in normalised.split():
            return True
    return False


def workspace_explanation_for_request(text: str) -> str | None:
    """Resolve a question about a named tab without relying on LLM classification.

    The matching vocabulary comes from OCCF navigation aliases. A question
    about account data (for example, “what are my accounts?”) is deliberately
    not treated as a workspace explanation unless it asks about the tab,
    workspace, purpose or how to use it.
    """
    value = " ".join(str(text or "").casefold().split())
    if not value or not _EXPLANATION_FORM.search(value):
        return None

    aliases = _workspace_aliases()
    candidates = sorted(aliases.items(), key=lambda pair: len(pair[0]), reverse=True)
    for alias, workspace_id in candidates:
        alias_value = alias.casefold().strip()
        if not alias_value:
            continue
        # Singular domain nouns are not sufficient to identify a workspace.
        # For example, "how does my savings account work?" is financial
        # education, not a question about the Accounts tab. The user must
        # explicitly frame the singular noun as a page/workspace/tab.
        if alias_value in {"account", "card", "payment", "transaction", "contact", "loan"}:
            framed = re.search(
                rf"(?<![a-z0-9]){re.escape(alias_value)}\s+(?:tab|workspace|page|section)(?![a-z0-9])",
                value,
            )
            if not framed:
                continue
        if not re.search(rf"(?<![a-z0-9]){re.escape(alias_value)}(?![a-z0-9])", value):
            continue
        # Avoid turning “what are my accounts?” or “what are my cards?” into
        # page descriptions. A page-purpose cue or a “tell me about/explain”
        # form makes the customer's explanatory intent clear.
        explicit_explain = bool(re.search(r"\b(?:explain|describe|tell\s+me\s+(?:more\s+)?about)\b", value))
        has_context = bool(_EXPLANATION_CONTEXT.search(value)) or _registered_explanation_cue_matches(value)
        # Generic purpose questions such as "what is Academy?" are valid
        # workspace explanations. Resolve the noun against registered aliases
        # rather than requiring the customer to know the word "workspace".
        simple_purpose_question = bool(re.fullmatch(
            rf"\s*(?:what\s+is|what(?:'|’)s|tell\s+me\s+about)\s+(?:the\s+)?{re.escape(alias_value)}(?:\s+(?:workspace|tab|page))?\s*[?!.]*\s*",
            value,
        ))
        purpose_question = bool(re.search(
            rf"(?<![a-z0-9]){re.escape(alias_value)}\s+(?:is\s+for|for\s+what)(?![a-z0-9])",
            value,
        ))
        if not (explicit_explain or has_context or simple_purpose_question or purpose_question):
            continue
        return workspace_explanation_message(workspace_id)
    return None


def workspace_help_for_request(text: str) -> str | None:
    """Return a registry-backed workspace explanation or all-workspace guide."""
    value = str(text or "")
    normalised = " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())
    policy = get_registry().get("application_policy", {}).get("navigation", {})
    overview_phrases = policy.get("all_workspaces_phrases", [])
    if _ALL_WORKSPACES_REQUEST.search(value) or any(
        " ".join(re.sub(r"[^a-z0-9]+", " ", str(phrase).casefold()).split()) in normalised
        for phrase in overview_phrases if str(phrase).strip()
    ):
        return workspace_catalogue_overview_message()
    return workspace_explanation_for_request(value)
