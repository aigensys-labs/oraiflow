"""Deterministic parsing and disambiguation for synthetic saved beneficiaries.

This module only resolves a user-supplied display name to a customer-owned contact
record. It does not authorize, preview, approve, or submit a payment. Parsed values
remain untrusted draft inputs until the customer reviews them and the Banking Kernel
revalidates the contact, account, amount, currency and policy at preview/confirmation.
"""

from __future__ import annotations

import re
from typing import Any

_CURRENCY_TOKEN = r"EUR|EUROS?|USD|DOLLARS?|GBP|POUNDS?|[€$£]"
_AMOUNT_CURRENCY = re.compile(
    rf"(?:(?P<currency_prefix>{_CURRENCY_TOKEN})\s*)?"
    r"(?P<amount>\d+(?:[.,]\d{1,2})?)(?![\d.,])"
    rf"(?:\s*(?P<currency_suffix>{_CURRENCY_TOKEN}))?",
    re.IGNORECASE,
)
_CURRENCY_CODES = {
    "EUR": "EUR",
    "EURO": "EUR",
    "EUROS": "EUR",
    "€": "EUR",
    "USD": "USD",
    "DOLLAR": "USD",
    "DOLLARS": "USD",
    "$": "USD",
    "GBP": "GBP",
    "POUND": "GBP",
    "POUNDS": "GBP",
    "£": "GBP",
}
_CANCEL = re.compile(r"\b(cancel|never mind|nevermind|stop|don't transfer|do not transfer)\b", re.I)
_ORDINALS = {
    "first": 1,
    "first one": 1,
    "the first": 1,
    "the first one": 1,
    "second": 2,
    "second one": 2,
    "the second": 2,
    "the second one": 2,
    "third": 3,
    "third one": 3,
    "the third": 3,
    "the third one": 3,
    "fourth": 4,
    "fourth one": 4,
    "the fourth": 4,
    "the fourth one": 4,
    "last one": -1,
    "the last one": -1,
    "latter": -1,
    "the latter": -1,
    "latter one": -1,
    "the latter one": -1,
    "former": 1,
    "the former": 1,
    "former one": 1,
    "the former one": 1,
}


def _normalise(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _clean_recipient(value: str) -> str:
    value = re.sub(r"\b(?:please|thanks|thank you)\s*$", "", value, flags=re.I)
    value = re.sub(r"^please\s+", "", value, flags=re.I)
    return value.strip(" ,;:.")


def _is_plausible_recipient_name(value: str) -> bool:
    """Reject transfer scaffolding and amount fragments masquerading as names."""
    candidate = _clean_recipient(value)
    words = _normalise(candidate).split()
    if not words or len(candidate) > 80:
        return False
    # Common instruction/preposition fragments are not contact identifiers.
    if words[0] in {
        "of",
        "for",
        "from",
        "using",
        "to",
        "with",
        "the",
        "a",
        "an",
        "some",
        "any",
        "money",
        "funds",
        "payment",
        "transfer",
        "please",
    }:
        return False
    # A contact name must not contain the amount/currency that the parser
    # independently extracted. This rejects e.g. "John 50 EUR" as a name.
    if _AMOUNT_CURRENCY.search(candidate):
        return False
    return bool(re.search(r"[a-zA-Z]", candidate))


def extract_contact_transfer_request(
    message: str,
    context: dict[str, Any] | None = None,
    *,
    allow_currency_missing: bool = False,
) -> dict[str, Any] | None:
    """Extract a narrow, explicit amount/currency/recipient transfer request.

    Supports forms such as ``send 50 EUR to John``, ``transfer €50 to John`` and
    ``pay John 50 euros``. It deliberately does not guess number words, missing
    currencies, recipients or accounts. Returned values are an untrusted draft.
    """
    text = " ".join(str(message or "").strip().split())
    if not text or not re.search(r"\b(send|transfer|pay)\b", text, re.I):
        return None

    amount_match = _AMOUNT_CURRENCY.search(text)
    if not amount_match:
        return None
    amount = amount_match.group("amount").replace(",", ".")
    currency_token = amount_match.group("currency_prefix") or amount_match.group("currency_suffix")
    currency = _CURRENCY_CODES.get(str(currency_token or "").upper())
    if not currency and not allow_currency_missing:
        return None

    # Preferred explicit recipient form: "send/transfer 50 EUR to John".
    recipient_match = re.search(
        r"\b(?:to|for)\s+(.+?)(?=\s+\b(?:from|using|out of)\b|[.!?]|$)",
        text,
        re.I,
    )
    recipient = _clean_recipient(recipient_match.group(1)) if recipient_match else ""

    # Also accept "pay John 50 EUR" / "send John 50 EUR" without inventing
    # a beneficiary from unrelated trailing words.
    if not recipient:
        before_amount = text[: amount_match.start()].strip(" ,;:")
        prefix_match = re.search(r"\b(?:send|transfer|pay)\s+(.+)$", before_amount, re.I)
        if prefix_match:
            recipient = _clean_recipient(prefix_match.group(1))

    if not 2 <= len(recipient) <= 80 or not _is_plausible_recipient_name(recipient):
        return None
    if re.search(r"\b(?:my|the)\s+(?:account|bank|savings)(?:\s+account)?\b", recipient, re.I):
        return None
    # Reject what is obviously a generic instruction rather than a contact name.
    if re.search(r"\b(?:tomorrow|today|next week|later|now|please transfer)\b", recipient, re.I):
        return None

    context = context if isinstance(context, dict) else {}
    accounts = context.get("accounts") if isinstance(context.get("accounts"), list) else []
    valid_accounts = [
        account for account in accounts if isinstance(account, dict) and account.get("account_id")
    ]
    selected_id = context.get("selected_account_id")
    selected = next(
        (account for account in valid_accounts if account.get("account_id") == selected_id), None
    )
    explicit_account = next(
        (
            account
            for account in valid_accounts
            if account.get("account_name")
            and _normalise(account["account_name"]) in _normalise(text)
        ),
        None,
    )
    explicit_source_clause = re.search(
        r"\b(?:from|using|out of)\s+(.+?)\s*[.!?]*$", text, re.IGNORECASE
    )
    currency_accounts = [
        account
        for account in valid_accounts
        if currency and str(account.get("currency", "")).upper() == currency
    ]

    # Never choose the first account merely because it appears first in context.
    # A current selection or an account named explicitly by the customer is a
    # valid source; otherwise an account may be inferred only when exactly one
    # currency-compatible account exists.
    if explicit_account:
        source = (
            explicit_account
            if str(explicit_account.get("currency", "")).upper() == currency
            else None
        )
    elif explicit_source_clause:
        # An explicit source clause owns the slot even when it cannot yet be
        # resolved. Never substitute the ambient UI selection or sole account.
        source = None
    elif selected:
        source = selected if str(selected.get("currency", "")).upper() == currency else None
    else:
        source = currency_accounts[0] if len(currency_accounts) == 1 else None
    return {
        "beneficiary_query": recipient,
        "amount": amount,
        "currency": currency or "",
        "from_account_id": str(source["account_id"]) if source else "",
        "source_account_candidates": [
            {
                "account_id": str(account["account_id"]),
                "account_name": str(account.get("account_name", "")),
                "currency": str(account.get("currency", "")).upper(),
            }
            for account in currency_accounts
        ],
    }


def is_contact_resolution_cancel(message: str) -> bool:
    """Return whether a customer message explicitly cancels contact disambiguation."""
    return bool(_CANCEL.search(str(message or "")))


def resolve_contact_choice(candidates: list[dict[str, Any]], answer: str) -> dict[str, Any] | None:
    """Resolve a follow-up only when exactly one stored candidate matches."""
    if not isinstance(candidates, list) or not candidates:
        return None
    value = _normalise(answer)
    if not value:
        return None

    ordinal = re.fullmatch(r"(?:option\s*)?(\d{1,2})", value)
    index = int(ordinal.group(1)) if ordinal else _ORDINALS.get(value)
    if index is not None:
        if index == -1:
            index = len(candidates)
        return candidates[index - 1] if 1 <= index <= len(candidates) else None

    exact = [
        candidate for candidate in candidates if _normalise(candidate.get("full_name", "")) == value
    ]
    if len(exact) == 1:
        return exact[0]

    # A bank name or masked account suffix can disambiguate identical names.
    narrowed = [
        candidate
        for candidate in candidates
        if (
            _normalise(candidate.get("full_name", ""))
            and _normalise(candidate.get("full_name", "")) in value
        )
        or (
            _normalise(candidate.get("bank_name", ""))
            and _normalise(candidate.get("bank_name", "")) in value
        )
        or (
            _normalise(candidate.get("account_reference_masked", ""))
            and _normalise(candidate.get("account_reference_masked", "")) in value
        )
    ]
    if len(narrowed) == 1:
        return narrowed[0]

    # A unique name token is acceptable only within the already retrieved
    # candidate set. Remove common connective words so combined answers such as
    # "Carter from Everyday account" still resolve the bounded contact choice.
    stop_words = {
        "the",
        "a",
        "an",
        "from",
        "with",
        "to",
        "for",
        "please",
        "use",
        "using",
        "account",
        "my",
        "source",
        "one",
        "contact",
    }
    answer_tokens = {token for token in re.findall(r"[a-z0-9]+", value) if token not in stop_words}
    token_matches = []
    for candidate in candidates:
        name_tokens = set(re.findall(r"[a-z0-9]+", _normalise(candidate.get("full_name", ""))))
        if answer_tokens & name_tokens:
            token_matches.append(candidate)
    return token_matches[0] if len(token_matches) == 1 else None


def is_contact_choice_reply(candidates: list[dict[str, Any]], answer: str) -> bool:
    """Distinguish an ambiguous contact answer from a new unrelated question."""
    value = _normalise(answer)
    if not value:
        return False
    if re.fullmatch(r"(?:option\s*)?\d{1,2}", value) or value in _ORDINALS:
        return True
    candidate_tokens = set()
    for candidate in candidates or []:
        for field in ("full_name", "bank_name", "account_reference_masked"):
            candidate_tokens.update(_normalise(candidate.get(field, "")).split())
    return any(token in candidate_tokens for token in value.split())


def format_contact_choices(candidates: list[dict[str, Any]]) -> str:
    """Format candidate contacts with masked references for a customer choice prompt."""
    lines = []
    for index, candidate in enumerate(candidates, start=1):
        lines.append(
            f"{index}. **{candidate.get('full_name', 'Saved contact')}** — "
            f"{candidate.get('bank_name', 'Bank not listed')} · "
            f"{candidate.get('account_reference_masked', 'Account ending unavailable')}"
        )
    return "\n".join(lines)
