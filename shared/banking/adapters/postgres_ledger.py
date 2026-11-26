"""Synthetic PostgreSQL ledger adapter used by the local reference deployment."""
from __future__ import annotations

import json
from typing import Any, Mapping

from sqlalchemy import text

from shared.banking.ports import SettlementResult


class SyntheticPostgresLedgerAdapter:
    """Implements LedgerPort against ORAIflow's synthetic PostgreSQL ledger only."""

    def settle_transfer(self, conn: Any, command: Mapping[str, Any]) -> SettlementResult:
        """Apply a validated synthetic transfer settlement within the active database transaction."""
        account = conn.execute(
            text("SELECT * FROM accounts WHERE account_id=:id FOR UPDATE"),
            {"id": command["from_account_id"]},
        ).mappings().first()
        if not account or account["owner_id"] != command["user_id"] or account["tenant_id"] != command["tenant_id"]:
            return SettlementResult(status="FAILED", reason="Account not accessible")
        amount_minor = int(command["amount_minor"])
        currency = str(command["currency"]).strip()
        if account["currency"].strip() != currency or int(account["balance_minor"]) < amount_minor:
            return SettlementResult(status="FAILED", reason="Insufficient funds or currency mismatch")

        transaction_id = f"txn-{command['command_id']}"
        conn.execute(
            text("UPDATE accounts SET balance_minor=balance_minor-:amount WHERE account_id=:id"),
            {"amount": amount_minor, "id": command["from_account_id"]},
        )
        conn.execute(text("""
            INSERT INTO transactions(transaction_id, account_id, description, category,
                                     amount_minor, currency, status)
            VALUES (:txn, :account, :description, 'TRANSFER', :amount,
                    CAST(:currency AS text), 'BOOKED')
            ON CONFLICT (transaction_id) DO NOTHING
        """), {
            "txn": transaction_id,
            "account": command["from_account_id"],
            "description": f"Transfer to {command['beneficiary']}",
            "amount": -amount_minor,
            "currency": currency,
        })
        conn.execute(text("""
            INSERT INTO ledger_reconciliations(command_id, status, transaction_id,
                                               amount_minor, currency, evidence)
            VALUES (:command_id, 'MATCHED', :transaction_id, :amount_minor,
                    CAST(:currency AS text), CAST(:evidence AS jsonb))
            ON CONFLICT (command_id) DO NOTHING
        """), {
            "command_id": command["command_id"],
            "transaction_id": transaction_id,
            "amount_minor": amount_minor,
            "currency": currency,
            "evidence": json.dumps({
                "source": "synthetic-postgresql-ledger",
                "debit_transaction_id": transaction_id,
                "amount_minor": amount_minor,
                "currency": currency,
                "reconciliation": "command amount matches booked debit",
            }),
        })
        return SettlementResult(status="SETTLED", transaction_id=transaction_id)
