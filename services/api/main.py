"""FastAPI application for demo workspace reads and explicitly authorized commands."""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import hmac
from decimal import Decimal

from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
import httpx
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import text

from shared.database import ensure_runtime_schema, get_engine
from shared.version import __version__
from shared.money import amount_minor as _amount_minor
from shared.audit import append_audit_event
from shared.identity import current_demo_identity, require_capability
from shared.financial_health import calculate_recorded_financial_health
from shared.demo_configuration import demo_loan_enabled
from shared.previews import preview_digest, verify_preview_digest
from shared.contracts.registry import (
    ContractViolation,
    get_kernel_operation,
    validate_kernel_input,
    validate_kernel_output,
)

logger = logging.getLogger("oraiflow.api")
POLICY_VERSION = os.getenv("DEMO_POLICY_VERSION", "demo-transfer-policy-v1")
PREVIEW_TTL_MINUTES = max(1, min(int(os.getenv("PREVIEW_TTL_MINUTES", "5")), 30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Additive, safe for both freshly-created and pre-existing v0.1 volumes.
    """Initialize and release application-scoped API resources."""
    ensure_runtime_schema()
    yield


app = FastAPI(
    title="ORAIflow Core API",
    description="Local fintech reference API. Synthetic financial data only.",
    version=__version__,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("WEB_ORIGIN", "http://localhost:3000")],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Idempotency-Key"],
)


class ChatRequest(BaseModel):
    """Validated request body for the demo chat endpoint."""

    message: str = Field(min_length=1, max_length=4000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=10)


class ProfileUpdateRequest(BaseModel):
    """Validated profile fields accepted by the demo profile endpoint."""

    model_config = {"extra": "forbid"}
    display_name: str = Field(min_length=2, max_length=80)
    preferred_name: str = Field(default="", max_length=80)
    email: str = Field(default="", max_length=254)
    phone: str = Field(default="", max_length=40)
    preferred_currency: str = Field(default="EUR", min_length=3, max_length=3)
    language: str = Field(default="en", min_length=2, max_length=10)


class TransferPreviewRequest(BaseModel):
    """Validated inputs for a non-executing transfer preview."""

    model_config = {"extra": "forbid"}
    from_account_id: str = Field(default="acc-001", min_length=1, max_length=80)
    beneficiary: str = Field(min_length=2, max_length=120)
    beneficiary_contact_id: str | None = Field(default=None, min_length=8, max_length=64)
    amount: str = Field(min_length=1, max_length=30)
    currency: str = Field(default="EUR", min_length=3, max_length=3)


class TransferConfirmRequest(BaseModel):
    """Customer confirmation payload for a previously validated demo transfer."""

    model_config = {"extra": "forbid"}
    preview_id: str = Field(min_length=36, max_length=64)
    payload_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=120)
    customer_confirmed: bool
    sca_code: str = Field(min_length=4, max_length=12)


def _require(capability: str):
    identity = current_demo_identity()
    try:
        require_capability(identity, capability)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Capability not permitted") from exc
    return identity


def _require_kernel_operation(operation_id: str):
    """Enforce the capability declared by a Banking Kernel operation contract."""
    operation = get_kernel_operation(operation_id)
    if (
        operation.get("authority") != "banking_kernel"
        or operation.get("agent_callable") is not False
    ):
        raise RuntimeError(f"Invalid Banking Kernel authority declaration: {operation_id}")
    return _require(operation["required_capability"])


def _validate_kernel_request(operation_id: str, payload: dict) -> None:
    try:
        validate_kernel_input(operation_id, payload)
    except ContractViolation as exc:
        raise HTTPException(
            status_code=422, detail="Request does not match the declared banking operation contract"
        ) from exc


def _validate_kernel_response(operation_id: str, payload: dict) -> dict:
    try:
        validate_kernel_output(operation_id, payload)
        return payload
    except ContractViolation as exc:
        logger.exception("kernel_response_contract_violation operation=%s", operation_id)
        raise HTTPException(
            status_code=500,
            detail="The Banking Kernel returned a response that failed its declared contract",
        ) from exc


def _evaluate_transfer(
    conn,
    from_account_id: str,
    beneficiary_value: str,
    amount_value: str,
    currency_value: str,
    identity,
    lock_account: bool = False,
    beneficiary_contact_id: str | None = None,
) -> dict:
    """Deterministic server-side transfer policy. Never trusts model/client totals."""
    amount_minor = _amount_minor(amount_value)
    currency = currency_value.upper().strip()
    if currency not in {"EUR", "USD", "GBP"}:
        raise HTTPException(status_code=422, detail="Unsupported demo currency")
    beneficiary = beneficiary_value.strip()
    beneficiary_contact = None
    if beneficiary_contact_id:
        beneficiary_contact = (
            conn.execute(
                text("""
            SELECT contact_id, full_name, bank_name, account_reference_masked, currency, status
              FROM contacts
             WHERE contact_id=:contact_id AND owner_id=:owner AND tenant_id=:tenant
        """),
                {
                    "contact_id": beneficiary_contact_id,
                    "owner": identity.subject,
                    "tenant": identity.tenant_id,
                },
            )
            .mappings()
            .first()
        )
        if not beneficiary_contact or beneficiary_contact["status"] != "ACTIVE":
            raise HTTPException(
                status_code=422, detail="The selected saved contact is no longer available"
            )
        if " ".join(beneficiary.casefold().split()) != " ".join(
            beneficiary_contact["full_name"].casefold().split()
        ):
            raise HTTPException(
                status_code=409,
                detail="The beneficiary name does not match the selected saved contact",
            )
        if str(beneficiary_contact["currency"]).strip().upper() != currency:
            raise HTTPException(
                status_code=422,
                detail="The selected saved contact does not support the requested transfer currency",
            )
        beneficiary = beneficiary_contact["full_name"]
    if len(beneficiary) < 2:
        raise HTTPException(status_code=422, detail="Enter a beneficiary name")
    lock = " FOR UPDATE" if lock_account else ""
    account = (
        conn.execute(
            text(f"""
        SELECT * FROM accounts
         WHERE account_id=:id AND owner_id=:owner AND tenant_id=:tenant{lock}
    """),
            {"id": from_account_id, "owner": identity.subject, "tenant": identity.tenant_id},
        )
        .mappings()
        .first()
    )
    if not account:
        raise HTTPException(status_code=404, detail="Account not found or not accessible")
    if account["status"] != "ACTIVE":
        raise HTTPException(status_code=422, detail="The selected account is not active")
    max_minor = int(Decimal(os.getenv("DEMO_MAX_TRANSFER", "1000.00")) * 100)
    if amount_minor > max_minor:
        raise HTTPException(
            status_code=422, detail=f"Demo policy limit is {max_minor / 100:,.2f} {currency}"
        )
    if account["currency"].strip() != currency:
        raise HTTPException(
            status_code=422, detail="Transfer currency must match the selected account"
        )
    if int(account["balance_minor"]) < amount_minor:
        raise HTTPException(status_code=422, detail="Insufficient available balance")
    return {
        "from_account_id": account["account_id"],
        "from_account_name": account["account_name"],
        "from_masked_number": account["masked_number"],
        "beneficiary": beneficiary,
        "beneficiary_contact_id": (
            beneficiary_contact["contact_id"] if beneficiary_contact else None
        ),
        "beneficiary_bank_name": beneficiary_contact["bank_name"] if beneficiary_contact else None,
        "beneficiary_account_reference_masked": (
            beneficiary_contact["account_reference_masked"] if beneficiary_contact else None
        ),
        "amount_minor": amount_minor,
        "amount": f"{amount_minor / 100:,.2f}",
        "currency": currency,
        "fee_minor": 0,
        "total_minor": amount_minor,
        "policy_version": POLICY_VERSION,
        "policy_decision": "APPROVED",
        "available_balance_minor": int(account["balance_minor"]),
        "max_transfer_minor": max_minor,
        "demo": True,
    }


def _preview_signing_row(
    preview_id: str, identity, transfer: dict, expires_at: datetime, policy_details: dict
) -> dict:
    return {
        "preview_id": preview_id,
        "user_id": identity.subject,
        "tenant_id": identity.tenant_id,
        "from_account_id": transfer["from_account_id"],
        "beneficiary": transfer["beneficiary"],
        "beneficiary_contact_id": transfer.get("beneficiary_contact_id"),
        "amount_minor": transfer["amount_minor"],
        "currency": transfer["currency"],
        "fee_minor": transfer["fee_minor"],
        "total_minor": transfer["total_minor"],
        "policy_version": transfer["policy_version"],
        "policy_decision": transfer["policy_decision"],
        "policy_details": policy_details,
        "expires_at": expires_at,
    }


def _public_preview(transfer: dict, preview_id: str, digest: str, expires_at: datetime) -> dict:
    return {
        **transfer,
        "preview_id": preview_id,
        "payload_digest": digest,
        "expires_at": expires_at.isoformat(),
        "status": "PREVIEWED",
        "approval_required": True,
        "demo": True,
    }


@app.get("/healthz")
async def healthz():
    """Report API process and dependency health."""
    return {"status": "ok", "service": "api", "version": __version__}


@app.get("/api/config")
async def config():
    """Return public, non-secret workspace configuration."""
    runtime_url = os.getenv("AGENT_RUNTIME_URL", "http://agent-runtime:8101").rstrip("/")
    internal_token = os.getenv(
        "AGENT_RUNTIME_INTERNAL_TOKEN", "local-agent-runtime-token-change-me"
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{runtime_url}/internal/config",
                headers={"Authorization": f"Bearer {internal_token}"},
            )
            response.raise_for_status()
            runtime = response.json()
    except httpx.HTTPError as exc:
        logger.warning("Could not retrieve Agent Runtime configuration: %s", type(exc).__name__)
        runtime = {
            "llm_provider": "unavailable",
            "llm_model": "—",
            "llm_configured": False,
            "llm_external_data_approved": False,
            "model_data_policy": "unavailable",
        }
    return {
        "product": "ORAIflow Core",
        "version": __version__,
        "workspace_name": os.getenv("DEMO_WORKSPACE_NAME", "Atlas Fintech").strip()
        or "Atlas Fintech",
        **runtime,
        "mcp_required": True,
        "mode": "synthetic-local-demo",
    }


@app.get("/api/overview")
async def overview():
    """Return the synthetic workspace overview and recorded totals."""
    identity = _require("accounts:read")
    _require("transactions:read")
    with get_engine().connect() as conn:
        profile_row = (
            conn.execute(
                text(
                    "SELECT display_name, preferred_name FROM customer_profiles WHERE user_id=:owner AND tenant_id=:tenant"
                ),
                {"owner": identity.subject, "tenant": identity.tenant_id},
            )
            .mappings()
            .first()
        )
        accounts = (
            conn.execute(
                text("""
                SELECT account_id, account_name, account_type, masked_number,
                       currency, balance_minor, status
                FROM accounts WHERE owner_id=:owner AND tenant_id=:tenant ORDER BY account_name
            """),
                {"owner": identity.subject, "tenant": identity.tenant_id},
            )
            .mappings()
            .all()
        )
        transactions = (
            conn.execute(
                text("""
                SELECT t.transaction_id, t.account_id, a.account_name, t.description,
                       t.category, t.amount_minor, t.currency, t.status, t.occurred_at
                FROM transactions t JOIN accounts a ON a.account_id=t.account_id
                WHERE a.owner_id=:owner AND a.tenant_id=:tenant ORDER BY t.occurred_at DESC LIMIT 8
            """),
                {"owner": identity.subject, "tenant": identity.tenant_id},
            )
            .mappings()
            .all()
        )
        recent_transfers = (
            conn.execute(
                text("""
                SELECT tc.command_id, tc.preview_id, tc.approval_id,
                       tc.outbox_event_id, tc.from_account_id, tc.beneficiary,
                       tc.amount_minor, tc.currency, tc.status,
                       tc.temporal_workflow_id, tc.error_message,
                       reconciliation.status AS reconciliation_status,
                       reconciliation.transaction_id AS ledger_transaction_id,
                       tc.created_at, tc.updated_at
                  FROM transfer_commands AS tc
                  LEFT JOIN ledger_reconciliations AS reconciliation
                    ON reconciliation.command_id=tc.command_id
                 WHERE tc.user_id=:owner AND tc.tenant_id=:tenant
                 ORDER BY tc.created_at DESC LIMIT 10
            """),
                {"owner": identity.subject, "tenant": identity.tenant_id},
            )
            .mappings()
            .all()
        )
    total_minor = sum(int(a["balance_minor"]) for a in accounts if a["currency"] == "EUR")
    return {
        "customer": {
            "id": identity.subject,
            "display_name": (
                (profile_row["preferred_name"] or profile_row["display_name"])
                if profile_row
                else os.getenv("DEMO_DISPLAY_NAME", "Alex Morgan")
            ),
        },
        "total_balance": {"amount_minor": total_minor, "currency": "EUR"},
        "accounts": [dict(a) for a in accounts],
        "transactions": [dict(t) for t in transactions],
        "transfers": [dict(t) for t in recent_transfers],
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/loans")
async def list_loans():
    """Read the authenticated customer's loan liabilities; illustrative demo data is opt-in."""
    identity = _require("loans:read")
    include_demo = demo_loan_enabled()
    with get_engine().connect() as conn:
        loans = (
            conn.execute(
                text("""
            SELECT loan_id, product_name, principal_minor, outstanding_principal_minor,
                   currency, annual_interest_rate_bps, term_months, monthly_payment_minor,
                   total_interest_minor, total_repayment_minor, disbursement_date,
                   first_due_date, status, demo_only, payment_tracking
              FROM demo_loans
             WHERE owner_id=:owner AND tenant_id=:tenant
               AND (demo_only=FALSE OR :include_demo=TRUE)
             ORDER BY created_at DESC
        """),
                {
                    "owner": identity.subject,
                    "tenant": identity.tenant_id,
                    "include_demo": include_demo,
                },
            )
            .mappings()
            .all()
        )
        result = []
        for loan in loans:
            item = dict(loan)
            installments = (
                conn.execute(
                    text("""
                SELECT installment_number, due_date, payment_minor, principal_minor,
                       interest_minor, status
                  FROM demo_loan_installments
                 WHERE loan_id=:loan_id ORDER BY installment_number
            """),
                    {"loan_id": loan["loan_id"]},
                )
                .mappings()
                .all()
            )
            item["installments"] = [dict(row) for row in installments]
            result.append(item)
    return {"loans": jsonable_encoder(result), "as_of": datetime.now(timezone.utc).isoformat()}


@app.get("/api/financial-health")
async def financial_health_overview():
    """Return a read-only recorded-leverage snapshot for the authenticated customer."""
    identity = _require("financial_health:read")
    _require("accounts:read")
    _require("loans:read")
    with get_engine().connect() as conn:
        account_rows = (
            conn.execute(
                text("""
            SELECT currency, balance_minor, status
              FROM accounts WHERE owner_id=:owner AND tenant_id=:tenant
        """),
                {"owner": identity.subject, "tenant": identity.tenant_id},
            )
            .mappings()
            .all()
        )
        loan_rows = (
            conn.execute(
                text("""
            SELECT currency, outstanding_principal_minor, principal_minor, demo_only
              FROM demo_loans
             WHERE owner_id=:owner AND tenant_id=:tenant
               AND (demo_only=FALSE OR :include_demo=TRUE)
        """),
                {
                    "owner": identity.subject,
                    "tenant": identity.tenant_id,
                    "include_demo": demo_loan_enabled(),
                },
            )
            .mappings()
            .all()
        )
    result = calculate_recorded_financial_health(
        account_rows, loan_rows, include_demo_loans=demo_loan_enabled()
    )
    result["as_of"] = datetime.now(timezone.utc).isoformat()
    return jsonable_encoder(result)


@app.get("/api/profile")
async def get_profile():
    """Return the current synthetic demo profile."""
    identity = _require("accounts:read")
    with get_engine().begin() as conn:
        conn.execute(
            text("""
            INSERT INTO customer_profiles(user_id, tenant_id, display_name)
            VALUES (:user, :tenant, :name)
            ON CONFLICT (user_id, tenant_id) DO NOTHING
        """),
            {
                "user": identity.subject,
                "tenant": identity.tenant_id,
                "name": os.getenv("DEMO_DISPLAY_NAME", "Alex Morgan"),
            },
        ).rowcount
        profile = (
            conn.execute(
                text("""
            SELECT display_name, preferred_name, email, phone,
                   preferred_currency, language, updated_at
              FROM customer_profiles WHERE user_id=:user AND tenant_id=:tenant
        """),
                {"user": identity.subject, "tenant": identity.tenant_id},
            )
            .mappings()
            .one()
        )
    return {
        "profile": dict(profile),
        "identity": {"subject": identity.subject, "tenant_id": identity.tenant_id},
    }


@app.put("/api/profile")
async def update_profile(payload: ProfileUpdateRequest):
    """Update permitted fields on the synthetic demo profile."""
    identity = _require("profile:update")
    display_name = payload.display_name.strip()
    if len(display_name) < 2:
        raise HTTPException(
            status_code=422, detail="Display name must contain at least two characters"
        )
    currency = payload.preferred_currency.upper()
    if currency not in {"EUR", "USD", "GBP"}:
        raise HTTPException(status_code=422, detail="Unsupported preferred currency")
    if payload.email and "@" not in payload.email:
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    with get_engine().begin() as conn:
        conn.execute(
            text("""
            INSERT INTO customer_profiles(user_id, tenant_id, display_name, preferred_name,
                email, phone, preferred_currency, language)
            VALUES (:user, :tenant, :display, :preferred, :email, :phone, :currency, :language)
            ON CONFLICT (user_id, tenant_id) DO UPDATE SET
                display_name=EXCLUDED.display_name, preferred_name=EXCLUDED.preferred_name,
                email=EXCLUDED.email, phone=EXCLUDED.phone,
                preferred_currency=EXCLUDED.preferred_currency, language=EXCLUDED.language,
                updated_at=NOW()
        """),
            {
                "user": identity.subject,
                "tenant": identity.tenant_id,
                "display": display_name,
                "preferred": payload.preferred_name.strip(),
                "email": payload.email.strip(),
                "phone": payload.phone.strip(),
                "currency": currency,
                "language": payload.language.strip().lower(),
            },
        )
        profile = (
            conn.execute(
                text("""
            SELECT display_name, preferred_name, email, phone,
                   preferred_currency, language, updated_at
              FROM customer_profiles WHERE user_id=:user AND tenant_id=:tenant
        """),
                {"user": identity.subject, "tenant": identity.tenant_id},
            )
            .mappings()
            .one()
        )
        append_audit_event(
            conn,
            actor_id=identity.subject,
            tenant_id=identity.tenant_id,
            event_type="CUSTOMER_PROFILE_UPDATED",
            subject_id=identity.subject,
            detail={
                "fields": [
                    "display_name",
                    "preferred_name",
                    "email",
                    "phone",
                    "preferred_currency",
                    "language",
                ]
            },
        )
    return {
        "profile": dict(profile),
        "identity": {"subject": identity.subject, "tenant_id": identity.tenant_id},
    }


@app.get("/api/contacts")
async def list_contacts():
    """List the authenticated customer's synthetic saved contacts for the workspace."""
    identity = _require("contacts:read")
    with get_engine().connect() as conn:
        rows = (
            conn.execute(
                text("""
            SELECT contact_id, full_name, email, phone, bank_name,
                   account_reference_masked, currency, status
              FROM contacts
             WHERE owner_id=:owner AND tenant_id=:tenant
             ORDER BY lower(full_name), contact_id
        """),
                {"owner": identity.subject, "tenant": identity.tenant_id},
            )
            .mappings()
            .all()
        )
    return {"contacts": [dict(row) for row in rows], "total": len(rows), "synthetic": True}


@app.get("/api/transactions")
async def list_transactions(limit: int = Query(default=100, ge=1, le=250)):
    """List booked synthetic transactions within the requested limit."""
    identity = _require("transactions:read")
    with get_engine().connect() as conn:
        rows = (
            conn.execute(
                text("""
                SELECT t.transaction_id, t.account_id, a.account_name, t.description,
                       t.category, t.amount_minor, t.currency, t.status, t.occurred_at
                FROM transactions t JOIN accounts a ON a.account_id=t.account_id
                WHERE a.owner_id=:owner AND a.tenant_id=:tenant ORDER BY t.occurred_at DESC LIMIT :limit
            """),
                {"owner": identity.subject, "tenant": identity.tenant_id, "limit": limit},
            )
            .mappings()
            .all()
        )
    return {"transactions": [dict(row) for row in rows]}


@app.get("/api/transfers")
async def list_transfers(limit: int = Query(default=50, ge=1, le=100)):
    """List synthetic transfer records within the requested limit."""
    identity = _require("payments:read")
    with get_engine().connect() as conn:
        rows = (
            conn.execute(
                text("""
                SELECT tc.command_id, tc.preview_id, tc.approval_id,
                       tc.outbox_event_id, tc.from_account_id, tc.beneficiary,
                       tc.amount_minor, tc.currency, tc.status,
                       tc.temporal_workflow_id, tc.error_message,
                       reconciliation.status AS reconciliation_status,
                       reconciliation.transaction_id AS ledger_transaction_id,
                       tc.created_at, tc.updated_at
                  FROM transfer_commands AS tc
                  LEFT JOIN ledger_reconciliations AS reconciliation
                    ON reconciliation.command_id=tc.command_id
                 WHERE tc.user_id=:owner AND tc.tenant_id=:tenant
                 ORDER BY tc.created_at DESC LIMIT :limit
            """),
                {"owner": identity.subject, "tenant": identity.tenant_id, "limit": limit},
            )
            .mappings()
            .all()
        )
    return {"transfers": [dict(row) for row in rows]}


@app.get("/api/audit")
async def list_audit_events(
    limit: int = Query(default=10, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
):
    """Return a stable page of audit evidence plus the tenant-scoped total."""
    identity = _require("audit:read")
    with get_engine().connect() as conn:
        total = conn.execute(
            text("SELECT count(*) FROM audit_events WHERE tenant_id=:tenant"),
            {"tenant": identity.tenant_id},
        ).scalar_one()
        rows = (
            conn.execute(
                text("""
                SELECT event_id, actor_id, tenant_id, event_type, subject_id, detail,
                       occurred_at, previous_hash, event_hash
                  FROM audit_events WHERE tenant_id=:tenant
                 ORDER BY occurred_at DESC, event_id DESC
                 LIMIT :limit OFFSET :offset
            """),
                {"tenant": identity.tenant_id, "limit": limit, "offset": offset},
            )
            .mappings()
            .all()
        )
    return {
        "events": [dict(row) for row in rows],
        "total": int(total),
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/audit/verify")
async def verify_audit_chain():
    """Verify the integrity of the synthetic demo audit chain."""
    identity = _require("audit:read")
    import hashlib

    with get_engine().connect() as conn:
        rows = (
            conn.execute(
                text("""
            SELECT event_id, actor_id, tenant_id, event_type, subject_id, detail,
                   occurred_at, previous_hash, event_hash
              FROM audit_events WHERE tenant_id=:tenant ORDER BY event_id
        """),
                {"tenant": identity.tenant_id},
            )
            .mappings()
            .all()
        )
        chain_state = (
            conn.execute(
                text("""
            SELECT head_event_id, head_hash FROM audit_chain_state WHERE tenant_id=:tenant
        """),
                {"tenant": identity.tenant_id},
            )
            .mappings()
            .first()
        )
    previous_hash = "0" * 64
    for row in rows:
        detail = row["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)
        occurred_at = (
            row["occurred_at"].isoformat()
            if hasattr(row["occurred_at"], "isoformat")
            else str(row["occurred_at"])
        )
        evidence = {
            "event_id": int(row["event_id"]),
            "actor_id": row["actor_id"],
            "tenant_id": row["tenant_id"],
            "event_type": row["event_type"],
            "subject_id": row["subject_id"],
            "detail": detail,
            "occurred_at": occurred_at,
            "previous_hash": previous_hash,
        }
        canonical = json.dumps(
            evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        )
        actual = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if row["previous_hash"] != previous_hash or row["event_hash"] != actual:
            return {
                "valid": False,
                "checked_events": int(row["event_id"]),
                "failed_event_id": int(row["event_id"]),
            }
        previous_hash = actual
    expected_head_id = int(rows[-1]["event_id"]) if rows else None
    expected_stored_hash = chain_state["head_hash"] if chain_state else "0" * 64
    expected_stored_id = chain_state["head_event_id"] if chain_state else None
    if expected_stored_hash != previous_hash or expected_stored_id != expected_head_id:
        return {
            "valid": False,
            "checked_events": len(rows),
            "failed_event_id": expected_head_id,
            "reason": "audit chain head does not match persisted events",
        }
    return {"valid": True, "checked_events": len(rows), "head_hash": previous_hash}


@app.post("/api/chat")
async def chat(payload: ChatRequest):
    """Retained direct-chat diagnostic proxy; the browser uses CopilotKit/AG-UI.

    This route is kept for isolated API-to-agent troubleshooting. Agent Runtime
    accesses banking reads only through the mandatory MCP capability boundary.
    """
    runtime_url = os.getenv("AGENT_RUNTIME_URL", "http://agent-runtime:8101").rstrip("/")
    internal_token = os.getenv(
        "AGENT_RUNTIME_INTERNAL_TOKEN", "local-agent-runtime-token-change-me"
    )
    timeout = max(10.0, float(os.getenv("AGENT_RUNTIME_TIMEOUT_SECONDS", "120")))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{runtime_url}/api/chat",
                json=payload.model_dump(),
                headers={"Authorization": f"Bearer {internal_token}"},
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        logger.exception("Agent Runtime request failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail="The Agent Runtime is unavailable or returned an error. Check its logs and health status.",
        ) from exc


@app.post("/api/transfers/preview")
async def transfer_preview(payload: TransferPreviewRequest):
    """Create a policy-checked transfer preview without submitting or executing a payment."""
    identity = _require_kernel_operation("kernel.transfer.preview")
    _validate_kernel_request("kernel.transfer.preview", payload.model_dump(exclude_none=True))
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=PREVIEW_TTL_MINUTES)
    preview_id = str(uuid.uuid4())
    try:
        with get_engine().begin() as conn:
            transfer = _evaluate_transfer(
                conn,
                payload.from_account_id,
                payload.beneficiary,
                payload.amount,
                payload.currency,
                identity,
                beneficiary_contact_id=payload.beneficiary_contact_id,
            )
            policy_details = {
                "max_transfer_minor": transfer["max_transfer_minor"],
                "available_balance_minor": transfer["available_balance_minor"],
                "fee_minor": transfer["fee_minor"],
                "total_minor": transfer["total_minor"],
            }
            signing_row = _preview_signing_row(
                preview_id, identity, transfer, expires_at, policy_details
            )
            digest = preview_digest(signing_row)
            conn.execute(
                text("""
                INSERT INTO transfer_previews(
                    preview_id, user_id, tenant_id, from_account_id, beneficiary, beneficiary_contact_id,
                    amount_minor, currency, fee_minor, total_minor, policy_version,
                    policy_decision, policy_details, payload_digest, expires_at
                ) VALUES (
                    :preview_id, :user_id, :tenant_id, :account, :beneficiary, :beneficiary_contact_id,
                    :amount, CAST(:currency AS text), :fee, :total, :policy_version,
                    :decision, CAST(:policy_details AS jsonb), :digest, :expires_at
                )
            """),
                {
                    "preview_id": preview_id,
                    "user_id": identity.subject,
                    "tenant_id": identity.tenant_id,
                    "account": transfer["from_account_id"],
                    "beneficiary": transfer["beneficiary"],
                    "beneficiary_contact_id": transfer.get("beneficiary_contact_id"),
                    "amount": transfer["amount_minor"],
                    "currency": transfer["currency"],
                    "fee": transfer["fee_minor"],
                    "total": transfer["total_minor"],
                    "policy_version": POLICY_VERSION,
                    "decision": "APPROVED",
                    "policy_details": json.dumps(policy_details),
                    "digest": digest,
                    "expires_at": expires_at,
                },
            )
            append_audit_event(
                conn,
                identity.subject,
                "TRANSFER_PREVIEW_CREATED",
                preview_id,
                {
                    "payload_digest": digest,
                    "policy_version": POLICY_VERSION,
                    "beneficiary_contact_id": transfer.get("beneficiary_contact_id"),
                    "amount_minor": transfer["amount_minor"],
                    "currency": transfer["currency"],
                },
                identity.tenant_id,
            )
        return _validate_kernel_response(
            "kernel.transfer.preview", _public_preview(transfer, preview_id, digest, expires_at)
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Transfer preview persistence failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=500, detail="The transfer preview could not be saved. Check the API logs."
        )


@app.post("/api/transfers")
async def create_transfer(payload: TransferConfirmRequest):
    """Accept an explicitly confirmed synthetic transfer command through the Banking Kernel."""
    identity = _require_kernel_operation("kernel.transfer.confirm")
    _validate_kernel_request("kernel.transfer.confirm", payload.model_dump())
    if not payload.customer_confirmed:
        raise HTTPException(status_code=400, detail="Customer confirmation is required")
    if os.getenv("DEMO_SCA_APPROVED", "true").lower() != "true":
        raise HTTPException(status_code=403, detail="Demo SCA is disabled in this environment")
    if not hmac.compare_digest(payload.sca_code, os.getenv("DEMO_SCA_CODE", "123456")):
        raise HTTPException(status_code=403, detail="Demo SCA challenge was not accepted")

    try:
        with get_engine().begin() as conn:
            preview_row = (
                conn.execute(
                    text("""
                SELECT * FROM transfer_previews
                 WHERE preview_id=:preview_id AND user_id=:user_id AND tenant_id=:tenant_id
                 FOR UPDATE
            """),
                    {
                        "preview_id": payload.preview_id,
                        "user_id": identity.subject,
                        "tenant_id": identity.tenant_id,
                    },
                )
                .mappings()
                .first()
            )
            if not preview_row:
                raise HTTPException(
                    status_code=404, detail="Transfer preview not found or not accessible"
                )
            preview = dict(preview_row)
            expected_digest = preview_digest(preview)
            if not verify_preview_digest(preview, preview["payload_digest"]):
                logger.error(
                    "Stored transfer preview digest integrity check failed: preview=%s",
                    payload.preview_id,
                )
                raise HTTPException(
                    status_code=409, detail="The stored transfer preview failed its integrity check"
                )
            if not hmac.compare_digest(expected_digest, preview["payload_digest"]):
                raise HTTPException(status_code=409, detail="Transfer preview payload has changed")
            if not hmac.compare_digest(payload.payload_digest, preview["payload_digest"]):
                raise HTTPException(
                    status_code=409,
                    detail="The approved preview does not match the displayed transfer. Review it again.",
                )

            existing = (
                conn.execute(
                    text("""
                SELECT * FROM transfer_commands WHERE idempotency_key=:key FOR UPDATE
            """),
                    {"key": payload.idempotency_key},
                )
                .mappings()
                .first()
            )
            if existing:
                if (
                    existing["user_id"] != identity.subject
                    or existing["tenant_id"] != identity.tenant_id
                    or existing["preview_id"] != payload.preview_id
                    or existing["preview_digest"] != preview["payload_digest"]
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency key was already used for a different command",
                    )
                command_id = existing["command_id"]
                status = existing["status"]
                workflow_id = existing["temporal_workflow_id"]
                replay = True
            else:
                if preview["consumed_at"] is not None:
                    raise HTTPException(
                        status_code=409, detail="This transfer preview has already been used"
                    )
                if preview["expires_at"] <= datetime.now(timezone.utc):
                    raise HTTPException(
                        status_code=410,
                        detail="Transfer preview expired. Please review a new preview.",
                    )
                if (
                    preview["policy_version"] != POLICY_VERSION
                    or preview["policy_decision"] != "APPROVED"
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Transfer policy changed. Please create a new preview.",
                    )

                # Re-check ownership, account state, funds, currency and limits under
                # the same DB transaction as command acceptance.
                rechecked = _evaluate_transfer(
                    conn,
                    preview["from_account_id"],
                    preview["beneficiary"],
                    f"{int(preview['amount_minor']) / 100:.2f}",
                    preview["currency"],
                    identity,
                    lock_account=True,
                    beneficiary_contact_id=preview.get("beneficiary_contact_id"),
                )
                approved_fields = (
                    rechecked["from_account_id"] == preview["from_account_id"]
                    and rechecked["beneficiary"] == preview["beneficiary"]
                    and rechecked.get("beneficiary_contact_id")
                    == preview.get("beneficiary_contact_id")
                    and rechecked["amount_minor"] == int(preview["amount_minor"])
                    and rechecked["currency"] == preview["currency"].strip()
                    and rechecked["fee_minor"] == int(preview["fee_minor"])
                    and rechecked["total_minor"] == int(preview["total_minor"])
                    and rechecked["max_transfer_minor"]
                    == int((preview["policy_details"] or {}).get("max_transfer_minor", -1))
                )
                if not approved_fields:
                    raise HTTPException(
                        status_code=409,
                        detail="Transfer details changed. Please review a new preview.",
                    )

                command_id = str(uuid.uuid4())
                approval_id = str(uuid.uuid4())
                conn.execute(
                    text("""
                    INSERT INTO transfer_approvals(
                        approval_id, preview_id, user_id, tenant_id, payload_digest,
                        authentication_method, evidence
                    ) VALUES (
                        :approval_id, :preview_id, :user_id, :tenant_id, :digest,
                        'local-demo-code', CAST(:evidence AS jsonb)
                    )
                """),
                    {
                        "approval_id": approval_id,
                        "preview_id": payload.preview_id,
                        "user_id": identity.subject,
                        "tenant_id": identity.tenant_id,
                        "digest": preview["payload_digest"],
                        "evidence": json.dumps(
                            {
                                "customer_confirmed": True,
                                "demo_sca_accepted": True,
                                "authentication_context": identity.authentication_method,
                            }
                        ),
                    },
                )
                conn.execute(
                    text("""
                    INSERT INTO transfer_commands(
                        command_id, idempotency_key, user_id, tenant_id, from_account_id,
                        beneficiary, beneficiary_contact_id, amount_minor, currency, status, preview_id,
                        preview_digest, approval_id
                    ) VALUES (
                        :id, :key, :user, :tenant, :account, :beneficiary, :beneficiary_contact_id, :amount,
                        CAST(:currency AS text), 'PENDING', :preview_id, :digest, :approval_id
                    )
                """),
                    {
                        "id": command_id,
                        "key": payload.idempotency_key,
                        "user": identity.subject,
                        "tenant": identity.tenant_id,
                        "account": preview["from_account_id"],
                        "beneficiary": preview["beneficiary"],
                        "beneficiary_contact_id": preview.get("beneficiary_contact_id"),
                        "amount": preview["amount_minor"],
                        "currency": preview["currency"],
                        "preview_id": payload.preview_id,
                        "digest": preview["payload_digest"],
                        "approval_id": approval_id,
                    },
                )
                append_audit_event(
                    conn,
                    identity.subject,
                    "TRANSFER_CUSTOMER_APPROVED",
                    command_id,
                    {
                        "preview_id": payload.preview_id,
                        "approval_id": approval_id,
                        "beneficiary_contact_id": preview.get("beneficiary_contact_id"),
                        "payload_digest": preview["payload_digest"],
                        "authentication_method": "local-demo-code",
                        "policy_version": POLICY_VERSION,
                    },
                    identity.tenant_id,
                )
                append_audit_event(
                    conn,
                    identity.subject,
                    "TRANSFER_COMMAND_ACCEPTED",
                    command_id,
                    {
                        "amount_minor": int(preview["amount_minor"]),
                        "currency": preview["currency"].strip(),
                        "beneficiary": preview["beneficiary"],
                        "beneficiary_contact_id": preview.get("beneficiary_contact_id"),
                        "preview_id": payload.preview_id,
                        "approval_id": approval_id,
                        "sca": "demo-confirmed",
                    },
                    identity.tenant_id,
                )
                conn.execute(
                    text("""
                    INSERT INTO transfer_outbox(event_type, aggregate_id, payload)
                    VALUES ('TRANSFER_ACCEPTED', :command_id,
                            jsonb_build_object('command_id', CAST(:command_id AS text)))
                    ON CONFLICT (event_type, aggregate_id) DO NOTHING
                """),
                    {"command_id": command_id},
                )
                conn.execute(
                    text("""
                    UPDATE transfer_previews
                       SET consumed_at=NOW(), consumed_by_command_id=:command_id
                     WHERE preview_id=:preview_id AND consumed_at IS NULL
                """),
                    {"command_id": command_id, "preview_id": payload.preview_id},
                )
                status = "PENDING"
                workflow_id = None
                replay = False
        response = {
            "command_id": command_id,
            "workflow_id": workflow_id,
            "status": status,
            "idempotent_replay": replay,
            "message": "Transfer accepted by the demo Banking Kernel and durably queued for Temporal dispatch.",
            "demo": True,
        }
        return _validate_kernel_response("kernel.transfer.confirm", response)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not persist transfer command: %s", type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail="The transfer could not be recorded in the local ledger. Check the API logs.",
        )


@app.get("/api/transfers/{command_id}")
async def get_transfer(command_id: str):
    """Return the recorded state of a synthetic transfer command."""
    identity = _require_kernel_operation("kernel.transfer.status")
    _validate_kernel_request("kernel.transfer.status", {"command_id": command_id})
    with get_engine().connect() as conn:
        row = (
            conn.execute(
                text("""
                SELECT command.*, reconciliation.status AS reconciliation_status,
                       reconciliation.transaction_id AS ledger_transaction_id,
                       reconciliation.reconciled_at
                  FROM transfer_commands AS command
                  LEFT JOIN ledger_reconciliations AS reconciliation
                    ON reconciliation.command_id=command.command_id
                 WHERE command.command_id=:id AND command.user_id=:user AND command.tenant_id=:tenant
            """),
                {"id": command_id, "user": identity.subject, "tenant": identity.tenant_id},
            )
            .mappings()
            .first()
        )
    if not row:
        raise HTTPException(status_code=404, detail="Transfer not found")
    response = {
        "command_id": row["command_id"],
        "preview_id": row["preview_id"],
        "approval_id": row["approval_id"],
        "outbox_event_id": row["outbox_event_id"],
        "beneficiary": row["beneficiary"],
        "beneficiary_contact_id": row.get("beneficiary_contact_id"),
        "amount_minor": row["amount_minor"],
        "currency": row["currency"],
        "status": row["status"],
        "workflow_id": row["temporal_workflow_id"],
        "error": row["error_message"],
        "reconciliation_status": row["reconciliation_status"],
        "ledger_transaction_id": row["ledger_transaction_id"],
        "reconciled_at": row["reconciled_at"],
        "updated_at": row["updated_at"],
    }
    return _validate_kernel_response("kernel.transfer.status", response)
