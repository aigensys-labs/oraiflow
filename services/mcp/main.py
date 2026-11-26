"""MCP server exposing the authenticated, registered ORAIflow demo capabilities."""

from __future__ import annotations

import hmac
import json
import os
import uuid
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import text

from shared.database import get_mcp_engine
from shared.version import __version__
from shared.identity import current_demo_identity, require_capability
from shared.financial_health import calculate_recorded_financial_health
from shared.demo_configuration import demo_loan_enabled
from shared.contracts.registry import (
    create_result_envelope,
    get_capability,
    validate_capability_input,
    validate_capability_output,
)

MCP_ACCESS_TOKEN = os.getenv("MCP_ACCESS_TOKEN", "local-mcp-token-change-me")
_mcp_trace_id: ContextVar[str] = ContextVar("oraiflow_mcp_trace_id", default="-")


def _require_mcp_capability(capability_id: str):
    """Resolve the required capability from OCCF, then enforce it server-side."""
    contract = get_capability(capability_id)
    if contract.get("kind") != "mcp":
        raise RuntimeError(f"Not an MCP capability contract: {capability_id}")
    identity = current_demo_identity()
    require_capability(identity, contract["authorization"]["required_capability"])
    return identity


def _validated_tool_result(
    capability_id: str, payload: dict, data, *, outcome: str | None = None
) -> str:
    """Return a validated common result envelope from the MCP boundary."""
    validate_capability_input(capability_id, payload)
    json_data = jsonable_encoder(data)
    validate_capability_output(capability_id, json_data)
    outcome = outcome or (
        "EMPTY" if isinstance(json_data, (list, dict)) and not json_data else "SUCCESS"
    )
    trace_id = _mcp_trace_id.get()
    if not trace_id or trace_id == "-":
        trace_id = f"mcp-{uuid.uuid4().hex[:20]}"
    envelope = create_result_envelope(
        capability_id,
        outcome,
        data=json_data,
        trace_id=trace_id,
    )
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def _rejected_tool_result(capability_id: str, code: str) -> str:
    envelope = create_result_envelope(
        capability_id,
        "REJECTED",
        error={"code": code, "retryable": False},
        trace_id=(
            _mcp_trace_id.get() if _mcp_trace_id.get() != "-" else f"mcp-{uuid.uuid4().hex[:20]}"
        ),
    )
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


mcp = FastMCP(
    "ORAIflow Banking Capabilities",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["mcp:8100", "mcp:8100:*", "localhost:*", "127.0.0.1:*"],
        allowed_origins=[],
    ),
)


@mcp.tool()
def list_customer_accounts() -> str:
    """List the authenticated demo customer's accounts. Read-only."""
    validate_capability_input("banking.accounts.list", {})
    try:
        identity = _require_mcp_capability("banking.accounts.list")
    except PermissionError:
        return _rejected_tool_result("banking.accounts.list", "CAPABILITY_NOT_PERMITTED")
    with get_mcp_engine().connect() as conn:
        rows = (
            conn.execute(
                text("""
                SELECT account_id, account_name, account_type,
                       currency, balance_minor, status
                FROM accounts WHERE owner_id = :owner AND tenant_id = :tenant ORDER BY account_name
            """),
                {"owner": identity.subject, "tenant": identity.tenant_id},
            )
            .mappings()
            .all()
        )
    data = [dict(row) for row in rows]
    return _validated_tool_result("banking.accounts.list", {}, data)


@mcp.tool()
def get_account_balance(account_id: str) -> str:
    """Get a balance for an account owned by the authenticated demo customer."""
    request = {"account_id": account_id}
    validate_capability_input("banking.accounts.balance", request)
    try:
        identity = _require_mcp_capability("banking.accounts.balance")
    except PermissionError:
        return _rejected_tool_result("banking.accounts.balance", "CAPABILITY_NOT_PERMITTED")
    with get_mcp_engine().connect() as conn:
        row = (
            conn.execute(
                text("""
                SELECT account_id, account_name, currency, balance_minor, status
                FROM accounts WHERE account_id = :account AND owner_id = :owner AND tenant_id = :tenant
            """),
                {"account": account_id, "owner": identity.subject, "tenant": identity.tenant_id},
            )
            .mappings()
            .first()
        )
    if not row:
        # An inaccessible account is not an empty or zero-balance success.
        return _rejected_tool_result("banking.accounts.balance", "ACCOUNT_NOT_ACCESSIBLE")
    return _validated_tool_result("banking.accounts.balance", request, dict(row))


@mcp.tool()
def get_recent_transactions(limit: int = 5) -> str:
    """List recent transactions for the authenticated demo customer's accounts."""
    request = {"limit": limit}
    validate_capability_input("banking.transactions.recent", request)
    try:
        identity = _require_mcp_capability("banking.transactions.recent")
    except PermissionError:
        return _rejected_tool_result("banking.transactions.recent", "CAPABILITY_NOT_PERMITTED")
    safe_limit = int(limit)
    with get_mcp_engine().connect() as conn:
        rows = (
            conn.execute(
                text("""
                SELECT t.transaction_id, a.account_name, t.description, t.category,
                       t.amount_minor, t.currency, t.status, t.occurred_at
                FROM transactions t
                JOIN accounts a ON a.account_id = t.account_id
                WHERE a.owner_id = :owner AND a.tenant_id = :tenant
                ORDER BY t.occurred_at DESC
                LIMIT :limit
            """),
                {"owner": identity.subject, "tenant": identity.tenant_id, "limit": safe_limit},
            )
            .mappings()
            .all()
        )
    return _validated_tool_result(
        "banking.transactions.recent", request, [dict(row) for row in rows]
    )


_TRANSACTION_ANALYSIS_PERIODS = {
    "last_7_days",
    "last_30_days",
    "last_90_days",
    "last_365_days",
    "this_month",
    "last_month",
    "year_to_date",
    "all_available",
}
_TRANSACTION_ANALYSIS_GROUPS = {"none", "merchant", "category", "account", "month"}
_TRANSACTION_ANALYSIS_FLOWS = {"all", "inflows", "outflows"}


def _transaction_analysis_period_bounds(
    period: str,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> tuple[datetime | None, datetime | None, str | None, str | None, str]:
    """Resolve a registered relative period using the configured business timezone.

    The end timestamp is exclusive in SQL; returned end_date is inclusive for
    customer-facing evidence. The LLM never calculates calendar boundaries.
    """
    zone_name = timezone_name or os.getenv("ORAI_BUSINESS_TIMEZONE", "UTC")
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("ORAI_BUSINESS_TIMEZONE must be a valid IANA timezone") from exc
    current = now or datetime.now(zone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=zone)
    else:
        current = current.astimezone(zone)
    today = current.date()

    if period == "all_available":
        return None, None, None, None, zone_name
    if period.startswith("last_") and period.endswith("_days"):
        days = int(period.removeprefix("last_").removesuffix("_days"))
        if days not in {7, 30, 90, 365}:
            raise ValueError("Unsupported transaction-analysis period")
        start_day = today - timedelta(days=days - 1)
        end_day = today + timedelta(days=1)
    elif period == "this_month":
        start_day = today.replace(day=1)
        # Include only records up to the current customer-local day. A future
        # transaction timestamp must not leak into a "this month" answer.
        end_day = today + timedelta(days=1)
    elif period == "last_month":
        end_day = today.replace(day=1)
        start_day = (end_day - timedelta(days=1)).replace(day=1)
    elif period == "year_to_date":
        start_day = today.replace(month=1, day=1)
        end_day = today + timedelta(days=1)
    else:
        raise ValueError("Unsupported transaction-analysis period")

    start_at = datetime.combine(start_day, time.min, tzinfo=zone)
    end_at = datetime.combine(end_day, time.min, tzinfo=zone)
    return (
        start_at,
        end_at,
        start_day.isoformat(),
        (end_day - timedelta(days=1)).isoformat(),
        zone_name,
    )


@mcp.tool()
def analyze_customer_transactions(
    period: str | None = None,
    flow: str | None = None,
    merchant_query: str | None = None,
    category_query: str | None = None,
    account_name_query: str | None = None,
    measure: str = "sum",
    group_by: str = "none",
    analysis_mode: str = "activity",
    include_transfers: bool = False,
    limit: int = 50,
) -> str:
    """Analyze the authenticated customer's booked transactions. Read-only.

    Filters are parameterized and customer scope is derived only from the MCP
    identity. Aggregates are deterministic and kept separate by currency.
    Merchant grouping is based on the recorded transaction description; it is
    not a claim that ORAIflow has a normalized merchant identity.
    """
    request = {
        "measure": measure,
        "group_by": group_by,
        "analysis_mode": analysis_mode,
        "include_transfers": include_transfers,
        "limit": limit,
    }
    for key, value in (
        ("period", period),
        ("flow", flow),
        ("merchant_query", merchant_query),
        ("category_query", category_query),
        ("account_name_query", account_name_query),
    ):
        if value is not None:
            request[key] = str(value).strip()

    validate_capability_input("banking.transactions.analyze", request)
    try:
        identity = _require_mcp_capability("banking.transactions.analyze")
    except PermissionError:
        return _rejected_tool_result("banking.transactions.analyze", "CAPABILITY_NOT_PERMITTED")

    effective_period = period or (
        "last_365_days" if analysis_mode == "recurring_inflows" else "last_30_days"
    )
    effective_flow = flow or ("inflows" if analysis_mode == "recurring_inflows" else "all")
    effective_group_by = group_by
    if analysis_mode == "recurring_inflows":
        if flow == "outflows":
            return _rejected_tool_result(
                "banking.transactions.analyze", "INCOMPATIBLE_ANALYSIS_FILTERS"
            )
        effective_flow = "inflows"
        if group_by == "none":
            effective_group_by = "merchant"

    try:
        start_at, end_at, start_date, end_date, timezone_name = _transaction_analysis_period_bounds(
            effective_period
        )
    except ValueError:
        return _rejected_tool_result(
            "banking.transactions.analyze", "INVALID_BUSINESS_TIMEZONE_OR_PERIOD"
        )

    predicates = [
        "a.owner_id = :owner",
        "a.tenant_id = :tenant",
        "UPPER(TRIM(t.status)) NOT IN ('PENDING', 'FAILED', 'REVERSED', 'CANCELLED', 'DECLINED')",
    ]
    params = {
        "owner": identity.subject,
        "tenant": identity.tenant_id,
        "business_timezone": timezone_name,
        "include_transfers": bool(include_transfers),
    }
    if start_at is not None:
        predicates.append("t.occurred_at >= :period_start")
        params["period_start"] = start_at
    if end_at is not None:
        predicates.append("t.occurred_at < :period_end_exclusive")
        params["period_end_exclusive"] = end_at
    if effective_flow == "inflows":
        predicates.append("t.amount_minor > 0")
    elif effective_flow == "outflows":
        predicates.append("t.amount_minor < 0")
    elif effective_flow not in _TRANSACTION_ANALYSIS_FLOWS:
        return _rejected_tool_result("banking.transactions.analyze", "INVALID_FLOW")
    if not include_transfers:
        # The data model only establishes category=TRANSFER. It cannot prove
        # whether every other transaction is external or an own-account move.
        predicates.append("UPPER(COALESCE(t.category, '')) <> 'TRANSFER'")
    if merchant_query:
        predicates.append("POSITION(LOWER(:merchant_query) IN LOWER(t.description)) > 0")
        params["merchant_query"] = merchant_query.casefold()
    if category_query:
        predicates.append("POSITION(LOWER(:category_query) IN LOWER(COALESCE(t.category, ''))) > 0")
        params["category_query"] = category_query.casefold()
    if account_name_query:
        predicates.append("POSITION(LOWER(:account_name_query) IN LOWER(a.account_name)) > 0")
        params["account_name_query"] = account_name_query.casefold()

    where_sql = " AND ".join(predicates)
    amount_expression = (
        "ABS(t.amount_minor)" if effective_flow in {"inflows", "outflows"} else "t.amount_minor"
    )
    summary_sql = f"""
        SELECT t.currency,
               COUNT(*)::BIGINT AS transaction_count,
               COALESCE(SUM({amount_expression}), 0)::BIGINT AS sum_minor,
               COALESCE(CAST(ROUND(AVG({amount_expression})) AS BIGINT), 0) AS average_minor
          FROM transactions t
          JOIN accounts a ON a.account_id = t.account_id
         WHERE {where_sql}
         GROUP BY t.currency
         ORDER BY t.currency
    """

    group_sql = None
    if effective_group_by != "none":
        # SQL fragments are selected exclusively from this static allowlist;
        # customer/model values are always bound parameters, never SQL syntax.
        group_specs = {
            "merchant": ("LOWER(BTRIM(t.description))", "MIN(BTRIM(t.description))"),
            "category": (
                "LOWER(COALESCE(NULLIF(BTRIM(t.category), ''), 'OTHER'))",
                "MIN(COALESCE(NULLIF(BTRIM(t.category), ''), 'OTHER'))",
            ),
            "account": ("a.account_id", "MIN(a.account_name)"),
            "month": (
                "TO_CHAR(DATE_TRUNC('month', t.occurred_at AT TIME ZONE :business_timezone), 'YYYY-MM')",
                "TO_CHAR(DATE_TRUNC('month', t.occurred_at AT TIME ZONE :business_timezone), 'YYYY-MM')",
            ),
        }
        if effective_group_by not in group_specs:
            return _rejected_tool_result("banking.transactions.analyze", "UNSUPPORTED_GROUPING")
        group_key, group_label = group_specs[effective_group_by]
        group_order = (
            "group_value ASC"
            if effective_group_by == "month"
            else "transaction_count DESC, group_value ASC"
        )
        group_sql = f"""
            SELECT {group_label} AS group_value, t.currency,
                   COUNT(*)::BIGINT AS transaction_count,
                   COALESCE(SUM({amount_expression}), 0)::BIGINT AS sum_minor,
                   COALESCE(CAST(ROUND(AVG({amount_expression})) AS BIGINT), 0) AS average_minor,
                   COUNT(DISTINCT DATE_TRUNC('month', t.occurred_at AT TIME ZONE :business_timezone))::BIGINT AS active_months
              FROM transactions t
              JOIN accounts a ON a.account_id = t.account_id
             WHERE {where_sql}
             GROUP BY {group_key}, t.currency
             ORDER BY {group_order}
             LIMIT 101
        """

    detail_sql = f"""
        SELECT t.transaction_id, a.account_name, t.description, t.category,
               t.amount_minor, t.currency, t.status, t.occurred_at
          FROM transactions t
          JOIN accounts a ON a.account_id = t.account_id
         WHERE {where_sql}
         ORDER BY t.occurred_at DESC, t.transaction_id DESC
         LIMIT :detail_limit
    """

    with get_mcp_engine().connect() as conn:
        summary_rows = conn.execute(text(summary_sql), params).mappings().all()
        group_rows = conn.execute(text(group_sql), params).mappings().all() if group_sql else []
        detail_rows = []
        details_truncated = False
        if measure == "transactions":
            detail_params = {**params, "detail_limit": min(int(limit), 100) + 1}
            detail_rows = conn.execute(text(detail_sql), detail_params).mappings().all()
            details_truncated = len(detail_rows) > int(limit)
            detail_rows = detail_rows[: int(limit)]

    summary = [
        {
            "currency": str(row["currency"]).upper(),
            "transaction_count": int(row["transaction_count"]),
            "sum_minor": int(row["sum_minor"]),
            "average_minor": int(row["average_minor"]),
        }
        for row in summary_rows
    ]
    total_count = sum(row["transaction_count"] for row in summary)
    groups = [
        {
            "group_value": str(row["group_value"] or "Unspecified")[:160],
            "currency": str(row["currency"]).upper(),
            "transaction_count": int(row["transaction_count"]),
            "sum_minor": int(row["sum_minor"]),
            "average_minor": int(row["average_minor"]),
            "active_months": int(row["active_months"]),
            "potentially_recurring": bool(
                analysis_mode == "recurring_inflows"
                and effective_group_by == "merchant"
                and int(row["transaction_count"]) >= 3
                and int(row["active_months"]) >= 3
            ),
        }
        for row in group_rows[:100]
    ]
    transaction_rows = [
        {
            "transaction_id": str(row["transaction_id"]),
            "account_name": str(row["account_name"]),
            "description": str(row["description"]),
            "category": row["category"],
            "amount_minor": int(row["amount_minor"]),
            "currency": str(row["currency"]).upper(),
            "status": str(row["status"]),
            "occurred_at": (
                row["occurred_at"].isoformat()
                if hasattr(row["occurred_at"], "isoformat")
                else (str(row["occurred_at"]) if row["occurred_at"] is not None else None)
            ),
        }
        for row in detail_rows
    ]

    truncated = len(group_rows) > 100 or details_truncated
    data = {
        "period": effective_period,
        "period_start": start_date,
        "period_end": end_date,
        "business_timezone": timezone_name,
        "flow": effective_flow,
        "analysis_mode": analysis_mode,
        "measure": measure,
        "group_by": effective_group_by,
        "include_transfers": bool(include_transfers),
        "merchant_query": str(merchant_query).strip() if merchant_query else None,
        "category_query": str(category_query).strip() if category_query else None,
        "transaction_count": total_count,
        "truncated": truncated,
        "summary_by_currency": summary,
        "groups": groups,
        "transactions": transaction_rows,
    }
    # The generic result-envelope contract reserves EMPTY for an empty data
    # collection. This analysis result carries query metadata even when no rows
    # match, so SUCCESS + transaction_count=0 is the correct typed outcome.
    outcome = "PARTIAL" if truncated else "SUCCESS"
    return _validated_tool_result("banking.transactions.analyze", request, data, outcome=outcome)


@mcp.tool()
def get_customer_loan_overview() -> str:
    """Retrieve the authenticated customer's illustrative loan terms and repayment schedule. Read-only; dates and tracking status are explicitly synthetic."""
    validate_capability_input("banking.loans.overview", {})
    try:
        identity = _require_mcp_capability("banking.loans.overview")
    except PermissionError:
        return _rejected_tool_result("banking.loans.overview", "CAPABILITY_NOT_PERMITTED")
    if not demo_loan_enabled():
        return _validated_tool_result("banking.loans.overview", {}, {})
    with get_mcp_engine().connect() as conn:
        loan = (
            conn.execute(
                text("""
            SELECT loan_id, product_name, principal_minor, outstanding_principal_minor,
                   currency, annual_interest_rate_bps, term_months, monthly_payment_minor,
                   total_interest_minor, total_repayment_minor, disbursement_date,
                   first_due_date, status, demo_only, payment_tracking
              FROM demo_loans
             WHERE owner_id=:owner AND tenant_id=:tenant AND demo_only=TRUE
             ORDER BY created_at DESC LIMIT 1
        """),
                {"owner": identity.subject, "tenant": identity.tenant_id},
            )
            .mappings()
            .first()
        )
        if not loan:
            return _validated_tool_result("banking.loans.overview", {}, {})
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
    data = {**dict(loan), "installments": [dict(item) for item in installments]}
    return _validated_tool_result("banking.loans.overview", {}, data)


@mcp.tool()
def get_customer_financial_health_overview() -> str:
    """Calculate recorded leverage from the authenticated customer's active deposit accounts and registered loan liabilities. Read-only; not a credit score."""
    validate_capability_input("banking.financial_health.overview", {})
    try:
        identity = _require_mcp_capability("banking.financial_health.overview")
        # This aggregate requires both source permissions as well as the
        # dedicated financial-health permission. All identity is server-derived.
        require_capability(identity, "accounts:read")
        require_capability(identity, "loans:read")
    except PermissionError:
        return _rejected_tool_result(
            "banking.financial_health.overview", "CAPABILITY_NOT_PERMITTED"
        )

    with get_mcp_engine().connect() as conn:
        account_rows = (
            conn.execute(
                text("""
            SELECT currency, balance_minor, status
              FROM accounts
             WHERE owner_id=:owner AND tenant_id=:tenant
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

    data = calculate_recorded_financial_health(
        account_rows, loan_rows, include_demo_loans=demo_loan_enabled()
    )
    return _validated_tool_result("banking.financial_health.overview", {}, data)


@mcp.tool()
def search_contacts(name_query: str) -> str:
    """Search saved contacts for the authenticated demo customer. Read-only; never initiates a payment."""
    request = {"name_query": name_query}
    validate_capability_input("banking.contacts.search", request)
    try:
        identity = _require_mcp_capability("banking.contacts.search")
    except PermissionError:
        return _rejected_tool_result("banking.contacts.search", "CAPABILITY_NOT_PERMITTED")

    normalized = " ".join(str(name_query).casefold().split())
    # Exact, prefix and contained matches are returned with a deterministic rank.
    # The caller must resolve ambiguity; the MCP service never chooses a payee.
    with get_mcp_engine().connect() as conn:
        rows = (
            conn.execute(
                text("""
                SELECT contact_id, full_name, bank_name, account_reference_masked,
                       currency, status,
                       CASE WHEN normalized_name=:exact THEN 0
                            WHEN normalized_name LIKE :prefix THEN 1 ELSE 2 END AS match_rank
                  FROM contacts
                 WHERE owner_id=:owner AND tenant_id=:tenant
                   AND status='ACTIVE'
                   AND (normalized_name=:exact OR normalized_name LIKE :prefix OR normalized_name LIKE :contains)
                 ORDER BY match_rank, normalized_name, contact_id
                 LIMIT 10
            """),
                {
                    "exact": normalized,
                    "prefix": normalized + "%",
                    "contains": "%" + normalized + "%",
                    "owner": identity.subject,
                    "tenant": identity.tenant_id,
                },
            )
            .mappings()
            .all()
        )
    data = [{key: value for key, value in row.items() if key != "match_rank"} for row in rows]
    return _validated_tool_result("banking.contacts.search", request, data)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Parent lifespan is required because the MCP transport is mounted in FastAPI.
    """Initialize and release MCP server resources."""
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="ORAIflow MCP Banking Capabilities", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def local_bearer_guard(request: Request, call_next):
    # Development transport guard only. Replace with OAuth 2.1 resource-server
    # validation (issuer, signature, audience, scopes) before any shared deployment.
    """Authenticate internal MCP requests using the configured local demo bearer token."""
    if request.url.path.startswith("/mcp"):
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {MCP_ACCESS_TOKEN}"
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"error": "Unauthorized MCP client"}, status_code=401)
        supplied_trace = request.headers.get("x-orai-trace-id", "").strip()
        trace_id = (
            supplied_trace[:128]
            if supplied_trace and all(ch.isalnum() or ch in "-_" for ch in supplied_trace)
            else f"mcp-{uuid.uuid4().hex[:20]}"
        )
        token = _mcp_trace_id.set(trace_id)
        try:
            return await call_next(request)
        finally:
            _mcp_trace_id.reset(token)
    return await call_next(request)


@app.get("/healthz")
async def healthz():
    """Report MCP server process health."""
    return {"status": "ok", "service": "mcp", "transport": "streamable-http"}


app.mount("/", mcp.streamable_http_app())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("services.mcp.main:app", host="0.0.0.0", port=int(os.getenv("MCP_PORT", "8100")))
