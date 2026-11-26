"""Runtime switches for explicitly illustrative demo records.

Demo fixtures are opt-in. They must never be silently presented as a customer's
actual borrowing or included in personal financial-health calculations.
"""

from __future__ import annotations

import os


def demo_loan_enabled() -> bool:
    """Return whether the illustrative synthetic loan is enabled."""
    value = str(os.getenv("DEMO_LOAN_ENABLED", "false")).strip().casefold()
    return value in {"1", "true", "yes", "on"}


def demo_transaction_history_enabled() -> bool:
    """Whether the local sample database receives extended synthetic activity."""
    value = str(os.getenv("DEMO_TRANSACTION_HISTORY_ENABLED", "false")).strip().casefold()
    return value in {"1", "true", "yes", "on"}
