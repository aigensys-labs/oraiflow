#!/usr/bin/env bash
set -euo pipefail

web_base="${ORAI_WEB_BASE_URL:-http://localhost:3000}"

python3 - "$web_base" <<'PY'
import json
import sys
import urllib.error
import urllib.request
import uuid

base = sys.argv[1].rstrip("/")

def request(path, method="GET", body=None, accept="application/json"):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": accept}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path}: HTTP {exc.code}: {exc.read().decode(errors='replace')}") from exc

status, info_text = request("/api/copilotkit/info")
assert status == 200, f"Copilot Runtime info returned HTTP {status}"
info = json.loads(info_text)
assert "oraiflow_agent" in json.dumps(info), f"Runtime did not advertise oraiflow_agent: {info_text}"
print("PASS: browser-facing Copilot Runtime advertises oraiflow_agent.")

run_input = {
    "threadId": str(uuid.uuid4()),
    "runId": str(uuid.uuid4()),
    "messages": [{
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": "Show my account balances",
    }],
    "state": {},
    "tools": [],
    "context": [],
    "forwardedProps": {},
}
status, stream = request(
    "/api/copilotkit/agent/oraiflow_agent/run",
    "POST",
    run_input,
    "text/event-stream",
)
assert status == 200, f"Copilot agent run returned HTTP {status}"
assert "TEXT_MESSAGE" in stream, f"No AG-UI text-message events received: {stream[:1500]}"
assert "RUN_FINISHED" in stream, f"AG-UI stream did not emit its terminal RUN_FINISHED event: {stream[-2000:]}"
assert "account" in stream.lower() or "balance" in stream.lower(), (
    f"AG-UI stream did not contain an account/balance response: {stream[:2500]}"
)
print("PASS: Copilot Runtime → AG-UI → LangGraph agent → MCP → streamed assistant response.")

coffee_input = {
    "threadId": str(uuid.uuid4()),
    "runId": str(uuid.uuid4()),
    "messages": [{
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": "How much did I spend on coffee?",
    }],
    "state": {},
    "tools": [],
    "context": [],
    "forwardedProps": {},
}
status, coffee_stream = request(
    "/api/copilotkit/agent/oraiflow_agent/run",
    "POST",
    coffee_input,
    "text/event-stream",
)
assert status == 200, f"Coffee analysis run returned HTTP {status}"
assert "TEXT_MESSAGE_CONTENT" in coffee_stream, (
    "Coffee analysis did not emit customer-visible text content: " + coffee_stream[-2500:]
)
assert "RUN_FINISHED" in coffee_stream, "Coffee analysis did not emit RUN_FINISHED"

text_deltas = []
stream_errors = []
for line in coffee_stream.splitlines():
    if not line.startswith("data:"):
        continue
    try:
        event = json.loads(line.partition(":")[2].strip())
    except json.JSONDecodeError:
        continue
    event_type = str(event.get("type", "")).upper()
    if "TEXT_MESSAGE_CONTENT" in event_type:
        delta = event.get("delta")
        if isinstance(delta, str) and delta.strip():
            text_deltas.append(delta)
    if "RUN_ERROR" in event_type:
        stream_errors.append(event.get("code", "RUN_ERROR"))

assert text_deltas, "Coffee analysis emitted an empty text lifecycle"
assert not stream_errors, f"Coffee analysis emitted AG-UI errors: {stream_errors}"
print("PASS: coffee-spending request returns non-empty customer-visible AG-UI text and RUN_FINISHED.")
print("This smoke test verifies the backend request path. Also test frontend actions in the browser:")
print("  1. Ask ORAI: 'Open Activity'. The Activity workspace should open.")
print("  2. Ask ORAI: 'Open my Everyday account'. Activity should open with that account selected.")
print("  3. Ask ORAI: 'Open Accounts'. The Accounts workspace should open.")
print("  4. Ask ORAI: 'Show my account balances'. A structured account card should render inside the chat.")
print("  5. Ask ORAI: 'What are my recent transactions?'. A structured transaction list should render inside the chat.")
print("  6. Ask ORAI: 'Clear the account filter'. Activity should return to All accounts.")
print("  7. Return to Overview; Recent Activity must still show all accounts.")
PY
