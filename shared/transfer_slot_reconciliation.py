"""Narrow recovery of explicit transfer slots after the LLM selects contact search.

This module does not determine whether a customer wants to make a transfer or
select a capability. It recovers only values explicitly present in the current
customer message. Account names are matched against the application account
catalogue, then independently source-anchored by the interpretation gate and
re-verified through MCP before a transfer draft can open.
"""
from __future__ import annotations

import re
from typing import Any

from shared.contact_resolution import extract_contact_transfer_request


def _normalise_name(value: Any) -> str:
    text = str(value or "").casefold().replace("\u00a0", " ")
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split())




def _recipient_is_mentioned(text: str, recipient: str) -> bool:
    """Require the proposed search name to appear as a whole phrase in this turn."""
    tokens = _normalise_name(recipient).split()
    if not tokens:
        return False
    pattern = r"(?<![\w])" + r"[\W_]+".join(re.escape(token) for token in tokens) + r"(?![\w])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _explicit_source_account(text: str, context: dict[str, Any]) -> str | None:
    """Return one unique account label explicitly named after a source preposition.

    This is catalogue matching, not free-form intent detection. It only runs after
    the model has selected TRANSFER_PREPARATION plus the matching Contacts search.
    """
    raw_accounts = context.get("accounts") if isinstance(context, dict) else []
    matches: dict[str, str] = {}
    for account in raw_accounts or []:
        if not isinstance(account, dict) or not account.get("account_id"):
            continue
        canonical = str(account.get("account_name") or "").strip()
        if not canonical:
            continue
        aliases = {canonical}
        # Everyday account -> Everyday; Travel wallet -> Travel. These are
        # bounded aliases of the registered account name, not global keywords.
        short = re.sub(r"\s+(?:account|wallet)$", "", canonical, flags=re.IGNORECASE).strip()
        if short and short.casefold() != canonical.casefold():
            aliases.add(short)
        for alias in aliases:
            alias_tokens = _normalise_name(alias).split()
            if not alias_tokens:
                continue
            alias_pattern = r"(?<![\w])" + r"[\W_]+".join(re.escape(token) for token in alias_tokens) + r"(?![\w])"
            pattern = (
                r"\b(?:from|using|out of)\s+(?:my|the)\s+(?:" + alias_pattern + r")\b|"
                r"\b(?:from|using|out of)\s+(?:" + alias_pattern + r")\b"
            )
            if re.search(pattern, text, flags=re.IGNORECASE):
                matches[str(account["account_id"])] = canonical
    return next(iter(matches.values())) if len(matches) == 1 else None


def reconcile_explicit_transfer_slots(
    assessment: dict[str, Any],
    *,
    current_customer_text: str,
    proposed_recipient_query: str,
    context: dict[str, Any] | None = None,
) -> list[str]:
    """Fill only omitted explicit transfer slots for a matching search.

    The LLM must already have selected ``TRANSFER_PREPARATION`` and proposed a
    Contacts search. Amount/currency recovery uses the narrow explicit transfer
    parser; source-account recovery matches only a named account in the supplied
    account catalogue and requires a source preposition in the customer turn.
    Nothing here chooses a contact, trusts a browser account ID, or authorizes
    a transfer. The ordinary evidence gate and MCP verification still apply.
    """
    if not isinstance(assessment, dict):
        return []
    if str(assessment.get("intent_id", "")) != "TRANSFER_PREPARATION":
        return []
    if not current_customer_text or not proposed_recipient_query:
        return []

    parsed = extract_contact_transfer_request(current_customer_text, context or {})
    # When the full parser cannot parse a request because currency is omitted,
    # we may still preserve an account explicitly named in the same turn. The
    # proposed recipient must independently appear as a whole phrase.
    parsed_matches = bool(
        parsed and _normalise_name(parsed.get("beneficiary_query"))
        == _normalise_name(proposed_recipient_query)
    )
    if not parsed_matches and not _recipient_is_mentioned(current_customer_text, proposed_recipient_query):
        return []

    slots = assessment.get("slots")
    if not isinstance(slots, dict):
        slots = {}
        assessment["slots"] = slots

    recovered: list[str] = []
    if parsed_matches:
        for field in ("amount", "currency"):
            current_value = slots.get(field)
            if current_value is not None and str(current_value).strip():
                continue
            parsed_value = parsed.get(field)
            if parsed_value is None or not str(parsed_value).strip():
                continue
            slots[field] = str(parsed_value)
            recovered.append(field)

    # Preserve an explicitly named source account across a later recipient-
    # currency confirmation. Store the canonical catalogue label, not an ID.
    # The interpretation gate anchors the alias to current_turn + application
    # context, and the workflow independently verifies the selected ID via MCP.
    if not str(slots.get("source_account") or "").strip():
        source_account = _explicit_source_account(
            current_customer_text,
            context if isinstance(context, dict) else {},
        )
        if source_account:
            slots["source_account"] = source_account
            recovered.append("source_account")

    # No evidence is fabricated here. validate_interpretation_assessment()
    # independently anchors these values to current_turn and the account
    # catalogue, rejecting anything not supported by those sources.
    return recovered
