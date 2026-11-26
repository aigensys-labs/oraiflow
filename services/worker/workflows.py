"""Temporal workflow definitions for the synthetic transfer demonstration."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


@activity.defn
async def mark_transfer_executing(command_id: str) -> dict:
    """Persist the transition before the settlement activity performs a debit."""
    return await asyncio.to_thread(_mark_transfer_executing_sync, command_id)


def _mark_transfer_executing_sync(command_id: str) -> dict:
    from sqlalchemy import text
    from shared.database import get_engine

    from shared.audit import append_audit_event

    with get_engine().begin() as conn:
        row = (
            conn.execute(
                text("""
            UPDATE transfer_commands
               SET status='EXECUTING', updated_at=NOW()
             WHERE command_id=:id AND status='PENDING'
            RETURNING status
        """),
                {"id": command_id},
            )
            .mappings()
            .first()
        )
        if row:
            command = (
                conn.execute(
                    text("SELECT user_id, tenant_id FROM transfer_commands WHERE command_id=:id"),
                    {"id": command_id},
                )
                .mappings()
                .first()
            )
            append_audit_event(
                conn,
                command["user_id"],
                "TRANSFER_EXECUTING",
                command_id,
                {"source": "temporal_workflow"},
                command["tenant_id"],
            )
            return {"command_id": command_id, "status": row["status"], "transitioned": True}
        current = (
            conn.execute(
                text("SELECT status FROM transfer_commands WHERE command_id=:id"),
                {"id": command_id},
            )
            .mappings()
            .first()
        )
        if not current:
            raise ValueError(f"Transfer command {command_id} was not found")
        if current["status"] in ("EXECUTING", "SETTLED"):
            return {"command_id": command_id, "status": current["status"], "transitioned": False}
        if current["status"] == "FAILED":
            return {"command_id": command_id, "status": "FAILED", "transitioned": False}
        raise RuntimeError(f"Transfer command cannot execute from state {current['status']}")


@activity.defn
async def settle_transfer(command_id: str) -> dict:
    """Idempotently settle one synthetic transfer in the demo ledger."""
    return await asyncio.to_thread(_settle_transfer_sync, command_id)


def _settle_transfer_sync(command_id: str) -> dict:
    from sqlalchemy import text
    from shared.database import get_engine
    from shared.audit import append_audit_event

    with get_engine().begin() as conn:
        command = (
            conn.execute(
                text("SELECT * FROM transfer_commands WHERE command_id = :id FOR UPDATE"),
                {"id": command_id},
            )
            .mappings()
            .first()
        )
        if not command:
            raise ValueError(f"Transfer command {command_id} was not found")
        if command["status"] == "SETTLED":
            return {"command_id": command_id, "status": "SETTLED", "idempotent_replay": True}
        if command["status"] == "FAILED":
            return {"command_id": command_id, "status": "FAILED"}
        if command["status"] not in ("PENDING", "EXECUTING"):
            raise RuntimeError(f"Transfer command cannot settle from state {command['status']}")

        from shared.banking.adapters import get_ledger_adapter

        # Banking Kernel calls an institution-facing port. The configured local
        # adapter is synthetic PostgreSQL; a real institution adapter must be
        # introduced explicitly and preserve idempotency/reconciliation.
        settlement = get_ledger_adapter().settle_transfer(conn, command)
        if settlement.status != "SETTLED":
            conn.execute(
                text("""
                UPDATE transfer_commands
                   SET status='FAILED', error_message=:reason, updated_at=NOW()
                 WHERE command_id=:id
            """),
                {"reason": settlement.reason, "id": command_id},
            )
            append_audit_event(
                conn,
                command["user_id"],
                "TRANSFER_FAILED",
                command_id,
                {"reason": settlement.reason, "stage": "settlement"},
                command["tenant_id"],
            )
            return {"command_id": command_id, "status": "FAILED", "reason": settlement.reason}

        conn.execute(
            text(
                "UPDATE transfer_commands SET status='SETTLED', updated_at=NOW() WHERE command_id=:id"
            ),
            {"id": command_id},
        )
        append_audit_event(
            conn,
            command["user_id"],
            "TRANSFER_SETTLED",
            command_id,
            {
                "amount_minor": int(command["amount_minor"]),
                "currency": command["currency"].strip(),
                "beneficiary": command["beneficiary"],
                "source": "temporal_activity",
                "transaction_id": settlement.transaction_id,
                "reconciliation_status": "MATCHED",
            },
            command["tenant_id"],
        )
    return {
        "command_id": command_id,
        "status": "SETTLED",
        "transaction_id": settlement.transaction_id,
        "idempotent_replay": False,
    }


@activity.defn
async def mark_transfer_requires_reconciliation(command_id: str, reason: str) -> dict:
    """Record an uncertain workflow outcome without falsely calling it failed."""
    return await asyncio.to_thread(_mark_transfer_requires_reconciliation_sync, command_id, reason)


def _mark_transfer_requires_reconciliation_sync(command_id: str, reason: str) -> dict:
    from sqlalchemy import text
    from shared.database import get_engine
    from shared.audit import append_audit_event

    message = (reason or "Execution outcome could not be confirmed")[:1000]
    with get_engine().begin() as conn:
        command = (
            conn.execute(
                text("""
            UPDATE transfer_commands
               SET status='REQUIRES_RECONCILIATION', error_message=:error, updated_at=NOW()
             WHERE command_id=:id AND status IN ('PENDING','EXECUTING')
            RETURNING user_id, tenant_id
        """),
                {"error": message, "id": command_id},
            )
            .mappings()
            .first()
        )
        if command:
            append_audit_event(
                conn,
                command["user_id"],
                "TRANSFER_REQUIRES_RECONCILIATION",
                command_id,
                {
                    "reason": message,
                    "source": "temporal_workflow",
                    "automatic_retry_exhausted": True,
                },
                command["tenant_id"],
            )
            return {"command_id": command_id, "status": "REQUIRES_RECONCILIATION", "error": message}
        current = (
            conn.execute(
                text("SELECT status FROM transfer_commands WHERE command_id=:id"),
                {"id": command_id},
            )
            .mappings()
            .first()
        )
        if not current:
            raise ValueError(f"Transfer command {command_id} was not found")
        # Never overwrite a known terminal ledger outcome if the activity's
        # response was lost after the DB transaction committed.
        return {"command_id": command_id, "status": current["status"], "error": message}


@workflow.defn
class TransferWorkflow:
    """Coordinate the lifecycle of a synthetic transfer command using Temporal."""

    @workflow.run
    async def run(self, command_id: str) -> dict:
        """Execute the transfer workflow activities for one accepted command identifier."""
        try:
            transition = await workflow.execute_activity(
                mark_transfer_executing,
                command_id,
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            if transition["status"] == "FAILED":
                return transition
            return await workflow.execute_activity(
                settle_transfer,
                command_id,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"[:1000]
            return await workflow.execute_activity(
                mark_transfer_requires_reconciliation,
                command_id,
                reason,
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=15),
                    maximum_attempts=10,
                ),
            )
