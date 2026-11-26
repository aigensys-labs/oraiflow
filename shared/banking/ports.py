"""Narrow institution-facing ports used by the Banking Kernel execution path."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class SettlementResult:
    """Outcome returned by the ledger settlement port after a command is applied."""

    status: str
    transaction_id: str | None = None
    reason: str | None = None


class LedgerPort(Protocol):
    """A transaction-scoped ledger capability; implementation owns ledger I/O."""

    def settle_transfer(self, conn: Any, command: Mapping[str, Any]) -> SettlementResult:
        """Apply or reject one transfer inside the caller's DB transaction."""
        ...
