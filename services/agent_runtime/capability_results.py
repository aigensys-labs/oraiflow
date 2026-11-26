"""Validated MCP result inspection shared by workflow handlers.

This module intentionally depends only on OCCF contracts and message-like
objects. It does not import LangChain or graph runtime types, so result-state
classification remains independently testable.
"""
from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from datetime import date
from typing import Any

from shared.contracts.registry import (
    ContractViolation,
    get_capability,
    validate_capability_output,
    validate_result_envelope,
)
from shared.pending_task import message_type, message_value

logger = logging.getLogger("oraiflow.agent_runtime.capability_results")
_MONTH_ABBREVIATIONS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def capability_result_diagnostic(
    messages: list[Any], capability_id: str, *, trace_id: str = "-",
) -> tuple[bool, dict[str, Any] | None, str | None, str | None, str | None]:
    """Return a validated current-turn result and precise diagnostic fields.

    Tuple fields are: result-seen, validated-envelope, reason, outcome,
    connector-error-code. Raw payloads and customer data are never logged.
    Classification is diagnostic only: every accepted result still passes the
    canonical OCCF ``validate_result_envelope`` validator.
    """
    contract = get_capability(capability_id)
    tool_name = contract["name"]
    latest_user_index = max(
        (index for index, message in enumerate(messages or []) if message_type(message) == "human"),
        default=-1,
    )
    for message in reversed((messages or [])[latest_user_index + 1:]):
        if message_type(message) != "tool" or message_value(message, "name", "") != tool_name:
            continue

        try:
            envelope = json.loads(str(message_value(message, "content", "")))
        except (TypeError, ValueError):
            return _reject(trace_id, capability_id, "INVALID_RESULT_ENVELOPE")
        if not isinstance(envelope, dict):
            return _reject(trace_id, capability_id, "INVALID_RESULT_ENVELOPE")

        raw_outcome = str(envelope.get("outcome", "UNKNOWN")).upper()
        allowed_outcomes = {
            "SUCCESS", "EMPTY", "PARTIAL", "INVALID_INPUT", "REJECTED",
            "UNAVAILABLE", "INVALID_RESPONSE",
        }
        outcome = raw_outcome if raw_outcome in allowed_outcomes else "UNKNOWN"
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        raw_error_code = str(error.get("code", ""))
        error_code = re.sub(r"[^A-Za-z0-9_.:-]", "_", raw_error_code)[:80] or None

        # Classify common boundary failures before the canonical validator so
        # operators can distinguish an invalid envelope from an invalid data
        # payload or an incorrect trust label. These checks do not accept data.
        if envelope.get("schema_version") != "1.0.0" or envelope.get("capability") != capability_id:
            return _reject(trace_id, capability_id, "INVALID_RESULT_ENVELOPE", outcome, error_code)

        if outcome == "UNKNOWN":
            return _reject(trace_id, capability_id, "INVALID_RESULT_ENVELOPE", outcome, error_code)

        successful = outcome in {"SUCCESS", "EMPTY", "PARTIAL"}
        failed = outcome in {"INVALID_INPUT", "REJECTED", "UNAVAILABLE", "INVALID_RESPONSE"}
        if successful and envelope.get("trust_class") != "untrusted_banking_data":
            return _reject(trace_id, capability_id, "TRUST_CLASS_MISMATCH", outcome, error_code)
        if failed and envelope.get("trust_class") != "connector_error":
            return _reject(trace_id, capability_id, "TRUST_CLASS_MISMATCH", outcome, error_code)

        if successful:
            data = envelope.get("data")
            output_schema = contract.get("output_schema", {})
            if output_schema.get("type") == "array":
                if not isinstance(data, list):
                    return _reject(trace_id, capability_id, "INVALID_DATA_SHAPE", outcome, error_code)
                if any(not isinstance(item, dict) for item in data):
                    return _reject(trace_id, capability_id, "INVALID_DATA_RECORD", outcome, error_code)
            try:
                validate_capability_output(capability_id, data)
            except ContractViolation:
                return _reject(trace_id, capability_id, "INVALID_DATA_SCHEMA", outcome, error_code)

        try:
            validate_result_envelope(capability_id, envelope)
        except ContractViolation as exc:
            if exc.section == "output":
                reason = "INVALID_DATA_SCHEMA"
            elif exc.section == "evidence":
                reason = "INVALID_EVIDENCE"
            elif "trace identifier" in exc.message or "source or scope" in exc.message:
                reason = "INVALID_EVIDENCE"
            else:
                reason = "INVALID_RESULT_ENVELOPE"
            return _reject(trace_id, capability_id, reason, outcome, error_code)
        except (TypeError, ValueError):
            return _reject(trace_id, capability_id, "INVALID_RESULT_ENVELOPE", outcome, error_code)

        if not successful:
            logger.warning(
                "capability_result_not_success trace_id=%s capability=%s outcome=%s error_code=%s",
                trace_id, capability_id, outcome, error_code or "unknown",
            )
            return True, None, f"OUTCOME_{outcome}", outcome, error_code
        return True, envelope, None, outcome, None
    return False, None, "RESULT_NOT_FOUND", None, None


def _reject(
    trace_id: str,
    capability_id: str,
    reason: str,
    outcome: str | None = None,
    error_code: str | None = None,
) -> tuple[bool, None, str, str | None, str | None]:
    logger.warning(
        "capability_result_rejected trace_id=%s capability=%s reason=%s outcome=%s error_code=%s",
        trace_id, capability_id, reason, outcome or "unknown", error_code or "unknown",
    )
    return True, None, reason, outcome, error_code


def verified_list_result_diagnostic(
    messages: list[Any], capability_id: str, *, trace_id: str = "-",
) -> tuple[bool, list[dict[str, Any]] | None, str | None, str | None, str | None]:
    """Validate a list-valued result without collapsing connector/schema failures."""
    seen, envelope, reason, outcome, error_code = capability_result_diagnostic(
        messages, capability_id, trace_id=trace_id,
    )
    if not seen or envelope is None:
        return seen, None, reason, outcome, error_code
    data = envelope.get("data")
    if not isinstance(data, list):
        return True, None, "INVALID_DATA_SHAPE", outcome, None
    if any(not isinstance(item, dict) for item in data):
        return True, None, "INVALID_DATA_RECORD", outcome, None
    return True, data, None, outcome, None



def _format_minor_amount(value: Any, currency: str) -> str:
    """Format the platform's fixed two-decimal minor units without floats."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Expected a validated integer minor-unit amount")
    return f"{currency} {Decimal(value) / Decimal(100):,.2f}"


def _human_period(data: dict[str, Any]) -> str:
    start = data.get("period_start")
    end = data.get("period_end")
    if isinstance(start, str) and isinstance(end, str):
        try:
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)
            start_text = start_date.strftime("%d %b %Y").lstrip("0")
            end_text = end_date.strftime("%d %b %Y").lstrip("0")
            return f"from {start_text} to {end_text}"
        except ValueError:
            pass
    return str(data.get("period") or "the selected period").replace("_", " ")


def _display_transaction_group_value(value: str, group_by: str) -> str:
    """Format the capability's canonical YYYY-MM month key for customers."""
    if group_by == "month" and re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", value):
        year, month = value.split("-", 1)
        return f"{_MONTH_ABBREVIATIONS[int(month) - 1]} {year}"
    return value


def render_transaction_analysis_summary(data: dict[str, Any]) -> str:
    """Render a validated transaction-analysis result without relying on an LLM.

    This is used only when the response composer returns generic prose. It
    reports deterministic aggregates exactly as returned by the capability,
    keeps currencies separate and treats recurring-inflow labels as screening
    signals rather than factual salary/employer assertions.
    """
    if not isinstance(data, dict):
        raise ValueError("Transaction analysis data must be an object")
    count = data.get("transaction_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("Transaction count is invalid")
    period = _human_period(data)
    mode = str(data.get("analysis_mode") or "activity")
    flow = str(data.get("flow") or "all")
    measure = str(data.get("measure") or "sum")
    group_by = str(data.get("group_by") or "none")
    summaries = data.get("summary_by_currency") or []
    groups = data.get("groups") or []
    transactions = data.get("transactions") or []

    merchant_query = str(data.get("merchant_query") or "").strip()
    category_query = str(data.get("category_query") or "").strip()
    filter_label = merchant_query or category_query
    if count == 0:
        if filter_label:
            return f"I found no booked transactions matching {filter_label} {period}."
        return f"I found no matching booked transactions {period}."

    if flow == "outflows":
        activity_label = f"{filter_label} spending" if filter_label else "spending"
    elif flow == "inflows":
        activity_label = "incoming payments"
    else:
        activity_label = "net recorded activity"

    if measure == "count":
        headline = f"I found {count} booked transaction{'s' if count != 1 else ''} {period}."
    elif measure == "transactions":
        headline = f"I found {count} booked transaction{'s' if count != 1 else ''} {period}:"
        lines = []
        for row in transactions[:12]:
            if not isinstance(row, dict):
                continue
            description = str(row.get("description") or "Recorded transaction")[:120]
            currency = str(row.get("currency") or "")
            amount_minor = row.get("amount_minor")
            if isinstance(amount_minor, int) and not isinstance(amount_minor, bool) and currency:
                amount = _format_minor_amount(abs(amount_minor) if flow in {"outflows", "inflows"} else amount_minor, currency)
            else:
                amount = "amount unavailable"
            occurred = str(row.get("occurred_at") or "date unavailable")[:10]
            lines.append(f"- {occurred}: {description} — {amount}")
        if lines:
            headline += "\n" + "\n".join(lines)
        if len(transactions) > 12:
            headline += "\nOnly the first 12 matching records are shown here."
    else:
        amount_key = "average_minor" if measure == "average" else "sum_minor"
        metric_label = "average per transaction" if measure == "average" else activity_label
        amounts = []
        for item in summaries:
            if not isinstance(item, dict):
                continue
            currency = str(item.get("currency") or "").strip().upper()
            value = item.get(amount_key)
            if currency and isinstance(value, int) and not isinstance(value, bool):
                amounts.append(_format_minor_amount(value, currency))
        if not amounts:
            return f"I found {count} booked transactions {period}, but no validated currency aggregate was available to display."
        if mode == "recurring_inflows":
            headline = (
                f"I found {count} incoming-payment transactions {period}, totalling "
                f"{'; '.join(amounts)}."
            )
        elif measure == "average":
            headline = f"The average {activity_label} per booked transaction {period} was {'; '.join(amounts)} across {count} transactions."
        else:
            headline = f"Your {activity_label} {period} totalled {'; '.join(amounts)} across {count} booked transaction{'s' if count != 1 else ''}."

    detail_lines = []
    if group_by != "none" and groups:
        for item in groups[:8]:
            if not isinstance(item, dict):
                continue
            value = str(item.get("group_value") or "Unspecified")[:100]
            value = _display_transaction_group_value(value, group_by)
            currency = str(item.get("currency") or "").strip().upper()
            amount_minor = item.get("sum_minor")
            group_count = item.get("transaction_count")
            if currency and isinstance(amount_minor, int) and isinstance(group_count, int):
                amount = _format_minor_amount(amount_minor, currency)
                detail_lines.append(f"- {value}: {amount} across {group_count} transaction{'s' if group_count != 1 else ''}")
        if detail_lines:
            headline += "\nBreakdown:\n" + "\n".join(detail_lines)

    if mode == "recurring_inflows":
        recurring = [
            item for item in groups
            if isinstance(item, dict) and item.get("potentially_recurring") is True
        ]
        if recurring:
            labels = ", ".join(str(item.get("group_value") or "a recorded description")[:100] for item in recurring[:5])
            headline += f"\nPotentially recurring pattern(s): {labels}. This is a screening signal based on recorded repetitions, not proof of salary, employment or payer identity."
        else:
            headline += "\nThe recorded history does not establish a potentially recurring incoming-payment pattern."
    if data.get("truncated") is True:
        headline += "\nThis is a partial result because the registered result limit was reached."
    return headline



def render_saved_contact_lookup_summary(data: list[dict[str, Any]]) -> str:
    """Render actual saved-contact records without exposing unmasked account data."""
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError("Saved-contact result must be a list of records")
    active = [item for item in data if str(item.get("status", "")).upper() == "ACTIVE"]
    if not active:
        return "I couldn't find an active saved contact matching that name."

    lines: list[str] = []
    for item in active[:10]:
        name = str(item.get("full_name") or "Saved contact")[:120]
        bank = str(item.get("bank_name") or "Bank not listed")[:100]
        currency = str(item.get("currency") or "").upper()
        masked = str(item.get("account_reference_masked") or "")[:40]
        # The schema labels this field as masked, but fail closed if the value
        # does not visibly contain a redaction marker.
        masked_detail = f" · {masked}" if re.search(r"[•·*xX#]{2,}", masked) else ""
        currency_detail = f" · {currency}" if re.fullmatch(r"[A-Z]{3}", currency) else ""
        lines.append(f"- **{name}** — {bank}{masked_detail}{currency_detail}")

    count = len(active)
    heading = (
        "I found one active saved contact:" if count == 1
        else f"I found {count} active saved contacts matching that name:"
    )
    if len(active) > 10:
        heading += " (showing the first 10)"
    return heading + "\n" + "\n".join(lines)


def verified_saved_contact_lookup_summary(
    messages: list[Any] | tuple[Any, ...], *, trace_id: str | None = None,
) -> str | None:
    """Return contact details only from a validated current-turn MCP result."""
    seen, envelope, reason, _outcome, _error_code = capability_result_diagnostic(
        list(messages or []), "banking.contacts.search", trace_id=trace_id,
    )
    if not seen or envelope is None or reason is not None:
        return None
    try:
        data = envelope.get("data")
        return render_saved_contact_lookup_summary(data if isinstance(data, list) else [])
    except (TypeError, ValueError):
        return None


def verified_transaction_analysis_summary(
    messages: list[Any] | tuple[Any, ...], *, trace_id: str | None = None,
) -> str | None:
    """Return a deterministic summary only from a validated current-turn result."""
    seen, envelope, reason, _outcome, _error_code = capability_result_diagnostic(
        list(messages or []), "banking.transactions.analyze", trace_id=trace_id,
    )
    if not seen or envelope is None or reason is not None:
        return None
    try:
        return render_transaction_analysis_summary(envelope.get("data") or {})
    except (TypeError, ValueError):
        return None
