#!/usr/bin/env bash
set -euo pipefail

python3 - <<'PY'
import json
import os
import time
import urllib.error
import urllib.request
import uuid

BASE = os.getenv("ORAI_BASE_URL", "http://localhost:8000").rstrip("/")
SCA_CODE = os.getenv("ORAI_DEMO_SCA_CODE", "123456")

def request(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path}: HTTP {exc.code}: {exc.read().decode()}") from exc

beneficiary = f"ORAIflow Demo {time.strftime('%H%M%S')}"
print("This smoke test creates a synthetic EUR 1.00 transfer in the local demo ledger.")
before = request("/api/overview")
before_balance = next(a["balance_minor"] for a in before["accounts"] if a["account_id"] == "acc-001")
preview = request("/api/transfers/preview", "POST", {
    "from_account_id": "acc-001", "beneficiary": beneficiary, "amount": "1.00", "currency": "EUR",
})
assert preview.get("preview_id") and preview.get("payload_digest"), "No persisted preview/digest returned"
accepted = request("/api/transfers", "POST", {
    "preview_id": preview["preview_id"], "payload_digest": preview["payload_digest"],
    "idempotency_key": str(uuid.uuid4()), "customer_confirmed": True, "sca_code": SCA_CODE,
})
print(f"Command: {accepted['command_id']} · status: {accepted['status']}")
terminal = accepted
for _ in range(45):
    if terminal.get("status") in {"SETTLED", "FAILED", "REQUIRES_RECONCILIATION"}:
        break
    time.sleep(1)
    terminal = request(f"/api/transfers/{accepted['command_id']}")
assert terminal.get("status") == "SETTLED", f"Transfer did not settle: {terminal}"
assert terminal.get("reconciliation_status") == "MATCHED", f"Reconciliation mismatch: {terminal}"
after = request("/api/overview")
after_balance = next(a["balance_minor"] for a in after["accounts"] if a["account_id"] == "acc-001")
assert before_balance - after_balance == 100, f"Unexpected balance delta: {before_balance} -> {after_balance}"
transactions = request("/api/transactions?limit=150")["transactions"]
assert any(t["description"] == f"Transfer to {beneficiary}" and t["amount_minor"] == -100 for t in transactions), "Booked debit missing"
audit = request("/api/audit/verify")
assert audit.get("valid"), f"Audit chain invalid: {audit}"
chat = request("/api/chat", "POST", {"message": "What are my recent transactions?", "history": []})
assert "get_recent_transactions" in chat.get("tools_used", []), "Assistant did not read via MCP"
assert beneficiary in chat.get("reply", ""), "MCP-backed assistant did not report the new transaction"
print("PASS: preview → approval → PostgreSQL → outbox → Temporal → synthetic ledger → reconciliation → MCP → ORAI.")
print(f"Booked demo transfer: {beneficiary} · EUR 1.00 · {terminal.get('ledger_transaction_id')}")
PY
