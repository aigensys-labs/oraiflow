"""Small, candidate-bounded transfer-preparation workflow state machine.

This module owns the single transfer-preparation state machine. It accepts
validated semantic assessments or the narrow explicit amount/recipient action
grammar, then advances only through registered capabilities and trusted
contact/account candidates returned by authenticated MCP reads. It never
selects a beneficiary or authorizes a payment on its own.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from shared.contracts.registry import get_registry
from shared.pending_task import (
    PendingTaskContext,
    attach_pending_task,
    message_metadata,
    message_type,
    message_value,
    pending_task_from_message,
)
from shared.contact_resolution import extract_contact_transfer_request
from shared.contact_resolution import (
    format_contact_choices,
    is_contact_choice_reply,
    is_contact_resolution_cancel,
    resolve_contact_choice,
)


def transfer_workflow_id(messages: list[Any], thread_id: str = "") -> str:
    """Return a stable workflow ID for replays of the same customer message.

    AG-UI/CopilotKit can resume a run after a browser tool result. The same
    customer message must therefore resolve to the same workflow identity; a
    fresh user message in the same thread receives a different identity.
    """
    items = list(messages or [])
    latest_human_index = max(
        (index for index, message in enumerate(items) if message_type(message) == "human"),
        default=-1,
    )
    if latest_human_index < 0:
        raise ValueError("A transfer workflow requires a customer message identity")
    message = items[latest_human_index]
    message_id = str(message_value(message, "id", "") or "")
    content = message_value(message, "content", "")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
    # Prefer the stable AG-UI message identity so transcript trimming or
    # checkpoint rehydration cannot mint a new workflow for the same request.
    # Fall back to the full-history index plus content for native callers that
    # do not provide message IDs.
    message_identity = (
        ["message", message_id] if message_id else ["position", latest_human_index, content]
    )
    identity = json.dumps(
        [str(thread_id or ""), message_identity],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"trf-{digest}"


def transfer_operation_id(workflow_id: str, operation: str) -> str:
    """Return a deterministic identifier for one operation within a transfer."""
    digest = hashlib.sha256(f"{workflow_id}:{operation}".encode("utf-8")).hexdigest()[:32]
    return f"act-{digest}"


def transfer_draft_replay_status(
    workflow: dict[str, Any],
    draft_state: dict[str, Any] | None,
) -> str | None:
    """Classify an application draft receipt correlated to this workflow."""
    if not isinstance(draft_state, dict):
        return None
    same_workflow = draft_state.get("workflow_id") == workflow.get("workflow_id")
    same_action = draft_state.get("action_id") == workflow.get("action_id")
    if not (same_workflow and same_action):
        return None
    status = str(draft_state.get("status", "")).casefold()
    return status if status in {"open", "dismissed", "closed", "accepted"} else None


def _normalise(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("\u00a0", " ").split())


def _money_string(value: Any) -> str:
    try:
        amount = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    if amount <= 0 or not amount.is_finite():
        return ""
    return format(amount.normalize(), "f")


def _account_alias(value: str) -> str:
    words = [
        word
        for word in _normalise(value).split()
        if word not in {"account", "my", "the", "from", "use", "using", "please"}
    ]
    return " ".join(words)


def _source_account_preference(answer: str) -> str | None:
    """Resolve a bounded comparison slot using aliases declared in OCCF.

    This is domain-slot normalization, not a general customer-phrase router:
    it is only called while the transfer state machine is awaiting a source
    account, and the resulting preference is applied to authenticated MCP rows.
    """
    value = re.sub(r"[^a-z0-9]+", " ", _normalise(answer)).strip()
    if re.search(
        r"\b(?:not|dont|do not|rather than|instead of)\s+(?:the\s+)?(?:highest|largest|most|maximum|lowest|smallest|least|minimum)\b",
        value,
    ):
        return None
    padded = f" {value} "
    aliases = (
        get_registry().get("application_policy", {}).get("transfer_source_preference_aliases", {})
    )
    matches = []
    for preference, phrases in aliases.items():
        if preference not in {"highest_balance", "lowest_balance"} or not isinstance(phrases, list):
            continue
        if any(
            f" {re.sub(r'[^a-z0-9]+', ' ', _normalise(phrase)).strip()} " in padded
            for phrase in phrases
            if _normalise(phrase)
        ):
            matches.append(preference)
    return matches[0] if len(matches) == 1 else None


def _without_source_preference(answer: str) -> str:
    """Remove a registered source preference before resolving a contact choice."""
    value = str(answer or "")
    aliases = (
        get_registry().get("application_policy", {}).get("transfer_source_preference_aliases", {})
    )
    for preference in ("highest_balance", "lowest_balance"):
        phrases = aliases.get(preference, [])
        for phrase in sorted((str(item) for item in phrases if item), key=len, reverse=True):
            words = re.sub(r"[^a-z0-9]+", " ", _normalise(phrase)).strip().split()
            if not words:
                continue
            pattern = (
                r"(?<![a-z0-9])"
                + r"[\W_]+".join(re.escape(word) for word in words)
                + r"(?![a-z0-9])"
            )
            value = re.sub(pattern, " ", value, flags=re.I)
    value = re.sub(r"\b(?:and|also|use|using|from|with)\b", " ", value, flags=re.I)
    return " ".join(value.split()).strip(" ,;:.")


def _eligible_accounts(context: dict[str, Any], currency: str) -> list[dict[str, Any]]:
    accounts = context.get("accounts") if isinstance(context, dict) else []
    return [
        dict(item)
        for item in accounts or []
        if isinstance(item, dict)
        and item.get("account_id")
        and str(item.get("currency", "")).upper() == currency.upper()
    ]


def _account_by_preference(
    answer: str,
    accounts: list[dict[str, Any]],
    *,
    allow_unique_fallback: bool = True,
) -> tuple[str | None, str]:
    """Return (account_id, reason); never chooses the first catalogue entry.

    The unique-account fallback is appropriate only when the customer did not
    explicitly name a different source account. An explicit but unmatched
    account label must never be replaced by an unrelated sole/selected account.
    """
    if not accounts:
        return None, "NO_COMPATIBLE_ACCOUNT"
    normalized = _normalise(answer)
    if normalized:
        # Match account names and natural short forms such as `Everyday` against
        # `Everyday account`. Matching is unique within the currency candidates.
        query_alias = _account_alias(normalized)
        alias_matches = [
            account
            for account in accounts
            if query_alias
            and (
                query_alias == _account_alias(account.get("account_name", ""))
                or query_alias in _account_alias(account.get("account_name", "")).split()
                or _account_alias(account.get("account_name", "")) in query_alias.split()
            )
        ]
        if len(alias_matches) == 1:
            return str(alias_matches[0]["account_id"]), "EXACT_ACCOUNT_MATCH"
        if len(alias_matches) > 1:
            return None, "AMBIGUOUS_ACCOUNT_MATCH"

        ordinal = re.fullmatch(r"(?:option\s*)?(\d{1,2})", normalized)
        ordinal_value = (
            int(ordinal.group(1))
            if ordinal
            else {
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
            }.get(normalized)
        )
        if ordinal_value is not None:
            return (
                (str(accounts[ordinal_value - 1]["account_id"]), "ACCOUNT_ORDINAL")
                if 1 <= ordinal_value <= len(accounts)
                else (None, "ACCOUNT_ORDINAL_OUT_OF_RANGE")
            )

        preference = _source_account_preference(normalized)
        if preference:
            criterion = "MAX" if preference == "highest_balance" else "MIN"
            return None, f"NEEDS_VERIFIED_BALANCE_{criterion}"

    if allow_unique_fallback and len(accounts) == 1:
        return str(accounts[0]["account_id"]), "UNIQUE_COMPATIBLE_ACCOUNT"
    return None, "ACCOUNT_REQUIRED"


def build_transfer_workflow(
    assessment: dict[str, Any],
    context: dict[str, Any],
    *,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """Create workflow state from a validated LLM assessment, never raw prompt parsing."""
    slots = assessment.get("slots") if isinstance(assessment.get("slots"), dict) else {}
    entities = assessment.get("entities") if isinstance(assessment.get("entities"), dict) else {}
    beneficiary = str(
        entities.get("beneficiary")
        or entities.get("recipient")
        or slots.get("beneficiary")
        or slots.get("recipient")
        or ""
    ).strip()
    amount = _money_string(slots.get("amount"))
    currency = str(slots.get("currency") or "").upper().strip()
    source_query = str(slots.get("source_account") or slots.get("from_account") or "").strip()
    raw_preference = str(slots.get("source_account_preference") or "").strip().lower()
    source_preference = (
        raw_preference
        if raw_preference in {"highest_balance", "lowest_balance"}
        else (_source_account_preference(raw_preference) or "")
    )
    source_account_id = ""
    selected_id = context.get("selected_account_id") if isinstance(context, dict) else None
    # A selected UI account is not evidence of the transfer currency. Preserve
    # an omitted currency as a missing slot and ask the customer to confirm it
    # after resolving the saved recipient.
    resolved_workflow_id = workflow_id or f"trf-{uuid.uuid4().hex}"
    workflow = {
        "workflow_id": resolved_workflow_id,
        "action_id": transfer_operation_id(resolved_workflow_id, "prepare-transfer-draft"),
        "workflow_type": "TRANSFER_PREPARATION",
        "stage": "CONTACT_SEARCH_PENDING",
        "amount": amount,
        "currency": currency,
        "beneficiary_query": beneficiary,
        "contact": None,
        "contact_candidates": [],
        "source_account_query": source_query,
        "source_account_preference": source_preference,
        "source_account_id": source_account_id,
        "last_action_status": None,
    }
    if source_preference in {"highest_balance", "lowest_balance"}:
        # Keep contact discovery as the first transition. Comparative account
        # ranking cannot run until a unique recipient and an explicit currency
        # have been established.
        workflow["account_resolution_reason"] = (
            "NEEDS_VERIFIED_BALANCE_MAX"
            if source_preference == "highest_balance"
            else "NEEDS_VERIFIED_BALANCE_MIN"
        )
    # Resolve a directly named account against the current account catalogue.
    elif source_query and currency:
        accounts = _eligible_accounts(context, currency)
        account_id, _reason = _account_by_preference(
            source_query,
            accounts,
            allow_unique_fallback=False,
        )
        if account_id:
            workflow["source_account_id"] = account_id
    return workflow


def build_explicit_transfer_workflow(
    customer_text: str,
    context: dict[str, Any] | None = None,
    *,
    workflow_id: str | None = None,
) -> dict[str, Any] | None:
    """Build a bounded transfer workflow from an explicit amount/recipient request.

    This parser recognizes a narrow action grammar, not arbitrary financial
    intent. It only creates a preparation workflow; contact resolution, source
    account verification, editable-draft creation and Kernel approval remain
    separate mandatory stages.
    """
    text = str(customer_text or "").strip()
    # Explanatory questions about how to make a transfer must remain educational.
    if re.search(
        r"\b(?:how\s+(?:do\s+i|can\s+i|to)|what\s+if|explain|teach\s+me)\b", text, re.IGNORECASE
    ):
        return None
    parsed = extract_contact_transfer_request(text, context or {}, allow_currency_missing=True)
    if not parsed:
        return None
    source_preference = _source_account_preference(text)
    slots: dict[str, Any] = {
        "amount": parsed.get("amount"),
        "currency": parsed.get("currency") or "",
    }
    if parsed.get("from_account_id"):
        slots["source_account_id"] = parsed["from_account_id"]
    source_clause = re.search(r"\b(?:from|using|out of)\s+(.+?)\s*[.!?]*$", text, re.IGNORECASE)
    if source_preference:
        slots["source_account_preference"] = source_preference
    elif source_clause:
        # Preserve an explicitly named source even when it is not present in
        # the current UI context. This prevents a missing/unknown account label
        # from silently falling back to ambient selection or a sole account.
        slots["source_account"] = source_clause.group(1).strip(" ,;:.")
    assessment = {
        "slots": slots,
        "entities": {"beneficiary": parsed.get("beneficiary_query", "")},
    }
    workflow = build_transfer_workflow(assessment, context or {}, workflow_id=workflow_id)
    if parsed.get("from_account_id") and not source_preference:
        workflow["source_account_id"] = str(parsed["from_account_id"])
    return workflow


def workflow_from_message(message: Any) -> dict[str, Any] | None:
    """Recover the typed transfer workflow state from a serialized conversation message."""
    task = pending_task_from_message(message)
    if task is not None:
        # A typed task envelope is authoritative even when it belongs to a
        # different domain or is terminal. Never fall through to stale legacy
        # transfer metadata copied onto a navigation/education response.
        if (
            task.domain == "transfer"
            and task.status == "ACTIVE"
            and isinstance(task.state.get("workflow"), dict)
        ):
            return dict(task.state["workflow"])
        return None
    # Read-only compatibility for conversations persisted before the shared
    # PendingTaskContext contract was introduced.
    metadata = message_metadata(message)
    legacy = metadata.get("oraiflow_transfer_workflow")
    return dict(legacy) if isinstance(legacy, dict) else None


def latest_pending_workflow(messages: list[Any]) -> dict[str, Any] | None:
    """Return only the active transfer task owned by the latest assistant response.

    A valid typed task envelope takes precedence over all legacy fields. In
    particular, a navigation or education response must not accidentally resume
    a transfer just because an old checkpoint also contains transfer metadata.
    """
    items = list(messages or [])
    latest_human = max(
        (i for i, item in enumerate(items) if message_type(item) == "human"), default=-1
    )
    after = [item for item in items[latest_human + 1 :] if message_type(item) == "ai"]
    before = [item for item in items[:latest_human] if message_type(item) == "ai"]
    owner = after[-1] if after else (before[-1] if before else None)
    if owner is None:
        return None

    typed_task = pending_task_from_message(owner)
    if typed_task is not None:
        if (
            typed_task.domain == "transfer"
            and typed_task.status == "ACTIVE"
            and isinstance(typed_task.state.get("workflow"), dict)
        ):
            return dict(typed_task.state["workflow"])
        return None

    # Read-only legacy recovery, only when no typed owner exists.
    metadata = message_metadata(owner)
    workflow = metadata.get("oraiflow_transfer_workflow")
    return dict(workflow) if isinstance(workflow, dict) else None


def with_workflow(message: Any, workflow: dict[str, Any]) -> Any:
    """Attach a serialized transfer workflow snapshot to a message."""
    stage = str(workflow.get("stage", ""))
    terminal = stage in {
        "CANCELLED",
        "CONTACT_NOT_FOUND",
        "CONTACT_SEARCH_FAILED",
        "DRAFT_REJECTED",
        "DISMISSED",
        "DRAFT_OPENED",
        "COMPLETED",
    }
    task = PendingTaskContext(
        task_id=str(workflow.get("workflow_id") or f"transfer-{uuid.uuid4().hex}"),
        domain="transfer",
        intent_id="TRANSFER_PREPARATION",
        stage=stage or "UNKNOWN",
        status="COMPLETED" if terminal else "ACTIVE",
        state={"workflow": dict(workflow)},
    )
    return attach_pending_task(message, task)


def resolve_search_result(
    workflow: dict[str, Any],
    candidates: list[dict[str, Any]],
    context: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Resolve an authenticated contact search into a pending or ready workflow."""
    updated = dict(workflow)
    active = [
        item
        for item in candidates
        if isinstance(item, dict) and item.get("status", "ACTIVE") == "ACTIVE"
    ]
    if not active:
        updated["stage"] = "CONTACT_NOT_FOUND"
        return "CONTACT_NOT_FOUND", updated
    query = str(updated.get("beneficiary_query") or "")
    selected = resolve_contact_choice(active, query)
    if selected is None:
        updated["contact_candidates"] = active
        updated["stage"] = "AWAITING_CONTACT"
        return "AWAITING_CONTACT", updated
    updated["contact"] = selected
    updated["contact_candidates"] = active
    return _complete_or_ask_account(updated, context)


def _complete_or_ask_account(
    workflow: dict[str, Any], context: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    updated = dict(workflow)
    contact = updated.get("contact") or {}
    amount = _money_string(updated.get("amount"))
    currency = str(updated.get("currency") or "").upper()
    if not amount:
        updated["stage"] = "AWAITING_AMOUNT"
        return "AWAITING_AMOUNT", updated
    if not currency:
        proposed = str(contact.get("currency") or "").upper()
        updated["proposed_currency"] = proposed
        # Show the funding-account choices in the same clarification as the
        # proposed recipient currency. These names are suggestions only; any
        # selected account or balance preference is re-verified through MCP.
        if proposed:
            candidates = _eligible_accounts(context, proposed)
            updated["source_account_candidates"] = [
                {
                    "account_id": str(item.get("account_id", "")),
                    "account_name": str(item.get("account_name", "Account")),
                    "currency": proposed,
                }
                for item in candidates
            ]
        updated["stage"] = "AWAITING_CURRENCY"
        return "AWAITING_CURRENCY", updated
    if (
        str(contact.get("currency", "")).upper()
        and str(contact.get("currency", "")).upper() != currency
    ):
        updated["stage"] = "CURRENCY_MISMATCH"
        return "CURRENCY_MISMATCH", updated
    updated["currency"] = currency
    if str(updated.get("source_account_preference", "")).lower() in {
        "highest_balance",
        "lowest_balance",
    }:
        criterion = "MAX" if updated["source_account_preference"] == "highest_balance" else "MIN"
        updated["account_resolution_reason"] = f"NEEDS_VERIFIED_BALANCE_{criterion}"
        updated["stage"] = "RESOLVING_SOURCE_ACCOUNT"
        return "NEEDS_VERIFIED_BALANCE", updated
    accounts = _eligible_accounts(context, currency)
    selected_id = context.get("selected_account_id") if isinstance(context, dict) else None
    account_id = str(updated.get("source_account_id") or "")
    if account_id and any(str(item.get("account_id")) == account_id for item in accounts):
        updated["source_account_id"] = account_id
        updated["stage"] = "VERIFYING_SOURCE_ACCOUNT"
        return "VERIFY_SOURCE_ACCOUNT", updated
    # An explicit source account in the original request takes precedence over
    # ambient UI selection and single-account convenience. If it cannot be
    # matched uniquely in the requested currency, ask the customer rather than
    # silently substituting another funding account.
    if updated.get("source_account_query"):
        matched_id, reason = _account_by_preference(
            str(updated["source_account_query"]),
            accounts,
            allow_unique_fallback=False,
        )
        if matched_id:
            updated["source_account_id"] = matched_id
            updated["stage"] = "VERIFYING_SOURCE_ACCOUNT"
            return "VERIFY_SOURCE_ACCOUNT", updated
        updated["account_resolution_reason"] = reason
        updated["source_account_candidates"] = accounts
        updated["stage"] = "AWAITING_SOURCE_ACCOUNT"
        return "AWAITING_SOURCE_ACCOUNT", updated
    # A current selection is valid when it is among the currency-compatible accounts.
    if selected_id and any(str(item.get("account_id")) == str(selected_id) for item in accounts):
        updated["source_account_id"] = str(selected_id)
        updated["stage"] = "VERIFYING_SOURCE_ACCOUNT"
        return "VERIFY_SOURCE_ACCOUNT", updated
    # One compatible source is unambiguous; multiple candidates require a choice.
    if len(accounts) == 1:
        updated["source_account_id"] = str(accounts[0]["account_id"])
        updated["stage"] = "VERIFYING_SOURCE_ACCOUNT"
        return "VERIFY_SOURCE_ACCOUNT", updated
    updated["source_account_candidates"] = accounts
    updated["stage"] = "AWAITING_SOURCE_ACCOUNT"
    return "AWAITING_SOURCE_ACCOUNT", updated


def resolve_contact_reply(
    workflow: dict[str, Any],
    answer: str,
    context: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Apply a customer reply to the pending contact-selection stage of a transfer."""
    updated = dict(workflow)
    candidates = updated.get("contact_candidates") or []
    if is_contact_resolution_cancel(answer):
        updated["stage"] = "CANCELLED"
        return "CANCELLED", updated
    # A single customer reply may resolve an account preference before or while
    # it resolves the beneficiary (e.g. "Carter from Everyday account" or
    # "former, highest balance"). The preference remains a request until the
    # runtime ranks fresh authenticated MCP account rows.
    preference = _source_account_preference(answer)
    if preference:
        updated["source_account_preference"] = preference
    known_currency = str(updated.get("currency") or "").upper()
    if known_currency:
        account_id, reason = _account_by_preference(
            answer, _eligible_accounts(context, known_currency)
        )
        if account_id:
            updated["source_account_id"] = account_id
        elif reason.startswith("NEEDS_VERIFIED_BALANCE_"):
            updated["source_account_preference"] = (
                "highest_balance" if reason.endswith("_MAX") else "lowest_balance"
            )
            updated["account_resolution_reason"] = reason

    contact_answer = _without_source_preference(answer) if preference else answer
    contact = resolve_contact_choice(candidates, contact_answer)
    if not contact:
        if is_contact_choice_reply(candidates, answer):
            updated["stage"] = "AWAITING_CONTACT"
            return "AWAITING_CONTACT", updated
        # A source-account phrase alone is not enough to select a beneficiary.
        # Keep the pending contact clarification and ask only for the missing person.
        if updated.get("source_account_id") or updated.get("account_resolution_reason"):
            updated["stage"] = "AWAITING_CONTACT"
            return "AWAITING_CONTACT", updated
        return "UNRELATED", updated
    updated["contact"] = contact
    # The saved recipient currency is a trusted suggestion, not a substitute
    # for a currency the customer omitted. _complete_or_ask_account asks the
    # customer to confirm it before any source-account selection.
    currency = str(updated.get("currency") or "").upper()
    accounts = _eligible_accounts(context, currency) if currency else []
    if not updated.get("source_account_id"):
        account_id, reason = _account_by_preference(answer, accounts)
        if account_id:
            updated["source_account_id"] = account_id
        elif reason.startswith("NEEDS_VERIFIED_BALANCE_"):
            updated["account_resolution_reason"] = reason
    return _complete_or_ask_account(updated, context)


def currency_choice_prompt(workflow: dict[str, Any]) -> str:
    """Ask for currency and, at the same time, invite the funding-source choice."""
    contact = workflow.get("contact") or {}
    proposed = str(workflow.get("proposed_currency") or contact.get("currency") or "").upper()
    amount = str(workflow.get("amount") or "")
    recipient = str(
        contact.get("full_name") or workflow.get("beneficiary_query") or "the saved recipient"
    )
    source_query = str(workflow.get("source_account_query") or "").strip()
    source_preference = str(workflow.get("source_account_preference") or "").strip()
    accounts = [
        item
        for item in (workflow.get("source_account_candidates") or [])
        if isinstance(item, dict) and str(item.get("currency", proposed)).upper() == proposed
    ]
    choices = "\n".join(
        f"{index}. **{item.get('account_name', 'Account')}**"
        for index, item in enumerate(accounts, start=1)
    )
    if proposed:
        if source_query:
            source_instruction = (
                f"Your requested source is **{source_query}**; I will verify that exact account."
            )
            currency_instruction = (
                f"Reply **yes** to confirm {proposed}, or specify another supported currency."
            )
        elif source_preference in {"highest_balance", "lowest_balance"}:
            label = "highest" if source_preference == "highest_balance" else "lowest"
            source_instruction = (
                f"I will apply your {label}-balance preference using freshly verified balances."
            )
            currency_instruction = (
                f"Reply **yes** to confirm {proposed}, or specify another supported currency."
            )
        elif choices:
            source_instruction = (
                "Choose the account that should fund it, or say **highest balance** "
                "and I will compare fresh verified balances:\n\n" + choices
            )
            currency_instruction = f"Your account choice confirms {proposed}; tell me if you need a different supported currency."
        else:
            source_instruction = (
                "Choose the ORAIflow account that should fund it, or say **highest balance** "
                "and I will compare fresh verified balances."
            )
            currency_instruction = f"Your account choice confirms {proposed}; tell me if you need a different supported currency."
        return (
            f"I found **{recipient}**. The saved recipient account uses **{proposed}**. "
            f"I can prepare **{amount} {proposed}**. {source_instruction}\n\n"
            f"{currency_instruction} "
            "I will verify the recipient and funding account before opening an editable draft. "
            "No transfer draft has been opened."
        )
    return (
        f"I found **{recipient}** and understood the amount as **{amount}**, but the currency is missing. "
        "Which currency should I use (EUR, USD or GBP), and which ORAIflow account should fund it? "
        "You can also say **highest balance**; I will compare fresh verified balances. "
        "No transfer draft has been opened."
    )


def _remaining_source_choice_text(answer: str) -> str:
    """Remove a currency confirmation and currency token, leaving an optional source choice."""
    value = _normalise(answer)
    value = re.sub(
        r"^(?:yes|yeah|yep|ok|okay|sure|correct|sounds good|go ahead|please do|fine|alright|all right)\b[\s,]*(?:(?:and|also|use|choose|from)\b[\s,]*)?",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"\b(?:eur|euro|euros|usd|dollar|dollars|gbp|pound|pounds)\b|[€$£]", " ", value, flags=re.I
    )
    value = re.sub(r"\b(?:and|also|use|choose|from|the|my|account)\b", " ", value, flags=re.I)
    return " ".join(value.split()).strip(" ,.;:!?-")


def resolve_currency_reply(
    workflow: dict[str, Any],
    answer: str,
    context: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Resolve currency and an optional source-account choice from one reply."""
    updated = dict(workflow)
    value = _normalise(answer).strip(" .,!?;:")
    if is_contact_resolution_cancel(answer):
        updated["stage"] = "CANCELLED"
        return "CANCELLED", updated

    proposed = str(
        updated.get("proposed_currency") or (updated.get("contact") or {}).get("currency") or ""
    ).upper()
    currency_tokens = {
        "eur": "EUR",
        "euro": "EUR",
        "euros": "EUR",
        "usd": "USD",
        "dollar": "USD",
        "dollars": "USD",
        "gbp": "GBP",
        "pound": "GBP",
        "pounds": "GBP",
    }
    explicit_currency = ""
    for token, code in currency_tokens.items():
        if re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", value):
            explicit_currency = code
            break
    if not explicit_currency and any(symbol in value for symbol in ("€", "$", "£")):
        explicit_currency = {"€": "EUR", "$": "USD", "£": "GBP"}[
            next(symbol for symbol in ("€", "$", "£") if symbol in value)
        ]

    acknowledgement = bool(
        re.match(
            r"^(?:yes|yeah|yep|ok|okay|sure|correct|sounds good|go ahead|please do|fine|alright|all right)(?:\b|[, ])",
            value,
            flags=re.I,
        )
    ) or value in {
        "yes",
        "yeah",
        "yep",
        "ok",
        "okay",
        "sure",
        "correct",
        "sounds good",
        "go ahead",
        "please do",
        "fine",
        "alright",
        "all right",
    }
    source_reply = _remaining_source_choice_text(answer)
    source_accounts = _eligible_accounts(context, proposed) or [
        dict(item)
        for item in (updated.get("source_account_candidates") or [])
        if isinstance(item, dict) and str(item.get("currency", "")).upper() == proposed
    ]
    source_preference_reply = bool(_source_account_preference(source_reply))
    source_account_reply = bool(
        source_reply
        and _account_by_preference(
            source_reply,
            source_accounts,
            allow_unique_fallback=False,
        )[0]
    )
    # The clarification explicitly tells the customer that choosing a funding
    # account confirms the saved recipient currency. A source choice therefore
    # counts as confirmation without requiring a redundant separate "yes".
    currency = explicit_currency or (
        proposed if acknowledgement or source_preference_reply or source_account_reply else ""
    )
    if currency not in {"EUR", "USD", "GBP"}:
        return "UNRELATED", updated

    contact = updated.get("contact") or {}
    recipient_currency = str(contact.get("currency") or proposed).upper()
    if recipient_currency and currency != recipient_currency:
        updated["stage"] = "CURRENCY_MISMATCH"
        updated["requested_currency"] = currency
        updated["proposed_currency"] = recipient_currency
        return "CURRENCY_MISMATCH", updated

    updated["currency"] = currency
    updated.pop("proposed_currency", None)
    updated.pop("requested_currency", None)

    # If the customer included a source choice in the same answer, resolve it
    # now. A comparative preference only schedules an authenticated MCP read;
    # it never chooses from browser-supplied balances.
    if source_reply:
        preference = _source_account_preference(source_reply)
        if preference:
            updated["source_account_preference"] = preference
            updated.pop("source_account_query", None)
            updated.pop("source_account_id", None)
        else:
            accounts = _eligible_accounts(context, currency)
            if not accounts:
                accounts = [
                    dict(item)
                    for item in (updated.get("source_account_candidates") or [])
                    if isinstance(item, dict) and str(item.get("currency", "")).upper() == currency
                ]
            account_id, reason = _account_by_preference(
                source_reply,
                accounts,
                allow_unique_fallback=False,
            )
            if account_id:
                updated["source_account_id"] = account_id
                updated.pop("source_account_query", None)
            elif reason.startswith("NEEDS_VERIFIED_BALANCE_"):
                updated["source_account_preference"] = (
                    "highest_balance" if reason.endswith("_MAX") else "lowest_balance"
                )
                updated.pop("source_account_query", None)
                updated.pop("source_account_id", None)
            elif reason not in {"ACCOUNT_REQUIRED", "NO_COMPATIBLE_ACCOUNT"}:
                updated["source_account_query"] = source_reply
                updated.pop("source_account_id", None)

    return _complete_or_ask_account(updated, context)


def resolve_source_account_reply(
    workflow: dict[str, Any],
    answer: str,
    context: dict[str, Any],
    *,
    verified_accounts: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Resolve a source-account reply against freshly verified eligible account records."""
    updated = dict(workflow)
    if is_contact_resolution_cancel(answer):
        updated["stage"] = "CANCELLED"
        return "CANCELLED", updated
    contact = updated.get("contact") or {}
    currency = str(updated.get("currency") or "").upper()
    if not currency:
        updated["proposed_currency"] = str(contact.get("currency") or "").upper()
        updated["stage"] = "AWAITING_CURRENCY"
        return "AWAITING_CURRENCY", updated
    accounts = _eligible_accounts(context, currency)
    if not accounts:
        # The account choices were already displayed from authenticated data.
        # Reuse their identifiers/names only to interpret the bounded reply;
        # the selected ID is re-verified through MCP before a draft can open.
        stored_candidates = workflow.get("source_account_candidates") or []
        accounts = [
            dict(item)
            for item in stored_candidates
            if isinstance(item, dict) and str(item.get("currency", "")).upper() == currency
        ]
    # A pending clarification must be resolved by this answer, not silently
    # by a previously selected UI account. The selected account is considered
    # earlier when the workflow is first created; it is not an interpretation
    # of replies such as "ok" or "with the highest amount".
    account_id, reason = _account_by_preference(answer, accounts)
    if account_id:
        updated["source_account_id"] = account_id
        updated["stage"] = "VERIFYING_SOURCE_ACCOUNT"
        return "VERIFY_SOURCE_ACCOUNT", updated
    updated["account_resolution_reason"] = reason
    if reason.startswith("NEEDS_VERIFIED_BALANCE_"):
        updated["source_account_preference"] = (
            "highest_balance" if reason.endswith("_MAX") else "lowest_balance"
        )
        updated["stage"] = "RESOLVING_SOURCE_ACCOUNT"
        return "NEEDS_VERIFIED_BALANCE", updated
    if reason in {"ACCOUNT_REQUIRED", "NO_COMPATIBLE_ACCOUNT"}:
        # Do not consume arbitrary conversational text (e.g. "ok" or a new
        # question) as an account selection. The normal agent may answer it while
        # this typed workflow remains pending.
        return "UNRELATED", updated
    updated["source_account_candidates"] = accounts
    updated["stage"] = "AWAITING_SOURCE_ACCOUNT"
    return "AWAITING_SOURCE_ACCOUNT", updated


def apply_verified_account_ranking(
    workflow: dict[str, Any],
    context: dict[str, Any],
    verified_accounts: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Rank only authenticated, currency-compatible account results.

    The preference is a typed workflow value normalized from the registered
    OCCF preference vocabulary. This function never parses customer language
    and never ranks browser-supplied balances.
    """
    updated = dict(workflow)
    preference = str(updated.get("source_account_preference") or "").lower()
    if preference not in {"highest_balance", "lowest_balance"}:
        updated["stage"] = "AWAITING_SOURCE_ACCOUNT"
        updated["account_resolution_reason"] = "SOURCE_ACCOUNT_PREFERENCE_UNSUPPORTED"
        return "AWAITING_SOURCE_ACCOUNT", updated

    currency = str(updated.get("currency") or "").upper()
    # These rows come from the server-authenticated MCP capability. The
    # frontend context is not a necessary authority source for ranking; it may
    # be absent on a later conversational turn. Currency and active status are
    # checked against the trusted result itself.
    eligible = [
        dict(item)
        for item in (verified_accounts or [])
        if isinstance(item, dict)
        and str(item.get("currency", "")).upper() == currency
        and str(item.get("status", "ACTIVE")).upper() == "ACTIVE"
    ]
    trusted = [
        item
        for item in eligible
        if isinstance(item.get("balance_minor"), int)
        and not isinstance(item.get("balance_minor"), bool)
    ]
    if not trusted:
        updated["stage"] = "AWAITING_SOURCE_ACCOUNT"
        updated["account_resolution_reason"] = "VERIFIED_ACCOUNT_BALANCES_UNAVAILABLE"
        updated["source_account_candidates"] = eligible
        return "AWAITING_SOURCE_ACCOUNT", updated

    target = (max if preference == "highest_balance" else min)(
        int(item["balance_minor"]) for item in trusted
    )
    winners = [item for item in trusted if int(item["balance_minor"]) == target]
    if len(winners) != 1:
        updated["stage"] = "AWAITING_SOURCE_ACCOUNT"
        updated["account_resolution_reason"] = "BALANCE_RANKING_TIED"
        updated["source_account_candidates"] = eligible
        updated.pop("source_account_id", None)
        return "AWAITING_SOURCE_ACCOUNT", updated

    updated["source_account_id"] = str(winners[0]["account_id"])
    updated["account_resolution_reason"] = (
        "VERIFIED_HIGHEST_BALANCE" if preference == "highest_balance" else "VERIFIED_LOWEST_BALANCE"
    )
    updated["stage"] = "READY_FOR_DRAFT"
    return "READY_FOR_DRAFT", updated


def apply_verified_source_account_selection(
    workflow: dict[str, Any],
    verified_accounts: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Check the selected source against an authenticated MCP account read."""
    updated = dict(workflow)
    selected_id = str(updated.get("source_account_id") or "")
    currency = str(updated.get("currency") or "").upper()
    selected = next(
        (
            item
            for item in (verified_accounts or [])
            if isinstance(item, dict)
            and str(item.get("account_id", "")) == selected_id
            and str(item.get("currency", "")).upper() == currency
            and str(item.get("status", "ACTIVE")).upper() == "ACTIVE"
        ),
        None,
    )
    if not selected:
        updated["source_account_id"] = ""
        updated["stage"] = "AWAITING_SOURCE_ACCOUNT"
        updated["account_resolution_reason"] = "SELECTED_ACCOUNT_NOT_VERIFIED"
        updated["source_account_candidates"] = [
            dict(item)
            for item in (verified_accounts or [])
            if isinstance(item, dict)
            and str(item.get("currency", "")).upper() == currency
            and str(item.get("status", "ACTIVE")).upper() == "ACTIVE"
        ]
        return "AWAITING_SOURCE_ACCOUNT", updated
    updated["stage"] = "READY_FOR_DRAFT"
    updated["account_resolution_reason"] = "VERIFIED_SOURCE_ACCOUNT"
    return "READY_FOR_DRAFT", updated


def mark_draft_requested(workflow: dict[str, Any]) -> dict[str, Any]:
    """Mark the transfer workflow as requesting a frontend draft-opening action."""
    updated = dict(workflow)
    updated["stage"] = "DRAFT_REQUESTED"
    if not updated.get("action_id"):
        updated["action_id"] = f"act-{uuid.uuid4().hex}"
    return updated


def apply_frontend_draft_result(workflow: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Apply a correlated frontend draft receipt and advance the workflow to its terminal draft state."""
    updated = dict(workflow)
    status = str(result.get("status", "")).lower()
    if status == "draft_opened":
        # The browser receipt must acknowledge the exact action requested by
        # this workflow. A stale or mismatched receipt cannot advance state.
        if result.get("workflow_id") != workflow.get("workflow_id") or result.get(
            "action_id"
        ) != workflow.get("action_id"):
            status = "rejected"
    updated["last_action_status"] = status
    if status == "draft_opened":
        updated["stage"] = "DRAFT_OPENED"
    elif status in {"rejected", ""}:
        updated["stage"] = "DRAFT_REJECTED"
    return updated


def apply_frontend_dismissal(
    workflow: dict[str, Any], draft_state: dict[str, Any] | None
) -> dict[str, Any]:
    """Record dismissal of the currently correlated frontend transfer draft."""
    updated = dict(workflow)
    if not isinstance(draft_state, dict):
        return updated
    same_workflow = draft_state.get("workflow_id") == updated.get("workflow_id")
    same_action = not draft_state.get("action_id") or draft_state.get("action_id") == updated.get(
        "action_id"
    )
    status = str(draft_state.get("status", "")).lower()
    if same_workflow and same_action and status in {"dismissed", "closed"}:
        updated["stage"] = "DISMISSED"
        updated["last_action_status"] = "dismissed"
    elif same_workflow and same_action and status == "accepted":
        updated["stage"] = "COMPLETED"
        updated["last_action_status"] = "accepted"
    return updated


def account_choice_prompt(workflow: dict[str, Any]) -> str:
    """Render the source-account choices required by the current transfer stage."""
    accounts = workflow.get("source_account_candidates") or []
    source_query = str(workflow.get("source_account_query") or "").strip()
    contact_name = (workflow.get("contact") or {}).get("full_name", "the saved recipient")
    if not accounts:
        prefix = (
            f"I couldn't find a compatible account matching **{source_query}**. "
            if source_query
            else ""
        )
        return f"{prefix}I found **{contact_name}**, but no {workflow.get('currency', '')} source account is available. No transfer draft was opened."
    rows = "\n".join(
        f"{index}. **{item.get('account_name', 'Account')}** · {item.get('currency', workflow.get('currency', ''))}"
        for index, item in enumerate(accounts, start=1)
    )
    source_note = (
        f"I couldn't match your requested source **{source_query}** to one available {workflow.get('currency', '')} account. "
        if source_query
        else ""
    )
    return (
        f"{source_note}I found **{contact_name}**. "
        f"Which {workflow.get('currency', '')} account should fund the transfer of {workflow.get('amount', '')} {workflow.get('currency', '')}?\n\n"
        f"{rows}\n\nReply with the account name, a short name such as **Everyday**, or the option number. "
        "You can also say which account has the highest balance. I will compare verified account balances. No transfer draft has been opened."
    )


def contact_choice_prompt(workflow: dict[str, Any]) -> str:
    """Render saved-contact choices with masked account references."""
    return (
        f"I found more than one saved contact matching **{workflow.get('beneficiary_query', 'that name')}**. "
        "Which person did you mean?\n\n"
        + format_contact_choices(workflow.get("contact_candidates") or [])
        + "\n\nReply with the full name, a unique surname, bank name, masked account reference or option number. "
        "You can include the source account in the same reply. No transfer draft has been opened."
    )
