"""Database engine, schema initialization, and synthetic demo-data persistence."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from shared.demo_seed import (
    parse_demo_salary_amount_minor,
    demo_salary_balance_delta,
    build_demo_transaction_history,
)
from shared.loan_demo import build_demo_loan_record
from shared.demo_configuration import demo_loan_enabled, demo_transaction_history_enabled


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the configured SQLAlchemy engine for the current process."""
    url = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://oraiflow:local-only-change-me@postgres:5432/oraiflow",
    )
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


@lru_cache(maxsize=1)
def get_mcp_engine() -> Engine:
    """Use the dedicated SELECT-only database principal for MCP capabilities."""
    url = os.getenv(
        "MCP_DATABASE_URL",
        "postgresql+psycopg://oraiflow_mcp_reader:local-only-read-change-me@postgres:5432/oraiflow",
    )
    return create_engine(url, pool_pre_ping=True, pool_size=3, max_overflow=2)


def _backfill_audit_hashes(conn) -> None:
    """Hash-link legacy audit rows per tenant before the append-only trigger is installed."""
    rows = conn.execute(text("""
        SELECT event_id, actor_id, tenant_id, event_type, subject_id, detail,
               occurred_at, previous_hash, event_hash
          FROM audit_events ORDER BY tenant_id, event_id
    """)).mappings().all()
    previous_by_tenant: dict[str, str] = {}
    head_id_by_tenant: dict[str, int | None] = {}
    for row in rows:
        tenant = row["tenant_id"] or "unscoped"
        previous_hash = previous_by_tenant.get(tenant, "0" * 64)
        if row["event_hash"]:
            previous_by_tenant[tenant] = row["event_hash"]
            head_id_by_tenant[tenant] = row["event_id"]
            continue
        detail = row["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)
        occurred_at = row["occurred_at"]
        occurred_text = (
            occurred_at.isoformat() if isinstance(occurred_at, datetime) else str(occurred_at)
        )
        payload = {
            "event_id": int(row["event_id"]),
            "actor_id": row["actor_id"],
            "tenant_id": row["tenant_id"],
            "event_type": row["event_type"],
            "subject_id": row["subject_id"],
            "detail": detail,
            "occurred_at": occurred_text,
            "previous_hash": previous_hash,
        }
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        )
        event_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        conn.execute(
            text("""
            UPDATE audit_events
               SET previous_hash=:previous_hash, event_hash=:event_hash
             WHERE event_id=:event_id
        """),
            {"previous_hash": previous_hash, "event_hash": event_hash, "event_id": row["event_id"]},
        )
        previous_by_tenant[tenant] = event_hash
        head_id_by_tenant[tenant] = row["event_id"]
    for tenant, head_hash in previous_by_tenant.items():
        conn.execute(
            text("""
            INSERT INTO audit_chain_state(tenant_id, head_event_id, head_hash)
            VALUES (:tenant, :head_id, :head_hash)
            ON CONFLICT (tenant_id) DO UPDATE
              SET head_event_id=EXCLUDED.head_event_id, head_hash=EXCLUDED.head_hash
        """),
            {"tenant": tenant, "head_id": head_id_by_tenant.get(tenant), "head_hash": head_hash},
        )


def _build_mcp_role_ddl(conn, password: str, role_exists: bool) -> str:
    """Build safe CREATE/ALTER ROLE SQL using PostgreSQL's literal quoting.

    ``format`` takes a polymorphic variadic argument; casting the password
    bind to TEXT is required for PostgreSQL to infer its type with psycopg.
    The %L placeholder safely quotes the password as a SQL string literal.
    """
    template = (
        "ALTER ROLE oraiflow_mcp_reader WITH LOGIN NOINHERIT NOSUPERUSER "
        "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5 PASSWORD %L"
        if role_exists
        else "CREATE ROLE oraiflow_mcp_reader LOGIN NOINHERIT NOSUPERUSER "
        "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5 PASSWORD %L"
    )
    return conn.execute(
        text("SELECT format(CAST(:template AS text), CAST(:password AS text))"),
        {"template": template, "password": password},
    ).scalar_one()


def _build_checkpoint_role_ddl(conn, password: str, role_exists: bool) -> str:
    """Create a login restricted to the dedicated LangGraph checkpoint schema."""
    template = (
        "ALTER ROLE oraiflow_agent_checkpoint WITH LOGIN NOINHERIT NOSUPERUSER "
        "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5 PASSWORD %L"
        if role_exists
        else "CREATE ROLE oraiflow_agent_checkpoint LOGIN NOINHERIT NOSUPERUSER "
        "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5 PASSWORD %L"
    )
    return conn.execute(
        text("SELECT format(CAST(:template AS text), CAST(:password AS text))"),
        {"template": template, "password": password},
    ).scalar_one()


def ensure_runtime_schema() -> None:
    """Apply additive banking, MCP-role and agent-checkpoint migrations."""
    with get_engine().begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(730302)"))
        # MCP is a read capability boundary: give it a distinct database role
        # restricted to the two synthetic read models used by its tools.
        mcp_password = os.getenv("MCP_DATABASE_PASSWORD", "local-only-read-change-me")
        role_exists = (
            conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname='oraiflow_mcp_reader'")).first()
            is not None
        )
        role_ddl = _build_mcp_role_ddl(conn, mcp_password, role_exists)
        conn.exec_driver_sql(role_ddl)
        conn.execute(text("REVOKE ALL PRIVILEGES ON SCHEMA public FROM oraiflow_mcp_reader"))
        conn.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        conn.execute(text("GRANT USAGE ON SCHEMA public TO oraiflow_mcp_reader"))
        conn.execute(
            text("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM oraiflow_mcp_reader")
        )
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS contacts (
                contact_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                full_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                bank_name TEXT NOT NULL,
                account_reference_masked TEXT NOT NULL,
                currency CHAR(3) NOT NULL DEFAULT 'EUR',
                status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','INACTIVE')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(owner_id, tenant_id, contact_id)
            )
        """))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_contacts_owner_name ON contacts(owner_id, tenant_id, normalized_name)"
            )
        )
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS demo_loans (
                loan_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                product_name TEXT NOT NULL,
                principal_minor BIGINT NOT NULL CHECK (principal_minor > 0),
                outstanding_principal_minor BIGINT NOT NULL CHECK (outstanding_principal_minor >= 0),
                currency CHAR(3) NOT NULL,
                annual_interest_rate_bps INTEGER NOT NULL CHECK (annual_interest_rate_bps >= 0),
                term_months INTEGER NOT NULL CHECK (term_months BETWEEN 1 AND 600),
                monthly_payment_minor BIGINT NOT NULL CHECK (monthly_payment_minor >= 0),
                total_interest_minor BIGINT NOT NULL CHECK (total_interest_minor >= 0),
                total_repayment_minor BIGINT NOT NULL CHECK (total_repayment_minor > 0),
                disbursement_date DATE NOT NULL,
                first_due_date DATE NOT NULL,
                status TEXT NOT NULL CHECK (status = 'ILLUSTRATIVE'),
                demo_only BOOLEAN NOT NULL CHECK (demo_only = TRUE),
                payment_tracking TEXT NOT NULL CHECK (payment_tracking = 'NOT_TRACKED'),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(owner_id, tenant_id, loan_id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS demo_loan_installments (
                loan_id TEXT NOT NULL REFERENCES demo_loans(loan_id) ON DELETE CASCADE,
                installment_number INTEGER NOT NULL CHECK (installment_number > 0),
                due_date DATE NOT NULL,
                payment_minor BIGINT NOT NULL CHECK (payment_minor >= 0),
                principal_minor BIGINT NOT NULL CHECK (principal_minor >= 0),
                interest_minor BIGINT NOT NULL CHECK (interest_minor >= 0),
                status TEXT NOT NULL CHECK (status = 'ILLUSTRATIVE'),
                PRIMARY KEY(loan_id, installment_number)
            )
        """))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_demo_loans_owner ON demo_loans(owner_id, tenant_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_demo_loan_due ON demo_loan_installments(loan_id, due_date)"
            )
        )
        conn.execute(
            text(
                "GRANT SELECT ON TABLE accounts, transactions, contacts, demo_loans, demo_loan_installments TO oraiflow_mcp_reader"
            )
        )
        database_grant = conn.execute(
            text(
                "SELECT format('GRANT CONNECT ON DATABASE %I TO oraiflow_mcp_reader', current_database())"
            )
        ).scalar_one()
        conn.exec_driver_sql(database_grant)

        # LangGraph checkpoint persistence is isolated from the banking tables.
        # The Agent Runtime receives this schema-scoped principal, never the
        # Banking Core DATABASE_URL or the MCP read principal.
        checkpoint_password = os.getenv(
            "AGENT_CHECKPOINT_PASSWORD", "local-agent-checkpoint-change-me"
        )
        checkpoint_role_exists = (
            conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname='oraiflow_agent_checkpoint'")
            ).first()
            is not None
        )
        checkpoint_role_ddl = _build_checkpoint_role_ddl(
            conn, checkpoint_password, checkpoint_role_exists
        )
        conn.exec_driver_sql(checkpoint_role_ddl)
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS oraiflow_agent AUTHORIZATION CURRENT_USER"))
        conn.execute(text("REVOKE ALL ON SCHEMA oraiflow_agent FROM PUBLIC"))
        conn.execute(text("REVOKE ALL PRIVILEGES ON SCHEMA public FROM oraiflow_agent_checkpoint"))
        conn.execute(
            text(
                "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM oraiflow_agent_checkpoint"
            )
        )
        conn.execute(
            text(
                "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM oraiflow_agent_checkpoint"
            )
        )
        conn.execute(
            text("GRANT USAGE, CREATE ON SCHEMA oraiflow_agent TO oraiflow_agent_checkpoint")
        )
        checkpoint_database_grant = conn.execute(
            text(
                "SELECT format('GRANT CONNECT ON DATABASE %I TO oraiflow_agent_checkpoint', current_database())"
            )
        ).scalar_one()
        conn.exec_driver_sql(checkpoint_database_grant)
        conn.execute(
            text(
                "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'tenant-demo'"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE transfer_commands ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'tenant-demo'"
            )
        )
        conn.execute(text("ALTER TABLE transfer_commands ADD COLUMN IF NOT EXISTS preview_id TEXT"))
        conn.execute(
            text("ALTER TABLE transfer_commands ADD COLUMN IF NOT EXISTS preview_digest TEXT")
        )
        conn.execute(
            text("ALTER TABLE transfer_commands ADD COLUMN IF NOT EXISTS approval_id TEXT")
        )
        conn.execute(
            text("ALTER TABLE transfer_commands ADD COLUMN IF NOT EXISTS outbox_event_id BIGINT")
        )
        conn.execute(
            text(
                "ALTER TABLE transfer_commands ADD COLUMN IF NOT EXISTS beneficiary_contact_id TEXT"
            )
        )
        conn.execute(text("ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS tenant_id TEXT"))
        conn.execute(text("ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS previous_hash TEXT"))
        conn.execute(text("ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS event_hash TEXT"))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS transfer_previews (
                preview_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                from_account_id TEXT NOT NULL REFERENCES accounts(account_id),
                beneficiary TEXT NOT NULL,
                amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
                currency CHAR(3) NOT NULL,
                fee_minor BIGINT NOT NULL DEFAULT 0,
                total_minor BIGINT NOT NULL,
                policy_version TEXT NOT NULL,
                policy_decision TEXT NOT NULL CHECK (policy_decision IN ('APPROVED','REJECTED')),
                policy_details JSONB NOT NULL DEFAULT '{}'::jsonb,
                payload_digest TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                consumed_at TIMESTAMPTZ,
                consumed_by_command_id TEXT REFERENCES transfer_commands(command_id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(
            text(
                "ALTER TABLE transfer_previews ADD COLUMN IF NOT EXISTS beneficiary_contact_id TEXT"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_transfer_previews_owner ON transfer_previews(user_id, tenant_id, created_at DESC)"
            )
        )
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS transfer_approvals (
                approval_id TEXT PRIMARY KEY,
                preview_id TEXT NOT NULL REFERENCES transfer_previews(preview_id),
                user_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                authentication_method TEXT NOT NULL,
                approved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
                UNIQUE (preview_id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ledger_reconciliations (
                command_id TEXT PRIMARY KEY REFERENCES transfer_commands(command_id),
                status TEXT NOT NULL CHECK (status IN ('MATCHED','MISMATCH','REQUIRES_RECONCILIATION')),
                transaction_id TEXT REFERENCES transactions(transaction_id),
                amount_minor BIGINT NOT NULL,
                currency CHAR(3) NOT NULL,
                evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
                reconciled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS audit_chain_state (
                tenant_id TEXT PRIMARY KEY,
                head_event_id BIGINT,
                head_hash TEXT NOT NULL DEFAULT REPEAT('0', 64)
            )
        """))

        # Customer-editable demo profile is separate from immutable identity claims.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS customer_profiles (
                user_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                preferred_name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                preferred_currency CHAR(3) NOT NULL DEFAULT 'EUR',
                language TEXT NOT NULL DEFAULT 'en',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, tenant_id)
            )
        """))
        demo_user = os.getenv("DEMO_USER_ID", "user-001")
        demo_tenant = os.getenv("DEMO_TENANT_ID", "tenant-demo")
        demo_name = os.getenv("DEMO_DISPLAY_NAME", "Alex Morgan").strip() or "Demo Customer"
        conn.execute(
            text("""
            INSERT INTO customer_profiles(user_id, tenant_id, display_name)
            VALUES (:user, :tenant, :name)
            ON CONFLICT (user_id, tenant_id) DO NOTHING
        """),
            {"user": demo_user, "tenant": demo_tenant, "name": demo_name},
        )

        # A demo-only loan is deliberately stored outside the deposit accounts
        # table: it is a liability and can never be treated as spendable funds or
        # as a transfer source. Its dates/status are illustrative, not tracked.
        existing_loan = conn.execute(
            text("""
            SELECT loan_id FROM demo_loans
             WHERE owner_id=:owner AND tenant_id=:tenant
             ORDER BY created_at LIMIT 1
        """),
            {"owner": demo_user, "tenant": demo_tenant},
        ).first()
        if demo_loan_enabled() and not existing_loan:
            scoped_loan_id = (
                "loan-demo-"
                + hashlib.sha256(
                    f"{demo_user}|{demo_tenant}|illustrative-personal-loan".encode()
                ).hexdigest()[:20]
            )
            try:
                principal_minor = int(os.getenv("DEMO_LOAN_PRINCIPAL_MINOR", "500000"))
                apr_bps = int(os.getenv("DEMO_LOAN_APR_BPS", "690"))
                term_months = int(os.getenv("DEMO_LOAN_TERM_MONTHS", "24"))
                loan = build_demo_loan_record(
                    loan_id=scoped_loan_id,
                    principal_minor=principal_minor,
                    apr_bps=apr_bps,
                    term_months=term_months,
                    disbursement_date=date.today(),
                )
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid illustrative loan configuration: {exc}") from exc
            loan_row = {key: value for key, value in loan.items() if key != "installments"}
            loan_row["disbursement_date"] = date.fromisoformat(loan["disbursement_date"])
            loan_row["first_due_date"] = date.fromisoformat(loan["first_due_date"])
            conn.execute(
                text("""
                INSERT INTO demo_loans(loan_id, owner_id, tenant_id, product_name,
                    principal_minor, outstanding_principal_minor, currency,
                    annual_interest_rate_bps, term_months, monthly_payment_minor,
                    total_interest_minor, total_repayment_minor, disbursement_date,
                    first_due_date, status, demo_only, payment_tracking)
                VALUES (:loan_id, :owner, :tenant, :product_name, :principal_minor,
                    :outstanding_principal_minor, :currency, :annual_interest_rate_bps,
                    :term_months, :monthly_payment_minor, :total_interest_minor,
                    :total_repayment_minor, :disbursement_date, :first_due_date,
                    'ILLUSTRATIVE', TRUE, 'NOT_TRACKED')
                ON CONFLICT (loan_id) DO NOTHING
            """),
                {**loan_row, "owner": demo_user, "tenant": demo_tenant},
            )
            for installment in loan["installments"]:
                conn.execute(
                    text("""
                    INSERT INTO demo_loan_installments(loan_id, installment_number, due_date,
                        payment_minor, principal_minor, interest_minor, status)
                    VALUES (:loan_id, :installment_number, :due_date, :payment_minor,
                        :principal_minor, :interest_minor, 'ILLUSTRATIVE')
                    ON CONFLICT (loan_id, installment_number) DO NOTHING
                """),
                    {
                        "loan_id": loan["loan_id"],
                        **installment,
                        "due_date": date.fromisoformat(installment["due_date"]),
                    },
                )

        # Seed ten synthetic payee contacts idempotently. These are local demo
        # records, not verified external beneficiaries or real bank accounts.
        demo_contacts = [
            (
                "John Carter",
                "john.carter@example.test",
                "+420 601 000 101",
                "Northstar Bank",
                "DEMO ···· 1042",
                "EUR",
            ),
            (
                "John Mitchell",
                "john.mitchell@example.test",
                "+420 601 000 102",
                "Union Financial",
                "DEMO ···· 2781",
                "EUR",
            ),
            (
                "Anna Novak",
                "anna.novak@example.test",
                "+420 601 000 103",
                "Northstar Bank",
                "DEMO ···· 6409",
                "EUR",
            ),
            (
                "Anna Weber",
                "anna.weber@example.test",
                "+420 601 000 104",
                "Alpine Credit",
                "DEMO ···· 7715",
                "EUR",
            ),
            (
                "Michael Chen",
                "michael.chen@example.test",
                "+420 601 000 105",
                "Union Financial",
                "DEMO ···· 5238",
                "EUR",
            ),
            (
                "Sofia Rossi",
                "sofia.rossi@example.test",
                "+420 601 000 106",
                "Meridian Bank",
                "DEMO ···· 9914",
                "EUR",
            ),
            (
                "David Novak",
                "david.novak@example.test",
                "+420 601 000 107",
                "Northstar Bank",
                "DEMO ···· 1806",
                "EUR",
            ),
            (
                "Emma Wilson",
                "emma.wilson@example.test",
                "+420 601 000 108",
                "Alpine Credit",
                "DEMO ···· 3147",
                "EUR",
            ),
            (
                "Lucas Meyer",
                "lucas.meyer@example.test",
                "+420 601 000 109",
                "Meridian Bank",
                "DEMO ···· 8062",
                "EUR",
            ),
            (
                "Olivia Brown",
                "olivia.brown@example.test",
                "+420 601 000 110",
                "Union Financial",
                "DEMO ···· 4590",
                "EUR",
            ),
        ]
        for index, (full_name, email, phone, bank_name, account_ref, currency) in enumerate(
            demo_contacts, start=1
        ):
            contact_id = (
                "ct-"
                + hashlib.sha256(f"{demo_user}|{demo_tenant}|{index}".encode()).hexdigest()[:24]
            )
            conn.execute(
                text("""
                INSERT INTO contacts(
                    contact_id, owner_id, tenant_id, full_name, normalized_name,
                    email, phone, bank_name, account_reference_masked, currency, status
                ) VALUES (
                    :contact_id, :owner, :tenant, :full_name, :normalized_name,
                    :email, :phone, :bank_name, :account_ref, :currency, 'ACTIVE'
                ) ON CONFLICT (contact_id) DO NOTHING
            """),
                {
                    "contact_id": contact_id,
                    "owner": demo_user,
                    "tenant": demo_tenant,
                    "full_name": full_name,
                    "normalized_name": " ".join(full_name.casefold().split()),
                    "email": email,
                    "phone": phone,
                    "bank_name": bank_name,
                    "account_ref": account_ref,
                    "currency": currency,
                },
            )

        # Update only the known synthetic salary seed row. The amount is a
        # demo-only monthly net-pay-style illustration, not a customer's real
        # compensation or a tax calculation. Adjust the matching synthetic
        # account balance by the same delta so the transaction and balance stay
        # internally consistent. The amount comparison makes this idempotent.
        employer = os.getenv("DEMO_SALARY_EMPLOYER", "Northstar Labs").strip() or "Northstar Labs"
        try:
            target_salary_minor = parse_demo_salary_amount_minor(
                os.getenv("DEMO_SALARY_AMOUNT_MINOR")
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        salary_row = conn.execute(text("""
            SELECT transaction_id, account_id, amount_minor, currency, category, description
              FROM transactions
             WHERE transaction_id = 'txn-001'
             FOR UPDATE
        """)).mappings().first()
        if (
            salary_row
            and salary_row["category"] == "INCOME"
            and str(salary_row["currency"]).strip() == "EUR"
            and str(salary_row["description"] or "").startswith("Salary")
        ):
            account_row = conn.execute(
                text("""
                SELECT account_id
                  FROM accounts
                 WHERE account_id = :account_id AND owner_id = :owner_id
                 FOR UPDATE
            """),
                {"account_id": salary_row["account_id"], "owner_id": demo_user},
            ).first()
            salary_delta = demo_salary_balance_delta(
                int(salary_row["amount_minor"]), target_salary_minor
            )
            if account_row and salary_delta != 0:
                conn.execute(
                    text("""
                    UPDATE accounts
                       SET balance_minor = balance_minor + :delta
                     WHERE account_id = :account_id AND owner_id = :owner_id
                """),
                    {
                        "delta": salary_delta,
                        "account_id": salary_row["account_id"],
                        "owner_id": demo_user,
                    },
                )
                conn.execute(
                    text("""
                    UPDATE transactions
                       SET amount_minor = :target, description = 'Salary · ' || :employer
                     WHERE transaction_id = 'txn-001' AND amount_minor = :old_amount
                       AND category = 'INCOME' AND currency = 'EUR'
                """),
                    {
                        "target": target_salary_minor,
                        "employer": employer,
                        "old_amount": int(salary_row["amount_minor"]),
                    },
                )
            else:
                conn.execute(
                    text("""
                    UPDATE transactions
                       SET description = 'Salary · ' || :employer
                     WHERE transaction_id = 'txn-001' AND category = 'INCOME'
                       AND currency = 'EUR' AND description LIKE 'Salary%'
                       AND description <> 'Salary · ' || :employer
                """),
                    {"employer": employer},
                )

        # This opt-in, local-only fixture extends the sample analytics dataset.
        # Stable transaction IDs and ON CONFLICT DO NOTHING make the migration
        # repeatable without moving or rewriting previously seeded activity.
        # It never changes account balance snapshots and is disabled by default
        # outside the documented local Compose demo configuration.
        if demo_transaction_history_enabled():
            history_rows = build_demo_transaction_history(
                anchor=datetime.now(timezone.utc),
                employer=employer,
                salary_amount_minor=target_salary_minor,
            )
            for row in history_rows:
                insert_parameters = {
                    key: row[key]
                    for key in (
                        "transaction_id",
                        "account_id",
                        "description",
                        "category",
                        "amount_minor",
                        "currency",
                        "status",
                        "occurred_at",
                    )
                }
                conn.execute(
                    text("""
                    INSERT INTO transactions(
                        transaction_id, account_id, description, category,
                        amount_minor, currency, status, occurred_at
                    )
                    SELECT :transaction_id, a.account_id, :description, :category,
                           :amount_minor, :currency, :status, :occurred_at
                      FROM accounts a
                     WHERE a.account_id = :account_id AND a.owner_id = :owner_id
                    ON CONFLICT (transaction_id) DO NOTHING
                """),
                    {**insert_parameters, "owner_id": demo_user},
                )

        # Existing demo accounts inherit the configured local tenant. Do not
        # accept tenant identity from the browser or an LLM tool argument.
        conn.execute(
            text(
                "UPDATE accounts SET tenant_id=:tenant WHERE tenant_id IS NULL OR tenant_id='tenant-demo'"
            ),
            {"tenant": os.getenv("DEMO_TENANT_ID", "tenant-demo")},
        )
        conn.execute(
            text(
                "UPDATE transfer_commands SET tenant_id=:tenant WHERE tenant_id IS NULL OR tenant_id='tenant-demo'"
            ),
            {"tenant": os.getenv("DEMO_TENANT_ID", "tenant-demo")},
        )
        conn.execute(
            text("UPDATE audit_events SET tenant_id=:tenant WHERE tenant_id IS NULL"),
            {"tenant": os.getenv("DEMO_TENANT_ID", "tenant-demo")},
        )
        _backfill_audit_hashes(conn)

        # Reject in-place changes to historical evidence. Hash chaining makes
        # subsequent tampering detectable; production also needs WORM/replication
        # and separation of database-administrator privileges.
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION oraiflow_reject_audit_mutation() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'audit_events is append-only';
            END;
            $$ LANGUAGE plpgsql
        """))
        conn.execute(text("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_events_immutable'
                ) THEN
                    CREATE TRIGGER trg_audit_events_immutable
                    BEFORE UPDATE OR DELETE ON audit_events
                    FOR EACH ROW EXECUTE FUNCTION oraiflow_reject_audit_mutation();
                END IF;
            END $$
        """))
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION oraiflow_guard_preview_mutation() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'transfer_previews cannot be deleted';
                END IF;
                IF ROW(OLD.preview_id, OLD.user_id, OLD.tenant_id, OLD.from_account_id,
                       OLD.beneficiary, OLD.amount_minor, OLD.currency, OLD.fee_minor,
                       OLD.total_minor, OLD.policy_version, OLD.policy_decision,
                       OLD.policy_details, OLD.payload_digest, OLD.expires_at, OLD.created_at)
                   IS DISTINCT FROM
                   ROW(NEW.preview_id, NEW.user_id, NEW.tenant_id, NEW.from_account_id,
                       NEW.beneficiary, NEW.amount_minor, NEW.currency, NEW.fee_minor,
                       NEW.total_minor, NEW.policy_version, NEW.policy_decision,
                       NEW.policy_details, NEW.payload_digest, NEW.expires_at, NEW.created_at) THEN
                    RAISE EXCEPTION 'transfer preview payload is immutable';
                END IF;
                IF OLD.consumed_at IS NOT NULL OR OLD.consumed_by_command_id IS NOT NULL THEN
                    IF ROW(OLD.consumed_at, OLD.consumed_by_command_id)
                       IS DISTINCT FROM ROW(NEW.consumed_at, NEW.consumed_by_command_id) THEN
                        RAISE EXCEPTION 'transfer preview consumption is immutable';
                    END IF;
                ELSIF (NEW.consumed_at IS NULL) <> (NEW.consumed_by_command_id IS NULL) THEN
                    RAISE EXCEPTION 'preview consumption fields must be set together';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """))
        conn.execute(text("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_transfer_previews_immutable') THEN
                    CREATE TRIGGER trg_transfer_previews_immutable
                    BEFORE UPDATE OR DELETE ON transfer_previews
                    FOR EACH ROW EXECUTE FUNCTION oraiflow_guard_preview_mutation();
                END IF;
            END $$
        """))
        conn.execute(text("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_transfer_approvals_immutable') THEN
                    CREATE TRIGGER trg_transfer_approvals_immutable
                    BEFORE UPDATE OR DELETE ON transfer_approvals
                    FOR EACH ROW EXECUTE FUNCTION oraiflow_reject_audit_mutation();
                END IF;
            END $$
        """))

        # Reconstruct the outbox for unsettled commands, including v0.1/v0.2
        # commands created before a worker dispatch completed.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS transfer_outbox (
                event_id BIGSERIAL PRIMARY KEY,
                event_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING', 'PROCESSING', 'DISPATCHED')),
                attempts INTEGER NOT NULL DEFAULT 0,
                claimed_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dispatched_at TIMESTAMPTZ,
                last_error TEXT,
                UNIQUE (event_type, aggregate_id)
            )
        """))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_transfer_outbox_pending ON transfer_outbox(status, created_at)"
            )
        )
        # Enforce the one-preview → one-approval → one-command relationship at
        # the database boundary as well as in the API transaction.
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_transfer_commands_preview_id ON transfer_commands(preview_id) WHERE preview_id IS NOT NULL"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_transfer_commands_approval_id ON transfer_commands(approval_id) WHERE approval_id IS NOT NULL"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_transfer_commands_outbox_event_id ON transfer_commands(outbox_event_id) WHERE outbox_event_id IS NOT NULL"
            )
        )
        conn.execute(text("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_transfer_commands_preview') THEN
                    ALTER TABLE transfer_commands ADD CONSTRAINT fk_transfer_commands_preview
                        FOREIGN KEY (preview_id) REFERENCES transfer_previews(preview_id);
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_transfer_commands_approval') THEN
                    ALTER TABLE transfer_commands ADD CONSTRAINT fk_transfer_commands_approval
                        FOREIGN KEY (approval_id) REFERENCES transfer_approvals(approval_id);
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_transfer_previews_consumed_command') THEN
                    ALTER TABLE transfer_previews ADD CONSTRAINT fk_transfer_previews_consumed_command
                        FOREIGN KEY (consumed_by_command_id) REFERENCES transfer_commands(command_id);
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_ledger_reconciliations_transaction') THEN
                    ALTER TABLE ledger_reconciliations ADD CONSTRAINT fk_ledger_reconciliations_transaction
                        FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id);
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_transfer_commands_outbox') THEN
                    ALTER TABLE transfer_commands ADD CONSTRAINT fk_transfer_commands_outbox
                        FOREIGN KEY (outbox_event_id) REFERENCES transfer_outbox(event_id);
                END IF;
            END $$
        """))
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION oraiflow_reject_immutable_truncate() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '% is append-only and cannot be truncated', TG_TABLE_NAME;
            END;
            $$ LANGUAGE plpgsql
        """))
        for table_name, trigger_name in (
            ("audit_events", "trg_audit_events_no_truncate"),
            ("transfer_approvals", "trg_transfer_approvals_no_truncate"),
            ("transfer_previews", "trg_transfer_previews_no_truncate"),
        ):
            conn.execute(text(f"""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='{trigger_name}') THEN
                        CREATE TRIGGER {trigger_name}
                        BEFORE TRUNCATE ON {table_name}
                        FOR EACH STATEMENT EXECUTE FUNCTION oraiflow_reject_immutable_truncate();
                    END IF;
                END $$
            """))
        conn.execute(text("""
            INSERT INTO transfer_outbox(event_type, aggregate_id, payload)
            SELECT 'TRANSFER_ACCEPTED', command_id,
                   jsonb_build_object('command_id', CAST(command_id AS text))
              FROM transfer_commands
             WHERE status IN ('PENDING', 'EXECUTING')
            ON CONFLICT (event_type, aggregate_id) DO NOTHING
        """))


def money(minor: int, currency: str = "EUR") -> str:
    """Convert integer minor units into a currency-aware decimal amount."""
    return f"{minor / 100:,.2f} {currency}"
