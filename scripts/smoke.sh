#!/usr/bin/env bash
set -euo pipefail
base="${ORAI_BASE_URL:-http://localhost:8000}"
curl -fsS "$base/healthz"
echo
curl -fsS "$base/api/config"
echo
curl -fsS "$base/api/overview" | python -m json.tool
curl -fsS "$base/api/chat" -H 'Content-Type: application/json' \
  -d '{"message":"Show my account balances","history":[]}' | python -m json.tool
printf '\nSmoke checks completed. Verify tools_used contains list_customer_accounts.\n'
