"""Temporal worker process and PostgreSQL outbox dispatch lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import text
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from shared.database import ensure_runtime_schema, get_engine
from shared.audit import append_audit_event
from services.worker.workflows import (
    TransferWorkflow, mark_transfer_executing, mark_transfer_requires_reconciliation, settle_transfer,
)

logger = logging.getLogger("oraiflow.worker")


def claim_outbox_event() -> dict | None:
    """Claim one event with a short lease so a crashed worker can recover it."""
    with get_engine().begin() as conn:
        row = conn.execute(text("""
            WITH candidate AS (
                SELECT event_id
                FROM transfer_outbox
                WHERE status = 'PENDING'
                   OR (status = 'PROCESSING' AND claimed_at < NOW() - INTERVAL '30 seconds')
                ORDER BY created_at, event_id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE transfer_outbox AS outbox
               SET status='PROCESSING', claimed_at=NOW(), attempts=attempts+1
              FROM candidate
             WHERE outbox.event_id=candidate.event_id
            RETURNING outbox.event_id, outbox.aggregate_id, outbox.payload
        """)).mappings().first()
        return dict(row) if row else None




def requeue_unsettled_outbox_events() -> int:
    """Reconcile pending commands after worker or disposable Temporal restarts.

    In this local demo, Temporal history lives in the container's /tmp. If the
    Temporal container was recreated, a previously dispatched workflow history
    may be gone while PostgreSQL still correctly says the command is PENDING.
    Requeue it; the stable workflow ID makes this safe when history still exists.
    """
    with get_engine().begin() as conn:
        result = conn.execute(text("""
            UPDATE transfer_outbox AS outbox
               SET status='PENDING', claimed_at=NULL, last_error=NULL
              FROM transfer_commands AS command
             WHERE outbox.aggregate_id=command.command_id
               AND command.status IN ('PENDING', 'EXECUTING')
               AND outbox.status IN ('PROCESSING', 'DISPATCHED')
        """))
        return int(result.rowcount or 0)


def mark_outbox_dispatched(event_id: int, command_id: str, workflow_id: str) -> None:
    """Mark an outbox event as dispatched to its Temporal workflow."""
    with get_engine().begin() as conn:
        conn.execute(text("""
            UPDATE transfer_outbox
               SET status='DISPATCHED', dispatched_at=NOW(), claimed_at=NULL, last_error=NULL
             WHERE event_id=:event_id
        """), {"event_id": event_id})
        command = conn.execute(text("""
            UPDATE transfer_commands
               SET temporal_workflow_id=:workflow_id, outbox_event_id=:event_id, updated_at=NOW()
             WHERE command_id=:command_id
            RETURNING user_id, tenant_id
        """), {"workflow_id": workflow_id, "event_id": event_id, "command_id": command_id}).mappings().first()
        if command:
            append_audit_event(conn, "system:temporal-dispatcher", "TRANSFER_WORKFLOW_DISPATCHED", command_id,
                {"outbox_event_id": event_id, "workflow_id": workflow_id,
                 "task_queue": os.getenv("TEMPORAL_TASK_QUEUE", "oraiflow-transfers")}, command["tenant_id"])


def release_outbox_event(event_id: int, error: Exception) -> None:
    """Release a claimed outbox event after a dispatch failure."""
    with get_engine().begin() as conn:
        conn.execute(text("""
            UPDATE transfer_outbox
               SET status='PENDING', claimed_at=NULL, last_error=:error
             WHERE event_id=:event_id
        """), {"event_id": event_id, "error": f"{type(error).__name__}: {error}"[:1000]})


async def dispatch_outbox(client: Client, task_queue: str) -> None:
    """Dispatch committed commands to Temporal; the outbox is the retry source."""
    while True:
        event = await asyncio.to_thread(claim_outbox_event)
        if not event:
            await asyncio.sleep(1.0)
            continue
        command_id = str(event["aggregate_id"])
        workflow_id = f"transfer-{command_id}"
        try:
            try:
                await client.start_workflow(
                    TransferWorkflow.run,
                    command_id,
                    id=workflow_id,
                    task_queue=task_queue,
                )
            except WorkflowAlreadyStartedError:
                # API/client retries and dispatcher lease recovery are idempotent.
                pass
            await asyncio.to_thread(mark_outbox_dispatched, event["event_id"], command_id, workflow_id)
            logger.info("Dispatched transfer command=%s workflow=%s", command_id, workflow_id)
        except Exception as exc:
            logger.exception("Outbox dispatch failed for command=%s", command_id)
            await asyncio.to_thread(release_outbox_event, event["event_id"], exc)
            await asyncio.sleep(1.0)


async def main() -> None:
    """Start the Temporal worker and its outbox polling loop."""
    address = os.getenv("TEMPORAL_ADDRESS", "temporal:7233")
    task_queue = os.getenv("TEMPORAL_TASK_QUEUE", "oraiflow-transfers")
    await asyncio.to_thread(ensure_runtime_schema)

    while True:
        try:
            client = await Client.connect(address)
            requeued = await asyncio.to_thread(requeue_unsettled_outbox_events)
            if requeued:
                logger.warning("Requeued %s unsettled transfer event(s) for Temporal reconciliation", requeued)
            logger.info("Connected to Temporal at %s; task queue=%s", address, task_queue)
            worker = Worker(
                client,
                task_queue=task_queue,
                workflows=[TransferWorkflow],
                activities=[mark_transfer_executing, settle_transfer, mark_transfer_requires_reconciliation],
            )
            worker_task = asyncio.create_task(worker.run())
            dispatcher_task = asyncio.create_task(dispatch_outbox(client, task_queue))
            done, pending = await asyncio.wait(
                {worker_task, dispatcher_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
            logger.warning("Worker or dispatcher stopped; reconnecting to Temporal")
        except Exception as exc:
            logger.exception("Temporal worker or outbox dispatcher stopped: %s", type(exc).__name__)
            await asyncio.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    asyncio.run(main())
