"""Canonical action-preview signing helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime
from typing import Any


def canonical_preview_payload(row: dict[str, Any]) -> bytes:
    """Serialize only material approval fields in a stable, unambiguous form."""
    fields = {
        "preview_id": str(row["preview_id"]),
        "user_id": str(row["user_id"]),
        "tenant_id": str(row["tenant_id"]),
        "from_account_id": str(row["from_account_id"]),
        "beneficiary": str(row["beneficiary"]),
        "beneficiary_contact_id": str(row.get("beneficiary_contact_id") or ""),
        "amount_minor": int(row["amount_minor"]),
        "currency": str(row["currency"]).strip().upper(),
        "fee_minor": int(row["fee_minor"]),
        "total_minor": int(row["total_minor"]),
        "policy_details": row.get("policy_details") or {},
        "policy_version": str(row["policy_version"]),
        "policy_decision": str(row["policy_decision"]),
        "expires_at": (
            row["expires_at"].isoformat()
            if isinstance(row["expires_at"], datetime)
            else str(row["expires_at"])
        ),
    }
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def preview_digest(row: dict[str, Any], secret: str | None = None) -> str:
    """Create the keyed integrity digest for a synthetic transfer preview."""
    key = (
        secret or os.getenv("PREVIEW_SIGNING_SECRET", "local-preview-signing-secret-change-me")
    ).encode("utf-8")
    return hmac.new(key, canonical_preview_payload(row), hashlib.sha256).hexdigest()


def verify_preview_digest(
    row: dict[str, Any], supplied_digest: str, secret: str | None = None
) -> bool:
    """Verify the supplied digest against the stored synthetic transfer preview."""
    return hmac.compare_digest(preview_digest(row, secret), str(supplied_digest))
