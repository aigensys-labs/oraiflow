"""Small deterministic redaction layer for text sent to model providers."""
from __future__ import annotations

import json
import re

# ISO 13616 IBAN lengths for common European markets. Restricting by country
# and exact length avoids greedily consuming ordinary words after a digit run.
_IBAN_LENGTHS = {
    "AT": 20, "BE": 16, "BG": 22, "CH": 21, "CY": 28, "CZ": 24,
    "DE": 22, "DK": 18, "EE": 20, "ES": 24, "FI": 18, "FR": 27,
    "GB": 22, "GR": 27, "HR": 21, "HU": 28, "IE": 22, "IS": 26,
    "IT": 27, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MT": 31,
    "NL": 18, "NO": 15, "PL": 28, "PT": 25, "RO": 24, "SE": 24,
    "SI": 19, "SK": 24,
}


def _build_iban_pattern(country: str, length: int) -> re.Pattern[str]:
    remaining = length - 4
    pieces = [rf"{country}\d{{2}}"]
    while remaining > 4:
        pieces.append(r"[A-Z0-9]{4} ?")
        remaining -= 4
    pieces.append(rf"[A-Z0-9]{{{remaining}}}")
    return re.compile(r"\b" + "".join(pieces) + r"\b", re.IGNORECASE)


_IBAN_PATTERNS = tuple(_build_iban_pattern(code, length) for code, length in _IBAN_LENGTHS.items())
_PAN = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|client[_ -]?secret)\b\s*[:=]\s*[^\s,;]+"
)


def redact_for_model(value: str) -> str:
    """Return a recursively redacted representation suitable for model context."""
    text = str(value)
    for pattern in _IBAN_PATTERNS:
        text = pattern.sub("[REDACTED_IBAN]", text)
    text = _PAN.sub("[REDACTED_PAYMENT_CARD_NUMBER]", text)
    text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    return text


def sanitize_mcp_tool_output(value: str) -> str:
    """Redact sensitive identifiers and label MCP results as untrusted data.

    Transaction descriptions and account metadata can contain customer- or
    third-party-supplied text. The envelope is a model-facing hint, not a
    substitute for tool authorization or prompt-injection defenses.
    """
    try:
        data = json.loads(str(value))
    except (TypeError, ValueError):
        data = str(value)
    envelope = json.dumps(
        {"trust_class": "untrusted_banking_data", "data": data},
        ensure_ascii=False, separators=(",", ":"),
    )
    return redact_for_model(envelope)
