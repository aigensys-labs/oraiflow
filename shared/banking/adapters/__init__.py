"""Banking Kernel adapter implementations and explicit adapter selection."""

from __future__ import annotations

import os

from shared.banking.ports import LedgerPort
from shared.banking.adapters.postgres_ledger import SyntheticPostgresLedgerAdapter


def get_ledger_adapter() -> LedgerPort:
    """Resolve the configured ledger adapter; never silently select a live bank."""
    adapter = os.getenv("BANK_LEDGER_ADAPTER", "postgres").strip().lower()
    if adapter == "postgres":
        return SyntheticPostgresLedgerAdapter()
    raise RuntimeError(f"Unsupported BANK_LEDGER_ADAPTER: {adapter}")
