# ORAIflow Capability Contract Framework (OCCF)

## Purpose and boundary

The ORAIflow Capability Contract Framework (OCCF) is the shared, versioned contract layer between conversational interpretation, application actions, banking connectors, presentation components and the Banking Kernel.

Its purpose is to ensure that every supported operation declares what it accepts, what it may do, what it returns, what evidence accompanies that result, and how that result may be presented. The LLM must operate within these contracts; it must not invent alternate inputs, skip preconditions, reinterpret errors as success, or bypass a capability's declared authority.

OCCF is implemented inside the existing modular monolith. It does not introduce a microservice and does not grant authority. The MCP server independently derives identity and enforces its capability checks. Frontend handlers independently enforce application allowlists and postconditions. The Banking Kernel independently enforces financial identity, ownership, policy, customer confirmation and command rules.

## Canonical source of truth

`shared/contracts/registry.json` is the language-neutral source of truth. It contains four distinct registries and shared schema definitions:

| Registry | Question it answers | Current entries |
|---|---|---:|
| `intents` | What does the customer appear to want, and what execution/presentation plan applies? | 17 |
| `capabilities` | What bounded operation may the agent/application invoke? | 11 (7 MCP, 4 frontend) |
| `presentations` | Which approved UI renderer can display verified results, and what evidence does it require? | 3 |
| `kernel_operations` | Which typed financial operations exist at the Kernel boundary? | 3, all explicitly non-agent-callable |

`$defs` contains reusable schemas such as `AccountRecord`, `TransactionRecord`, `ContactRecord`, `Evidence`, `FrontendActionResult`, `ResultError` and `ApplicationAccountContext` (including a read-only, minimized Contacts projection). The registry also defines a versioned, read-only `application_context` contract for the current workspace, selected account and visible account catalogue, including declarative cross-field semantic rules, plus the canonical workspace alias/allowlist policy. Its authority is explicitly `frontend_read_only`; it can never authorize a banking operation. Python and TypeScript execute the same registry-declared semantic rules. Frontend preconditions use a small allowlisted set of generic evaluators (workspace allowlist, required context field, and membership in a context collection), whose argument/context references are checked during registry validation. Schemas use local JSON Schema references only. Runtime validation never downloads schemas or follows external references.

Python services validate against this registry. The LangGraph model-facing MCP `StructuredTool` names, descriptions and Pydantic argument models are built from the registered declarations. `scripts/generate_contracts.py` generates `apps/web/src/generated/contracts.js` (including a SHA-256 digest of the registry), and `apps/web/src/contracts.js` converts the same JSON Schema declarations into frontend Zod validators. Hand-maintained duplicate frontend or model-facing schemas are not the source of truth.

Check generated output after editing the registry:

```bash
uv run --locked python scripts/generate_contracts.py
uv run --locked python scripts/generate_contracts.py --check
```

CI runs the check so an out-of-date generated frontend contract fails the build.

## The universal execution lifecycle

Every capability follows the declared lifecycle in `execution_lifecycle`:

1. **Resolve** — identify the registered intent/capability; unknown names are not executable.
2. **Validate input** — validate the complete argument object against the capability's versioned input schema. Unexpected fields are rejected where the schema disallows them.
3. **Establish context** — obtain customer, tenant, account and application context from trusted server/application state. Model-provided identity is never authoritative.
4. **Check preconditions** — verify declared scope and operation-specific prerequisites. Service-side authorization remains mandatory.
5. **Execute** — invoke the declared MCP, frontend or Kernel implementation with the declared timeout, effect and retry semantics.
6. **Validate output** — validate the returned data against the capability output schema before it is consumed by the agent or UI.
7. **Attach evidence** — preserve source, scope, retrieval time, completeness and trace identifiers where the operation returns data.
8. **Resolve presentation** — apply the registered presentation policy. Components receive validated, current-turn data; model-authored financial props are replaced, not trusted.
9. **Complete** — return a defined outcome and terminate/continue the agent lifecycle according to the operation's declared semantics.

The shared Python registry module validates individual schemas and cross-registry references at load time. Conformance tests compare every declared MCP tool, frontend action, GenUI renderer and Kernel route with the actual source implementation.

## Capability declarations

Each MCP/frontend capability declares:

- Stable registry ID, exposed tool name and contract version.
- Kind (`mcp` or `frontend`) and strict input/output JSON Schemas.
- Required capability, principal source and explicit `agent_invocable` flag.
- Effect classification, idempotency, timeout and retry policy.
- Explicit precondition declarations and the boundary responsible for enforcing each one.
- Evidence source/scope and customer-safe failure message.

Retry metadata is part of the contract, not an application-wide default. Bounded retries are only valid for idempotent MCP reads. Financial operations are not retried by a generic connector loop. A command retry requires the caller-controlled idempotency model declared by the Kernel operation and must retain Kernel-side duplicate protection.

### Current MCP capabilities

| Registry ID | Tool | Effect | Scope/evidence |
|---|---|---|---|
| `banking.accounts.list` | `list_customer_accounts` | Read-only | Server-authenticated customer's accounts |
| `banking.accounts.balance` | `get_account_balance` | Read-only | One authorized account |
| `banking.transactions.recent` | `get_recent_transactions` | Read-only | Server-authenticated customer's transactions |
| `banking.transactions.analyze` | `analyze_customer_transactions` | Read-only | Authenticated customer/tenant transactions; bounded filters, grouping and deterministic aggregation |
| `banking.contacts.search` | `search_contacts` | Read-only | Active saved contacts owned by the server-authenticated customer; model output excludes email and phone |
| `banking.loans.overview` | `get_customer_loan_overview` | Read-only | Synthetic illustrative loan data; disabled unless `DEMO_LOAN_ENABLED=true` |
| `banking.financial_health.overview` | `get_customer_financial_health_overview` | Read-only | Recorded leverage indicator derived from available account/loan data; not a bureau score |

MCP validates arguments and database results against the registry and returns the common result envelope. Its required capability is resolved from the registered capability declaration before server-side enforcement; account ownership is still checked in the owner/tenant-scoped query. The Agent Runtime validates that envelope again before using the result. The double boundary is deliberate: the connector validates what it emits; the consumer validates what it accepts.

### Current frontend capabilities

| Registry ID | Browser action | Declared effect |
|---|---|---|
| `frontend.workspace.navigate` | `navigate_workspace` | `ui_state_only` |
| `frontend.account.select` | `select_account` | `ui_state_only` |
| `frontend.account.clear_selection` | `clear_account_selection` | `ui_state_only` |
| `frontend.transfer.prepare_draft` | `prepare_transfer_draft` | `draft_only` |

The shared `executeFrontendCapability` validates arguments, validates the supplied `application_context`, executes the generic precondition evaluators declared in the registry, invokes the actual browser handler, validates its postcondition/result and wraps the outcome. The canonical workspace allowlist and aliases are also registry-driven. The `application_context` sent to CopilotKit is validated on both sides against the shared schema, including active contact IDs/names/status, and is never an authorization source. The agent can use it for current UI consistency only; beneficiary resolution must still come from a successful current-turn `search_contacts` MCP result. A transfer draft remains editable presentation state. It is not a preview, approval, SCA event, payment command or financial commitment.

## Standard capability result envelope

MCP and frontend capabilities return the same versioned envelope:

```json
{
  "schema_version": "1.0.0",
  "capability": "banking.transactions.recent",
  "outcome": "SUCCESS",
  "data": [],
  "evidence": {
    "source": "mcp",
    "scope": "authenticated_customer_transactions",
    "retrieved_at": "2026-11-21T00:00:00+00:00",
    "completeness": "COMPLETE",
    "trace_id": "trace-identifier"
  },
  "error": null,
  "trace_id": "trace-identifier",
  "trust_class": "untrusted_banking_data"
}
```

The example is illustrative. The schema requires `data` to conform to the selected capability's output schema. The outcome/evidence/error relationships are also enforced semantically; a JSON object that merely has the right top-level fields is insufficient.

| Outcome | Meaning and invariant |
|---|---|
| `SUCCESS` | Valid non-empty data with complete evidence. |
| `EMPTY` | The request succeeded and returned an empty collection/object; evidence completeness is `EMPTY`. It is not a connector failure. |
| `PARTIAL` | Valid non-empty partial data; evidence completeness is `PARTIAL`. The caller must preserve that limitation when explaining the result. |
| `INVALID_INPUT` | Arguments did not satisfy the declared input contract. No result data/evidence. |
| `REJECTED` | The capability/precondition/authorization rejected the operation. No result data/evidence. |
| `UNAVAILABLE` | The connector or dependency could not complete the request. No result data/evidence. |
| `INVALID_RESPONSE` | The connector returned data/envelope that failed contract validation. No result data/evidence. |

Failure outcomes require a normalized error and must have `data: null`, `evidence: null` and `trust_class: connector_error`. Successful data is labelled `untrusted_banking_data`: it is data, never an instruction to the agent. The source/scope/trace checks provide contract consistency and traceability; they are **not cryptographic attestation** and do not independently prove the upstream provider's truthfulness.

`Evidence` includes the capability-declared source and scope, retrieval time, completeness and trace ID. The Agent Runtime adds the provider trace ID when wrapping the MCP result in its own execution trace. This preserves correlation across the connector and agent boundaries. The current application context is validated against the same registry schema in both React and Agent Runtime, including cross-field consistency between selected account ID/name and the supplied account catalogue. It remains display/navigation context only; account ownership and financial authorization are rechecked by MCP or the Banking Kernel.

## Presentation contracts

A presentation declares a props schema, its source capabilities, data property, deterministic transform, optional preflight dependencies, follow-up behavior and safe failure message.

Current renderers:

- `render_account_summary` — account data from `banking.accounts.list` or `banking.accounts.balance`; canonical for account overview, balance and total-balance requests.
- `render_transaction_list` — transactions from `banking.transactions.recent`; canonical for plural/recent transaction requests.
- `render_transaction_detail` — exactly one record from `banking.transactions.recent` with `limit: 1`; canonical for singular latest/last transaction detail requests. Explanatory questions such as “why was it declined?” remain agent-assisted and are not forced into this renderer.

The Agent Runtime binds only validated results from the current user turn. It overwrites model-supplied data and titles with registry-controlled values and validated evidence. If a required read fails, the component does not render. Display-only components declare `follow_up: false`; browser actions retain follow-up where the model needs their execution result.

The intent registry defines deterministic canonical mappings such as `RECENT_TRANSACTIONS` → `banking.transactions.recent` → `render_transaction_list`, `LATEST_TRANSACTION_DETAIL` → the same capability with `limit: 1` → `render_transaction_detail`, and account overview/total balance → `render_account_summary`. It defines `TRANSFER_PREPARATION` with a required `banking.contacts.search` resolution step and a separate `SAVED_CONTACT_LOOKUP` intent that uses the same read-only capability for standalone contact discovery. The planner preserves this intent distinction: finding a contact does not imply a transfer, while transfer preparation still requires its amount, verified beneficiary, currency and source-account checks. This is enforced by application code, not solely by a model prompt. Agent-assisted intents cover transaction explanation and analysis, contact lookup, transfer preparation, navigation, account selection and clearing the account filter; each points only to an agent-invocable capability and declares its response mode. They do not inherit a display-only component policy.

## Banking Kernel operation contracts

The registry separately declares typed request/response schemas, route, required capability, authority, effect, idempotency and retry semantics for:

- `kernel.transfer.preview`
- `kernel.transfer.confirm`
- `kernel.transfer.status`

All three explicitly set `agent_callable: false` and `authority: banking_kernel`. Their required capability is resolved from the registered Kernel operation before service-side authorization. They are not exposed to the LLM as tools and do not use the agent capability result envelope. Their HTTP request/response schemas are validated at the Kernel API boundary, while existing identity, ownership, policy, approval, audit, outbox and Temporal controls remain independently enforced. Registry metadata cannot grant permission or replace Kernel authorization.

## Adding a capability

A new capability is not complete when its tool function exists. Follow this sequence:

1. **Declare it** in `shared/contracts/registry.json`: ID/version, kind/name, strict input/output schemas, authorization principal/capability, effect/idempotency/timeout/retry policy, evidence source/scope and safe failure message.
2. **Reuse `$defs`** for common records and envelopes instead of copying schema fragments.
3. **Implement it at the correct boundary**. MCP handlers validate inputs, enforce server-derived identity/capability, validate provider data and emit a result envelope. Frontend handlers use `executeFrontendCapability` and enforce real application allowlists/postconditions. Kernel operations remain non-agent-callable and enforce their own authority.
4. **Register the presentation if needed**: props schema, source capabilities, evidence requirement, data binding, transform, preflight and fallback policy. Do not allow model-provided financial values to become authoritative.
5. **Register the intent mapping if needed**: recognition mode, deterministic patterns/exclusions or agent-assisted semantics, and the permitted execution/presentation plan. Confidence scores are a separate interpretation assessment; they do not override validation or authorization.
6. **Regenerate TypeScript contracts** with `uv run --locked python scripts/generate_contracts.py`.
7. **Add conformance tests** for valid and invalid input/output, all outcomes, evidence provenance, preconditions, failure behavior, agent-callability, source registration and presentation binding.
8. **Run the complete suite** and inspect the diff. The registry consistency validator and source inventory tests must pass before merge.

## Testing and diagnostics

From the repository root:

```bash
uv run --locked python scripts/generate_contracts.py --check
uv run --locked python -m compileall -q services shared tests scripts
uv run --locked pytest -q tests/test_capability_registry.py tests/test_intent_contracts.py tests/test_contract_integration_inventory.py
uv run --locked pytest -q
```

The focused contract tests validate registry structure, schema references, envelope invariants and source inventory. The broader suite covers Agent Runtime integration, database policy, the Kernel and workflow behavior. The PostgreSQL integration test needs a disposable `TEST_DATABASE_URL`.

For a running stack, inspect the correlated `trace_id` across Agent Runtime and MCP:

```bash
docker compose logs --since=5m --follow copilot-runtime agent-runtime mcp api
```

Do not log raw account/transaction payloads or connector arguments merely to diagnose a contract violation. Log capability ID, trace ID, stage, normalized outcome and duration.

## Security and limits

OCCF is a contract and validation framework—not a complete security system. JSON Schema checks shape and declared constraints; it does not authenticate a customer, authorize an account, establish sanctions status, perform SCA, prove a bank's data is correct, or make an external payment atomic. MCP and the Banking Kernel remain authoritative at their own boundaries.

The current implementation is a local synthetic demo: identity and service tokens are development-only, SCA is simulated, ledger and contact records are fictional, masked contact account references are illustrative only, and the evidence envelope is not signed by an external institution. The Kernel verifies that a contact ID belongs to the current customer and remains active, but this is not live beneficiary account verification or Verification of Payee. Semantic-input coverage remains partial; confidence calibration and provider-backed natural-language evaluation have not yet been completed. A confidence score never substitutes for deterministic validation or policy.


## Frontend action receipt semantics

Each agent-invocable frontend capability declares `result_semantics.success_status` in OCCF. The runtime validates the complete versioned result envelope and only counts a frontend action as successful when both the envelope outcome is `SUCCESS` and the returned `data.status` matches that registered value. Rejected, malformed, or mismatched results are recorded as failed frontend actions in TurnOutcome v1.1. Do not add frontend status lookup tables to Agent code or tests using status-only fake payloads.
