"""Small deterministic helpers for synthetic demo seed adjustments."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

DEFAULT_DEMO_SALARY_AMOUNT_MINOR = 1_100_000  # EUR 11,000.00


def parse_demo_salary_amount_minor(value: str | None) -> int:
    """Parse a positive integer amount in currency minor units."""
    raw = str(value if value is not None else DEFAULT_DEMO_SALARY_AMOUNT_MINOR).strip()
    try:
        amount = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("DEMO_SALARY_AMOUNT_MINOR must be a positive integer in minor units") from exc
    if amount <= 0:
        raise ValueError("DEMO_SALARY_AMOUNT_MINOR must be a positive integer in minor units")
    return amount


def demo_salary_balance_delta(current_amount_minor: int, target_amount_minor: int) -> int:
    """Return the exact account-balance adjustment needed to mirror the salary row."""
    current = int(current_amount_minor)
    target = int(target_amount_minor)
    if current <= 0 or target <= 0:
        raise ValueError("Synthetic salary amounts must be positive")
    return target - current


# Additional illustrative activity added by the additive runtime migration.
# The original schema contributes txn-001 through txn-007; these 28 rows bring
# the default synthetic history to 35 booked transactions over roughly 63 days.
# Amounts are integer minor units and descriptions/categories are intentionally
# fictional. Account balance snapshots are not changed by this analytics seed.
DEMO_HISTORY_ROWS = (
    ("txn-demo-008", "acc-001", "Salary · {employer}", "INCOME", "salary", 31),
    ("txn-demo-009", "acc-001", "Salary · {employer}", "INCOME", "salary", 62),
    ("txn-demo-010", "acc-001", "Blue Bottle Coffee", "FOOD", -520, 16),
    ("txn-demo-011", "acc-001", "Blue Bottle Coffee", "FOOD", -485, 41),
    ("txn-demo-012", "acc-001", "Blue Bottle Coffee", "FOOD", -465, 63),
    ("txn-demo-013", "acc-001", "Metro transit pass", "TRANSPORT", -2800, 31),
    ("txn-demo-014", "acc-001", "City tram ticket", "TRANSPORT", -150, 8),
    ("txn-demo-015", "acc-001", "City tram ticket", "TRANSPORT", -150, 48),
    ("txn-demo-016", "acc-001", "City tram ticket", "TRANSPORT", -150, 58),
    ("txn-demo-017", "acc-001", "Market & Co.", "GROCERIES", -7250, 11),
    ("txn-demo-018", "acc-001", "Fresh Market", "GROCERIES", -5830, 19),
    ("txn-demo-019", "acc-001", "Market & Co.", "GROCERIES", -6420, 33),
    ("txn-demo-020", "acc-001", "Fresh Market", "GROCERIES", -5375, 44),
    ("txn-demo-021", "acc-001", "Market & Co.", "GROCERIES", -6910, 57),
    ("txn-demo-022", "acc-001", "Bistro Olive", "DINING", -2350, 13),
    ("txn-demo-023", "acc-001", "Green Bowl", "DINING", -1640, 25),
    ("txn-demo-024", "acc-001", "Bistro Olive", "DINING", -2820, 39),
    ("txn-demo-025", "acc-001", "Cinema City", "ENTERTAINMENT", -1290, 17),
    ("txn-demo-026", "acc-001", "Book Nook", "SHOPPING", -1890, 27),
    ("txn-demo-027", "acc-001", "Pharmacy Plus", "HEALTH", -870, 34),
    ("txn-demo-028", "acc-001", "Fitness Club", "HEALTH", -2999, 46),
    ("txn-demo-029", "acc-001", "Cloudstream subscription", "SUBSCRIPTIONS", -1299, 32),
    ("txn-demo-030", "acc-001", "Cloudstream subscription", "SUBSCRIPTIONS", -1299, 62),
    ("txn-demo-031", "acc-003", "Hotel Aria", "TRAVEL", -9600, 52),
    ("txn-demo-032", "acc-001", "City Fuel", "TRANSPORT", -4500, 24),
    ("txn-demo-033", "acc-001", "Transfer to Savings", "TRANSFER", -15000, 31),
    ("txn-demo-034", "acc-002", "Transfer from Everyday", "TRANSFER", 15000, 31),
    ("txn-demo-035", "acc-001", "Electric utility bill", "BILLS", -6240, 54),
)


def build_demo_transaction_history(
    *,
    anchor: datetime | None = None,
    employer: str = "Northstar Labs",
    salary_amount_minor: int = DEFAULT_DEMO_SALARY_AMOUNT_MINOR,
) -> list[dict[str, object]]:
    """Build stable-ID synthetic activity relative to one migration anchor.

    Callers persist these rows with ``ON CONFLICT DO NOTHING``. The anchor is
    supplied once per migration transaction, so reruns never shift existing
    transaction timestamps or rewrite historical evidence.
    """
    current = anchor or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    salary_minor = int(salary_amount_minor)
    if salary_minor <= 0:
        raise ValueError("Synthetic salary amount must be a positive integer in minor units")
    safe_employer = str(employer or "Northstar Labs").strip() or "Northstar Labs"
    rows: list[dict[str, object]] = []
    for transaction_id, account_id, description, category, amount_or_kind, days_ago in DEMO_HISTORY_ROWS:
        if amount_or_kind == "salary":
            description = str(description).format(employer=safe_employer)
            amount_minor = salary_minor
        else:
            amount_minor = int(amount_or_kind)
        rows.append({
            "transaction_id": transaction_id,
            "account_id": account_id,
            "description": description,
            "category": category,
            "amount_minor": amount_minor,
            "currency": "EUR",
            "status": "BOOKED",
            "occurred_at": current - timedelta(days=int(days_ago)),
            "days_ago": int(days_ago),
        })
    return rows
