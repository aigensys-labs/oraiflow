# ORAIflow — zero-to-hero local guide

This is the current hands-on local walkthrough for the self-hosted demo. It uses **Bash** syntax (Linux, macOS, WSL or Git Bash) and Docker Compose v2. For the authoritative interpretation, capability ownership and service boundaries, use [Interpretation and Contract Flow](INTERPRETATION-AND-CONTRACT-FLOW.md), [Capability Contracts](CAPABILITY-CONTRACTS.md) and [Architecture](ARCHITECTURE.md). The default `LLM_PROVIDER=demo` is a deterministic demonstration path, not a semantic-quality test; connect a configured model for interpretation acceptance.

> This is a self-hosted synthetic fintech demo. It does not connect to a bank, use real customer data, authenticate real customers or move real money.

## 0. Understand what you are about to run

| Service | Why it is present | What it adds to the demo |
|---|---|---|
| `web` | Customer-facing React application served by Nginx | The Overview, Accounts, Activity, Payments, Profile and assistant experiences; it also reverse-proxies browser requests to the backend services |
| `copilot-runtime` | TypeScript CopilotKit Runtime | Connects the custom ORAI chat UI to the agent using the AG-UI protocol and coordinates browser-side actions |
| `agent-runtime` | Separate Python LangGraph service | Runs the model/tool loop and streams the assistant response; it has no financial write capability |
| `mcp` | Narrow agent-to-banking capability server | Exposes account and transaction reads over MCP using its own read-only PostgreSQL role |
| `api` | Banking Core API / Kernel boundary | Serves customer workspace data and profile, creates transfer previews, verifies confirmation and accepts the financial command |
| `worker` | Outbox dispatcher and Temporal worker | Starts workflows from persisted outbox records and runs activities independently of an open browser session |
| `temporal` | Local Temporal development server | Persists workflow history during the lifetime of this local container and coordinates retries |
| `postgres` | Operational database and synthetic ledger | Stores demo balances, transactions, customer profile, previews, approvals, commands, audit/outbox state and graph checkpoints |
| `migrate` | One-shot database bootstrap | Applies additive schema updates and creates restricted database roles before dependent services start |

The design rule is: **the LLM interprets; MCP exposes capabilities; the Banking Kernel authorizes; Temporal orchestrates; the ledger determines financial truth.** Each component exists to preserve that separation.

## 1. Prerequisites

Install Docker Engine/Desktop with Compose v2 and Bash. Allow about 8 GB RAM for the base stack. A locally hosted model may need substantially more memory/VRAM depending on its size.

```bash
docker --version
docker compose version
```

**Why:** Compose supplies the isolated Python, Node, PostgreSQL and Temporal processes, so you do not need to install each runtime on your host. The initial `LLM_PROVIDER=demo` does not require a model or cloud API key.

## 2. Get the project and create configuration

Download and extract the published source archive, then open a Bash terminal in the extracted project root. For a Git checkout, use the clone URL published by the repository maintainer. Then copy the example configuration:

```bash
cp .env.example .env
```

For the first run, keep `LLM_PROVIDER=demo`. Change `POSTGRES_PASSWORD` to a local-only password and set the same credential in `DATABASE_URL`. Also change the local service tokens and `PREVIEW_SIGNING_SECRET` from their published placeholders before sharing the environment with anyone else.

Useful presentation options:

```dotenv
DEMO_DISPLAY_NAME=Alex Morgan
DEMO_WORKSPACE_NAME="Atlas Fintech"
DEMO_SALARY_EMPLOYER=Northstar Labs
```

- `DEMO_DISPLAY_NAME` is the seed name for a newly bootstrapped profile; a profile already edited in the app is kept in PostgreSQL.
- `DEMO_WORKSPACE_NAME` controls only the label in the sidebar. It does not change customer or tenant identity.
- `DEMO_SALARY_EMPLOYER` changes the label on the existing synthetic salary transaction `txn-001`. `DEMO_SALARY_AMOUNT_MINOR` sets its synthetic monthly EUR credit in minor units; the migration adjusts the same account balance by the matching amount delta, idempotently.

All host-published web/API/Temporal ports bind to loopback by default. `API_BIND_ADDRESS` can deliberately change the API host binding; do not expose the demo on a shared or public network without putting appropriate authentication, TLS and network controls in front of it.

**Why:** `.env` keeps environment-specific values out of source code. The fixed demo identity is configuration for one local customer, not a login system or trusted identity assertion.

## 3. Build and start the stack

Run from the repository root:

```bash
docker compose up -d --build
docker compose ps -a
```

`migrate` is expected to finish with exit code `0`; the other services should report healthy or running. To inspect startup:

```bash
docker compose logs --tail=100 postgres migrate mcp agent-runtime copilot-runtime api worker web temporal
```

**Why the startup order matters:** PostgreSQL must be healthy before the migration job can provision the base schema and restricted database roles. MCP/API/worker wait for migration. Agent Runtime waits for MCP. Copilot Runtime waits for Agent Runtime. The web service waits for API and Copilot Runtime. Temporal is required by the worker, but not by API command acceptance: a committed command remains in the PostgreSQL outbox until the dispatcher can start its workflow.

## 4. Open the local services

| Service | Local URL | What it is useful for |
|---|---|---|
| ORAIflow workspace | http://localhost:3000 | Main customer application and CopilotKit-powered ORAI assistant |
| Banking Core API docs | http://localhost:8000/docs | Inspect profile, read, preview, command and audit contracts |
| Banking Core health | http://localhost:8000/healthz | Check the API process |
| Temporal Web UI | http://localhost:8233 | Inspect transfer workflow history and activity execution |
| Agent Runtime docs | http://localhost:8101/docs | Loopback-only diagnostics for the Python LangGraph/AG-UI service |

MCP and Copilot Runtime do not publish host ports. They are reached through the internal Docker network and Nginx proxy. PostgreSQL and Temporal gRPC are also not intended for access from other machines; the local Compose port bindings are restricted to loopback.

## 5. Test the main assistant path

Open http://localhost:3000 and try these prompts in the ORAI assistant:

1. `Show my account balances`
2. `What are my recent transactions?`
3. `Open Activity`
4. `Open my Everyday account`
5. `Clear the account filter`
6. `Open Accounts`

Expected behavior: account and transaction information is retrieved by LangGraph through MCP; the assistant can render the structured account/transaction presentation; approved frontend actions navigate the workspace and select/clear an account filter. Returning to Overview should restore the unfiltered Recent Activity panel.

The normal browser route is:

```text
React / ORAI chat
  → CopilotKit React integration
  → Nginx /api/copilotkit proxy
  → CopilotKit Runtime (TypeScript)
  → AG-UI
  → Agent Runtime (LangGraph)
  → MCP client and MCP server
  → PostgreSQL (read-only MCP role)
```

**Why this matters:** CopilotKit provides the browser-agent transport, streaming and frontend tool lifecycle. LangGraph decides when to call a tool. MCP is the required banking-read boundary. The agent never receives Banking Core SQL credentials or a financial write tool. A browser action such as opening Activity is not a financial authorization.

You can check the whole backend CopilotKit/AG-UI request path without using a browser:

```bash
./scripts/smoke-copilot.sh
```

This verifies the Copilot Runtime agent endpoint and a streamed MCP-backed balance response. It prints additional manual browser checks for frontend actions and structured UI components.

### Canonical read presentations

Try these separate requests:

- `Show my account summary` — the approved account-summary component with verified account balances.
- `What is my total balance?` — the same component using the all-accounts scope, irrespective of a selected Activity account.
- `What was my last transaction?` — a single transaction-detail component backed by a one-record MCP query.
- `What are my recent transactions?` — the multi-record transaction-list component.
- `Why was my last transaction declined?` — an agent-assisted explanation grounded in the retrieved transaction status; it is deliberately not classified as a simple latest-transaction display request.

The first four have deterministic intent-to-capability-to-presentation mappings. Their graphical presentation is selected by the application contract rather than left to model preference. The explanation request remains agent-assisted because it requires interpretation.

### Contacts and recipient clarification

Open **Contacts**. The workspace contains ten synthetic saved contacts. There are two Johns and two Annas deliberately, so you can verify ambiguity handling. Names, banks and masked `DEMO` account references are fictional; they are not real or verified payees.

For a chat-initiated transfer, first select a source account in **Accounts** or **Activity**, or include the account name explicitly in the request. The assistant will not silently choose the first of several EUR accounts.

Recommended tests:

1. With an EUR source account selected, ask: `Send 50 EUR to John`. ORAI searches Contacts through MCP and must ask which John, showing John Carter / Northstar Bank / masked `1042` and John Mitchell / Union Financial / masked `2781`.
2. Reply `the first one` (or `John Carter`). ORAI should open an editable transfer draft for that contact. Nothing has been previewed, approved or submitted.
3. Repeat with `Send 50 EUR to Anna`. ORAI must ask which Anna and show both saved matches.
4. Ask `Send 50 EUR to Michael Chen`. This is a unique saved contact and should resolve without recipient clarification, provided a source account is selected or explicitly named.
5. With no source account selected, a duplicate-recipient request should still ask you to identify the contact first. After the recipient is resolved, ORAI asks which compatible source account to use. Reply with an account name or option number; it must not choose one arbitrarily.
6. Try `Send 50 EUR to a person who is not in Contacts`. ORAI must explain that no active saved match was found and must not open a draft.

The order is intentional: recipient resolution comes from a successful current-turn MCP search; source-account selection comes from trusted application context or an explicit customer choice; the frontend opens only an editable draft. The Banking Kernel independently rechecks contact ownership/status, account ownership/status, amount, currency, funds and policy when the customer requests a preview and again at confirmation. This local demo does not perform live beneficiary verification or Verification of Payee, and the demo SCA challenge is not real SCA.

## 6. Connect a model (optional)

The default `demo` provider uses deterministic assistant responses while still making real MCP calls for supported banking reads. To use a model, edit `.env` and rebuild only Agent Runtime:

```bash
docker compose up -d --build agent-runtime
```

### Option A — Ollama on the Docker host

Start Ollama on your host and pull a model suitable for your machine:

```bash
ollama pull qwen2.5:7b
```

Configure:

```dotenv
LLM_PROVIDER=ollama
LLM_MODEL=qwen2.5:7b
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_NUM_CTX=8192
OLLAMA_KEEP_ALIVE=5m
```

`OLLAMA_KEEP_ALIVE` is passed to the ChatOllama adapter and controls the requested model keep-alive period. The container uses `host.docker.internal` to reach Ollama on the host; `localhost` inside Agent Runtime would point back to that container. The `LLM_EXTERNAL_DATA_APPROVED` setting gates OpenAI/Anthropic, not Ollama URL locality: use a remote Ollama endpoint only after reviewing it as an external destination for the banking context sent to the model.

### Option B — OpenAI or an OpenAI-compatible endpoint

```dotenv
LLM_PROVIDER=openai
LLM_MODEL=gpt-4.1-mini
OPENAI_API_KEY=replace-with-your-token
OPENAI_BASE_URL=https://api.openai.com/v1
LLM_EXTERNAL_DATA_APPROVED=true
```

For a private OpenAI-compatible gateway, set `OPENAI_BASE_URL` to its API root before `/chat/completions` and choose a model it exposes.

### Option C — Anthropic

```dotenv
LLM_PROVIDER=anthropic
LLM_MODEL=claude-3-5-haiku-latest
ANTHROPIC_API_KEY=replace-with-your-token
LLM_EXTERNAL_DATA_APPROVED=true
```

**External-provider data approval:** OpenAI and Anthropic are blocked unless `LLM_EXTERNAL_DATA_APPROVED=true`. This is an explicit operator acknowledgement, not a technical or contractual guarantee. Before enabling it, review provider retention, training, residency and subprocessor terms. Chat content/history and retrieved banking data are subject to the project's minimization/redaction controls, which are not a complete DLP system.

After restarting Agent Runtime, check the provider status returned by:

```bash
curl -fsS http://localhost:8000/api/config | python3 -m json.tool
```

## 7. Prepare and execute a synthetic transfer

A transfer draft from ORAI is only a suggestion. It does not create a payment command. To demonstrate the complete customer-controlled process:

1. Ask ORAI: `Send €25 to John Carter from my Everyday account`.
2. Review and edit the populated form. Nothing has been previewed, approved or submitted yet.
3. Select **Review transfer**. The Banking Core API derives the demo identity and checks account ownership, account state, amount, currency, funds, policy and transfer limits, then returns a persisted expiring preview.
4. Read the exact beneficiary, source account, amount, fee and total displayed in the preview.
5. Confirm that exact transaction and enter the visible local demo code `123456`.
6. Select **Confirm & submit to Temporal**. The API reloads the stored preview and revalidates its digest, expiry, single-use state and policy. It atomically records approval, command, audit events and an outbox event in PostgreSQL.
7. The worker dispatches the outbox event to Temporal using a stable workflow ID.
8. Open http://localhost:8233 to inspect the workflow. Its activity invokes the synthetic PostgreSQL ledger adapter, books the debit and a matching transaction, and records reconciliation evidence.
9. Return to ORAIflow. The command status and balance/activity refresh from the Banking Core API. ORAI can subsequently read the booked transaction through MCP.

**Why there are several steps:** the agent may interpret intent, but only the customer-facing application can request the Kernel preview and submit the separate confirmation. The Kernel is the authority for identity and policy. PostgreSQL atomically records the command and dispatch intent. Temporal makes the long-running execution durable. The ledger adapter determines the booked result.

The visible `123456` challenge is **demo-only**, not real SCA. The transfer changes fictional data in your local PostgreSQL volume only.

## 8. Run tests and diagnostics

### Main CopilotKit path

```bash
./scripts/smoke-copilot.sh
```

### Direct `/api/chat` diagnostic path

```bash
./scripts/smoke.sh
```

This deliberately bypasses the browser's CopilotKit/AG-UI route to test the retained direct API-to-Agent Runtime request path. It is useful for isolating whether a problem is in the agent/MCP service or the CopilotKit/AG-UI/frontend integration. It is not the normal customer chat route.

### Full transfer smoke test

```bash
./scripts/smoke-transfer.sh
```

This script performs a full synthetic EUR 1.00 transfer, checks settlement/reconciliation/audit, and performs an MCP read-back through the direct diagnostic chat endpoint. It **writes to the demo ledger**; use it when you are comfortable creating a test debit.

### Automated Python tests

```bash
uv run --locked pytest -q
```

The JSONB regression test uses `TEST_DATABASE_URL` when present. CI runs it against a disposable PostgreSQL 17 service. See [tests/README.md](../tests/README.md).

### Inspect logs by boundary

```bash
docker compose ps -a
docker compose logs --tail=120 web
docker compose logs --tail=120 copilot-runtime
docker compose logs --tail=120 agent-runtime
docker compose logs --tail=120 mcp
docker compose logs --tail=120 api
docker compose logs --tail=120 worker
docker compose logs --tail=120 temporal
docker compose logs --tail=120 postgres
```

For a frozen or non-responsive assistant tab, open the browser developer console and capture the `[ORAI chat timing]` entries while reproducing the issue. In another Bash terminal run `docker compose logs --since=5m --follow copilot-runtime agent-runtime mcp`. The timing entries distinguish first assistant output from the actual composer-ready transition; server logs separately measure model generation, MCP calls, AG-UI event completion and Copilot Runtime response lifetime. See [Developer tools](DEVELOPER-TOOLS.md#diagnosing-assistant-run-latency-v08) for interpretation. Do not assume that Temporal is involved: reading account/activity data does not start a transfer workflow.

## 9. Profile and workspace customisation

The Profile workspace contains **Personal Information**, **Preferences** and **Customization**.

- Personal profile fields (display/preferred name, email, phone and regional preferences) are server-stored in PostgreSQL under the fixed demo identity.
- Workspace theme, font size, layout density, reduced motion, default workspace, Overview banner artwork and its visibility are saved in the browser's local storage. The Overview banner is hidden by default; Profile → Customization lets you enable it, choose one of the four designs, or turn it off again. When enabled, it appears above the greeting and all factual dashboard content. These are presentation preferences, not banking records.
- `DEMO_WORKSPACE_NAME` changes the workspace label in the sidebar. `DEMO_DISPLAY_NAME` seeds a new profile; existing profile edits are retained.
- `DEMO_SALARY_EMPLOYER` changes the seeded salary label. `DEMO_SALARY_AMOUNT_MINOR` changes the synthetic salary credit and applies the same delta to its account balance exactly once.

Changing the preferred currency does not convert balances or mutate account currency.

## 10. Stop, restart and protect your demo data

Stop services but keep the database volume:

```bash
docker compose down
```

Start again:

```bash
docker compose up -d
```

The PostgreSQL `postgres-data` named volume persists across normal stops and restarts. **Do not use `docker compose down -v` as a routine cleanup command**: it deletes that volume and your synthetic ledger/profile/command data. Temporal dev history is stored in `/tmp/temporal.db` inside the Temporal container and is disposable if that container is recreated; PostgreSQL remains the demo source of record.

## 11. Troubleshooting by boundary

| Symptom | First check | What to verify |
|---|---|---|
| Workspace cannot load data | `docker compose ps -a`; `docker compose logs --tail=100 api` | API health, PostgreSQL health and successful `migrate` exit |
| ORAI does not connect | `docker compose logs --tail=100 web copilot-runtime agent-runtime` | Nginx `/api/copilotkit` proxy, Copilot Runtime health, AG-UI URL and Agent Runtime startup |
| Copilot path fails but direct diagnostic succeeds | `./scripts/smoke-copilot.sh` plus `./scripts/smoke.sh` | Focus on Copilot Runtime, AG-UI events, frontend tool registration and browser state |
| Both assistant paths fail | `docker compose logs --tail=100 agent-runtime mcp` | Agent Runtime health, shared `MCP_ACCESS_TOKEN`, MCP health and model provider settings |
| MCP returns HTTP 401 | Agent Runtime and MCP `.env` token values | `MCP_ACCESS_TOKEN` must match in both services; recreate both after changing it |
| Provider not configured | `curl -fsS http://localhost:8000/api/config` | `LLM_PROVIDER`, provider key, model name and external-data approval |
| Ollama cannot be reached | Agent Runtime logs | Ollama is running on host and `OLLAMA_BASE_URL` uses `host.docker.internal` |
| Private HTTPS model gateway fails | Agent Runtime logs and `./certs/` | DNS, correct `CUSTOM_CA_FILE` filename and public CA trust chain |
| Transfer remains `PENDING` | Worker logs and Temporal UI | Worker can connect to Temporal and polls `TEMPORAL_TASK_QUEUE`; check the outbox error in PostgreSQL |
| Transfer is `REQUIRES_RECONCILIATION` | Command status, worker logs and ledger/audit evidence | Do not submit a second transfer. Establish the original outcome before retrying manually |
| PostgreSQL password error | `.env` `POSTGRES_*` and `DATABASE_URL` | Keep database username/password/database values consistent; changing an initialized Postgres volume's credentials requires a deliberate database procedure, not a casual volume reset |

## 12. What is intentionally not implemented

This release does not provide production OIDC login, multi-customer tenant isolation, OAuth 2.1 MCP authorization, production SCA, live bank/core APIs, a live payment rail, a production Temporal cluster, or card servicing. The audit chain is tamper-evident local evidence, not independent WORM storage. The static local service tokens are development guards, not an identity system.

Read [Architecture](ARCHITECTURE.md) for the detailed component/data-flow explanation and [Security boundaries](SECURITY-BOUNDARIES.md) before connecting any external service. Never connect real customer credentials or move real funds through this demo.
