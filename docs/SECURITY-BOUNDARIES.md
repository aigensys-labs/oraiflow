# Security boundaries and production gaps

ORAIflow Core is a local/self-hosted reference implementation using fictional customer and ledger data. Its central security objective is to prevent probabilistic model output from becoming financial authority.

## What the current local slice enforces

### Identity and data access

- The API and MCP derive a fixed demo subject and tenant from server configuration. They do not trust customer or tenant IDs supplied by the browser or model as identity claims.
- MCP is the required agent-to-banking read boundary. The MCP process connects with a dedicated PostgreSQL role that has `SELECT` privileges only on `accounts`, `transactions`, `contacts`, `demo_loans` and `demo_loan_installments`; it does not receive the Banking Core `DATABASE_URL`.
- Agent Runtime receives the MCP endpoint/token and a separate checkpoint database URL. The checkpoint role is restricted to the `oraiflow_agent` schema; it is not the Banking Core application role.
- Agent Runtime has seven registered, agent-invocable read-only MCP capabilities: account list, account balance, recent transactions, transaction analysis, saved-contact search, illustrative loan overview and recorded Financial Health overview. The interpretation model is not bound to executable MCP tools during the assessment phase; it proposes registered intent meaning, and the runtime selects and validates the operation. The model-facing contact result is minimized to contact ID, name, bank, masked account reference, currency and status; email and phone are not returned. Neither the interpreter nor the Agent's MCP read path has preview, approval, payment-submit or database-write authority.
- Profile changes update presentation/contact fields under the fixed server-derived identity. They cannot change the customer subject or tenant.

### Financial authority and execution

- A model-generated transfer proposal is an untrusted draft used to prefill an editable form. For chat transfers, the Agent Runtime must use a successful current-turn Contacts MCP search and exactly one resolved contact ID; duplicate matches require the customer to choose. The agent cannot use arbitrary contact context or choose the first source account when several currency-compatible accounts exist. It is not a command.
- The Banking Kernel/API rechecks that an optional beneficiary contact ID belongs to the authenticated customer/tenant, remains active, matches the canonical beneficiary name and supports the requested currency, then creates a persisted, expiring preview after deterministic account, amount, currency, funds and policy checks. The contact ID is included in the signed preview digest and rechecked at confirmation. This only verifies the local synthetic saved-contact record; it is not live external beneficiary verification.
- The preview is bound to an HMAC-protected digest of the exact transfer payload and policy context. Confirmation must reference the stored preview and displayed digest; the Kernel validates expiry, ownership, single-use state and current policy under database locking.
- The visible local challenge binds the demo approval to the preview. It is not real SCA.
- Approval, command, audit evidence and outbox event are committed atomically in PostgreSQL. The worker dispatches committed work to Temporal; Temporal activities invoke the ledger port.
- The current ledger adapter is `SyntheticPostgresLedgerAdapter`. It applies a synthetic debit, books a matching transaction and records reconciliation evidence in a PostgreSQL transaction.
- A deterministic local rejection is `FAILED`. If workflow retries are exhausted without a reliable outcome, the command becomes `REQUIRES_RECONCILIATION`. Do not submit a duplicate while the original outcome is uncertain.
- Temporal and database retries do not imply exactly-once semantics for an external payment rail. A future institution adapter must provide downstream idempotency and reconciliation.

### Agent instruction integrity and product scope

- The Agent Runtime blocks high-confidence direct attempts in a customer-authored turn to rewrite system/developer instructions, change ORAIflow's identity, disclose hidden prompts or bypass security policy. This is a targeted guard, not a complete prompt-injection detector; untrusted application context, retrieved records and tool output must still be treated as data rather than instructions.
- Ordinary assistant prose is returned through a runtime-only response envelope. The runtime accepts registered-intent clarifications, bounded pleasantries and capability-help requests, and replaces out-of-scope or instruction-override classifications with fixed customer-safe messages. The envelope has no side effects and is not an OCCF capability. If the provider ignores the response protocol, arbitrary free text is not forwarded as an unrestricted general-purpose answer.
- Customers may change the banking task, abandon a pending workflow or start a new supported task. They cannot use chat text to change model/system policy, expand the capability set, authorize a payment or bypass OCCF/Banking Kernel controls.
- A simple affirmative can confirm only the immediately preceding, validated, allowlisted workspace-navigation proposal with a single-use action ID. It does not confirm a transfer, authorize a payment or approve any other action. The production browser acknowledgement round-trip still requires deployment-level AG-UI testing.
- These controls narrow the conversational surface; they do not replace prompt-injection defense in depth, provider governance, tool-level authorization, data minimization, OCCF validation or independent Banking Kernel policy.

### Agent and transport boundaries

- The browser's primary assistant path is React/CopilotKit → Copilot Runtime → AG-UI → Agent Runtime → MCP.
- CopilotKit frontend actions are browser-side, allowlisted presentation operations (for example, navigation, account selection/filtering and opening a transfer draft). They do not authorize a banking command.
- The direct Banking Core `POST /api/chat` → Agent Runtime `/api/chat` path is retained only for developer diagnostics. The customer UI uses CopilotKit/AG-UI.
- MCP and Copilot Runtime have no published host ports. Agent Runtime diagnostics, API, web and Temporal ports bind to loopback by default. `API_BIND_ADDRESS` can deliberately widen only the API host binding; do not expose this demo without additional network and application controls.
- Copilot Runtime scopes local threads to the fixed single-user demo identity. It is not a production multi-user identity/session layer.
- `MCP_ACCESS_TOKEN` and `AGENT_RUNTIME_INTERNAL_TOKEN` are static local service tokens. They are not OAuth 2.1, OIDC or customer authentication.

### Data handling, audit and operations

- User text/history and MCP results sent to a model are subject to minimization and pattern-based redaction for high-confidence IBAN, payment-card-number and credential-assignment patterns. This is a baseline, not complete DLP, prompt-injection prevention or provider risk assessment.
- OpenAI and Anthropic use is blocked unless `LLM_EXTERNAL_DATA_APPROVED=true`. This flag records an operator acknowledgement; it does not replace provider contractual review. The guard does not inspect `OLLAMA_BASE_URL`: a remote Ollama endpoint or private model gateway must also be treated as an external data destination and reviewed by the operator.
- Model credentials are passed only to Agent Runtime. Banking Core API, MCP and worker do not receive those provider keys.
- Audit events are append-only at the database trigger level and hash-linked. `/api/audit/verify` verifies the local chain. A privileged database administrator can alter the database/schema; this is not independent WORM storage, external anchoring or legal evidence.
- Optional public CA certificates under `./certs/` are mounted read-only in backend containers to establish trust for outbound HTTPS. The project does not mount a `./secrets` directory and does not implement bank client-certificate/mTLS authentication.
- `.env` and local certificate files are excluded from Git. Docker build contexts exclude `.env` files (including local `.env.*` variants), `certs/` contents and `secrets/` contents so local configuration and trust material are not copied into images.

## Controls that are simulated or absent

| Area | Local demo behavior | Required before any real bank/customer deployment |
|---|---|---|
| Customer identity | Fixed server-configured `DEMO_USER_ID` and `DEMO_TENANT_ID` | OIDC login; JWT signature, issuer, audience and expiry validation; authoritative customer/tenant mapping |
| Multi-tenancy | One seeded demo customer | Tenant-aware authorization, isolation tests, row/domain-level access controls |
| MCP authorization | Internal static bearer token plus SELECT-only role on accounts, transactions, contacts and illustrative loan tables | OAuth 2.1 resource-server validation, audience/scopes, per-tool authorization and token lifecycle |
| Agent sessions | Copilot Runtime scoped to one demo user; process-local runner with PostgreSQL graph checkpoints | Authenticated, tenant-scoped sessions, thread access controls and retention policy |
| SCA | Visible `123456` demo challenge bound to stored preview digest | Bank-approved challenge/verification service and dynamic linking to payee, amount, currency, account and expiry |
| Payment execution | Local synthetic PostgreSQL debit; contact ID ownership/status is rechecked locally | Institution-approved payment/core adapter, downstream idempotency, live Verification of Payee and outcome reconciliation |
| Ledger source of truth | Synthetic PostgreSQL adapter | Institution ledger/core remains authoritative; robust query and command adapter contracts |
| Audit evidence | DB trigger + hash-linked rows + verification endpoint | Independently protected signed/WORM archive, external anchoring, retention and privileged-admin controls |
| Workflow platform | Local Temporal dev server; history under container `/tmp` | Production Temporal deployment, TLS/auth, durable persistence, HA, backups and operational runbooks |
| Data protection | Basic minimization/redaction and explicit cloud-provider opt-in | Data classification, complete DLP strategy, key management, encryption, provider due diligence, residency and access monitoring |
| Secrets | Local `.env`, static service tokens and optional public CA trust | Secret manager/workload identity, key rotation, revocation, audit and least privilege |
| Observability | Container logs and health probes | Distributed traces, metrics, alerting, log redaction, SLOs and incident procedures |

## Never do this with the demo

- Do not connect real customer data, production bank APIs, real payment rails or real payment credentials.
- Do not treat the demo identity, static bearer tokens or visible challenge as production authentication or SCA.
- Do not expose PostgreSQL, MCP, Copilot Runtime or Temporal gRPC to untrusted networks.
- Do not set `API_BIND_ADDRESS=0.0.0.0` unless the service is deliberately protected by appropriate network controls and a production-grade authentication layer.
- Do not commit `.env`, API tokens, passwords, private keys or private certificates.
- Do not put private keys in `./certs/`; that directory is for public CA certificates only.
- Do not use the default preview signing secret outside a disposable local environment.
- Do not interpret the local audit chain as immutable WORM storage or independently verified legal evidence.
- Do not interpret the presence of a Temporal workflow or audit row as proof of regulatory compliance.
- Do not use `docker compose down -v` as an ordinary restart/upgrade command; it deletes the PostgreSQL demo volume.

## Gate before a real institution adapter

A bank integration should be a separately designed milestone. At minimum:

1. Integrate authoritative identity and tenant resolution.
2. Replace static service tokens with OAuth 2.1 resource-server validation and scoped capability authorization.
3. Bind customer approval to normalized beneficiary, source account, amount, currency and expiry with the institution's approved SCA provider.
4. Store and verify immutable preview/approval evidence server-side; make command acceptance single-use and idempotent.
5. Define institution-facing query/command port contracts, client authentication, TLS/mTLS and credential rotation.
6. Implement downstream idempotency, payment status handling and reconciliation for accepted, rejected, returned and unknown outcomes.
7. Protect audit evidence independently and add threat modeling, security testing, penetration testing and disaster-recovery exercises.
8. Deploy a production-grade Temporal service and operationalize retries, replay, alerting and recovery.
