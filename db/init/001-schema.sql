CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'tenant-demo',
    account_name TEXT NOT NULL,
    account_type TEXT NOT NULL DEFAULT 'CURRENT',
    masked_number TEXT NOT NULL,
    currency CHAR(3) NOT NULL DEFAULT 'EUR',
    balance_minor BIGINT NOT NULL CHECK (balance_minor >= 0),
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


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
);
CREATE INDEX IF NOT EXISTS idx_contacts_owner_name
    ON contacts(owner_id, tenant_id, normalized_name);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    description TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'OTHER',
    amount_minor BIGINT NOT NULL,
    currency CHAR(3) NOT NULL DEFAULT 'EUR',
    status TEXT NOT NULL DEFAULT 'BOOKED',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_transactions_account_time
    ON transactions(account_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS transfer_commands (
    command_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'tenant-demo',
    from_account_id TEXT NOT NULL REFERENCES accounts(account_id),
    beneficiary TEXT NOT NULL,
    beneficiary_contact_id TEXT,
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    currency CHAR(3) NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    temporal_workflow_id TEXT,
    error_message TEXT,
    preview_id TEXT,
    preview_digest TEXT,
    approval_id TEXT,
    outbox_event_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id BIGSERIAL PRIMARY KEY,
    actor_id TEXT NOT NULL,
    tenant_id TEXT,
    event_type TEXT NOT NULL,
    subject_id TEXT,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    previous_hash TEXT,
    event_hash TEXT
);

CREATE TABLE IF NOT EXISTS transfer_previews (
    preview_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    from_account_id TEXT NOT NULL REFERENCES accounts(account_id),
    beneficiary TEXT NOT NULL,
    beneficiary_contact_id TEXT,
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
);

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
);

CREATE TABLE IF NOT EXISTS ledger_reconciliations (
    command_id TEXT PRIMARY KEY REFERENCES transfer_commands(command_id),
    status TEXT NOT NULL CHECK (status IN ('MATCHED','MISMATCH','REQUIRES_RECONCILIATION')),
    transaction_id TEXT REFERENCES transactions(transaction_id),
    amount_minor BIGINT NOT NULL,
    currency CHAR(3) NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    reconciled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_chain_state (
    tenant_id TEXT PRIMARY KEY,
    head_event_id BIGINT,
    head_hash TEXT NOT NULL DEFAULT REPEAT('0', 64)
);

INSERT INTO accounts(account_id, owner_id, account_name, account_type, masked_number, currency, balance_minor)
VALUES
 ('acc-001', 'user-001', 'Everyday account', 'CURRENT', '•••• 4821', 'EUR', 2018080),
 ('acc-002', 'user-001', 'Savings account', 'SAVINGS', '•••• 9034', 'EUR', 865000),
 ('acc-003', 'user-001', 'Travel wallet', 'CURRENT', '•••• 1172', 'EUR', 248750)
ON CONFLICT (account_id) DO NOTHING;

INSERT INTO transactions(transaction_id, account_id, description, category, amount_minor, currency, occurred_at)
VALUES
 ('txn-001', 'acc-001', 'Salary · Northstar Labs', 'INCOME', 1100000, 'EUR', NOW() - INTERVAL '1 day'),
 ('txn-002', 'acc-001', 'Blue Bottle Coffee', 'FOOD', -485, 'EUR', NOW() - INTERVAL '2 days'),
 ('txn-003', 'acc-001', 'Metro transit pass', 'TRANSPORT', -3200, 'EUR', NOW() - INTERVAL '3 days'),
 ('txn-004', 'acc-001', 'Market & Co.', 'GROCERIES', -6840, 'EUR', NOW() - INTERVAL '4 days'),
 ('txn-005', 'acc-001', 'Cloudstream subscription', 'SUBSCRIPTIONS', -1299, 'EUR', NOW() - INTERVAL '5 days'),
 ('txn-006', 'acc-002', 'Monthly savings transfer', 'TRANSFER', 15000, 'EUR', NOW() - INTERVAL '6 days'),
 ('txn-007', 'acc-003', 'Hotel Aria', 'TRAVEL', -12200, 'EUR', NOW() - INTERVAL '7 days')
ON CONFLICT (transaction_id) DO NOTHING;

-- Durable command-to-workflow handoff. Also created by ensure_runtime_schema()
-- so existing PostgreSQL volumes receive this additive migration automatically.
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
);
CREATE INDEX IF NOT EXISTS idx_transfer_outbox_pending
    ON transfer_outbox(status, created_at);
