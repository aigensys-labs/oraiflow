.PHONY: up down logs ps smoke smoke-copilot smoke-transfer smoke-api-diagnostic format format-check lint docs-check quality

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

# Primary customer chat path: React → Copilot Runtime → AG-UI → LangGraph → MCP.
smoke: smoke-copilot

smoke-copilot:
	./scripts/smoke-copilot.sh

# Writes a synthetic EUR 1.00 transfer to the local demo ledger.
smoke-transfer:
	./scripts/smoke-transfer.sh

# Retained direct /api/chat diagnostic path; not used by the customer UI.
smoke-api-diagnostic:
	./scripts/smoke.sh


# Python formatting and correctness checks (development host only).
format:
	uv run black services shared tests scripts

format-check:
	uv run black --check services shared tests scripts

lint:
	uv run ruff check services shared tests scripts

docs-check:
	uv run python scripts/check_docstrings.py

quality: format-check lint docs-check
