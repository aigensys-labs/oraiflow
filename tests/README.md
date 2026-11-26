# ORAIflow test guide

Initialize the Python environment once from the repository root, then run deterministic tests:

```bash
uv lock
uv sync --locked --group dev
uv run pytest -q
make quality
```

The suite covers OCCF registry/schema conformance, semantic interpretation validation, planner compilation, task/workflow continuity, capability boundaries, transaction analysis, grounded result handling and backend policy. Frontend behavior tests use Node's built-in runner; run the registered frontend tests with the commands in CI or:

```bash
node --test apps/web/src/*.test.js
```

The frontend behavior suite is run with `npm test`; the production React/Vite bundle is validated separately with `npm run build` in CI. The CI workflow is the authoritative complete command list; a local partial run must not be described as the whole suite.

## Optional PostgreSQL tests

A PostgreSQL-specific JSONB test is skipped unless `TEST_DATABASE_URL` is set. Point it only at a disposable test database, never the application's `oraiflow` demo database. CI provisions PostgreSQL and runs the test with that service.

## Contract conformance

The registry is canonical at `shared/contracts/registry.json`. After a contract change:

```bash
uv run --locked python scripts/generate_contracts.py
uv run --locked python scripts/generate_contracts.py --check
uv run --locked pytest -q tests/test_capability_registry.py tests/test_intent_contracts.py tests/test_contract_integration_inventory.py
```

Update the registry first, regenerate frontend metadata, implement the operation at its owning boundary, and test valid, invalid, empty, partial, authorization and provenance cases. See [Capability Contracts](../docs/CAPABILITY-CONTRACTS.md).

## End-to-end smoke paths

| Script | Path | Data impact |
|---|---|---|
| `./scripts/smoke-copilot.sh` | Browser-facing CopilotKit → AG-UI → Agent → MCP | Read-only |
| `./scripts/smoke.sh` | Banking API plus retained direct `/api/chat` diagnostic | Read-only |
| `./scripts/smoke-transfer.sh` | Kernel preview/approval → outbox → Temporal → synthetic ledger → audit/read-back | Creates a synthetic EUR 1.00 debit |

The customer UI uses the CopilotKit/AG-UI path. The direct `/api/chat` diagnostic is not a substitute for browser/provider integration testing.

## Behavioral acceptance

In addition to deterministic tests, evaluate paraphrased and multi-turn requests, unsupported requests, ambiguous inputs, empty/partial outcomes and failure paths through the configured customer-facing route. Keep held-out paraphrases out of model prompts and registry examples so that they measure generalization rather than memorization.

## Environment limitations

A passing unit/helper suite does not prove a live provider, browser, AG-UI, MCP, PostgreSQL or Temporal journey. When sharing test evidence, record the active `LLM_PROVIDER` and model, dependencies actually installed, database used, and which integration paths were not exercised. The default `LLM_PROVIDER=demo` is not semantically equivalent to a configured model-backed interpreter.


## Python code quality

Black is the canonical Python formatter. Ruff runs correctness-focused lint rules (including syntax-adjacent parser diagnostics and undefined-name checks); `compileall` is the explicit Python syntax-compilation check. CI checks Black formatting on Python files changed by a push or pull request, and runs Ruff, `compileall`, docstring coverage and tests across the repository. The existing v0.7.x tree still needs a one-time full formatting pass: maintainers should run `make format` and then `make quality` before declaring the complete legacy tree Black-clean. These tools are for maintainers; users running the Docker demo do not need a host Python installation.
