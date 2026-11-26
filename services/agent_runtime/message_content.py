"""Null-safe conversion of LangChain message content to customer text."""

from __future__ import annotations

from typing import Any


def message_content_to_text(content: Any) -> str:
    """Return text blocks only; never stringify ``None`` as customer-visible text.

    LangChain content can be a plain string or a list of multimodal dictionaries.
    Non-text blocks and dictionaries whose ``text`` value is null are ignored.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                if part:
                    parts.append(part)
            elif isinstance(part, dict):
                text_value = part.get("text")
                if isinstance(text_value, str) and text_value:
                    parts.append(text_value)
        return "".join(parts)
    return ""
