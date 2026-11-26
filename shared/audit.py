"""Append-only, hash-linked audit evidence helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

_LOCK_KEY = 730301


def append_audit_event(
    conn,
    actor_id: str,
    event_type: str,
    subject_id: str | None,
    detail: dict[str, Any],
    tenant_id: str | None = None,
) -> int:
    """Append one audit event to a per-database SHA-256 hash chain.

    The advisory transaction lock serializes concurrent appenders. This is
    tamper-evident application evidence, not a substitute for external WORM
    storage, signed attestations or database-admin separation in production.
    """
    tenant_key = tenant_id or "unscoped"
    conn.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key, hashtext(:tenant))"),
        {"lock_key": _LOCK_KEY, "tenant": tenant_key},
    )
    conn.execute(
        text("""
        INSERT INTO audit_chain_state(tenant_id, head_event_id, head_hash)
        VALUES (:tenant, NULL, REPEAT('0', 64))
        ON CONFLICT (tenant_id) DO NOTHING
    """),
        {"tenant": tenant_key},
    )
    state = conn.execute(
        text("SELECT head_hash FROM audit_chain_state WHERE tenant_id=:tenant FOR UPDATE"),
        {"tenant": tenant_key},
    ).first()
    previous_hash = state[0] if state else "0" * 64
    occurred_at = datetime.now(timezone.utc)
    event_id = conn.execute(
        text("SELECT nextval(pg_get_serial_sequence('audit_events','event_id'))")
    ).scalar_one()
    payload = {
        "event_id": int(event_id),
        "actor_id": actor_id,
        "tenant_id": tenant_id,
        "event_type": event_type,
        "subject_id": subject_id,
        "detail": detail,
        "occurred_at": occurred_at.isoformat(),
        "previous_hash": previous_hash,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    event_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    conn.execute(
        text("""
        INSERT INTO audit_events(event_id, actor_id, tenant_id, event_type, subject_id,
                                 detail, occurred_at, previous_hash, event_hash)
        VALUES (:event_id, :actor, :tenant, :event, :subject, CAST(:detail AS jsonb),
                :occurred_at, :previous_hash, :event_hash)
    """),
        {
            "event_id": event_id,
            "actor": actor_id,
            "tenant": tenant_id,
            "event": event_type,
            "subject": subject_id,
            "detail": json.dumps(detail, ensure_ascii=False, default=str),
            "occurred_at": occurred_at,
            "previous_hash": previous_hash,
            "event_hash": event_hash,
        },
    )
    conn.execute(
        text("""
        INSERT INTO audit_chain_state(tenant_id, head_event_id, head_hash)
        VALUES (:tenant, :event_id, :event_hash)
        ON CONFLICT (tenant_id) DO UPDATE
          SET head_event_id=EXCLUDED.head_event_id, head_hash=EXCLUDED.head_hash
    """),
        {"tenant": tenant_key, "event_id": event_id, "event_hash": event_hash},
    )
    return int(event_id)
