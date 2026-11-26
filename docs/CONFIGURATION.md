# Configuration reference

All local Compose settings are read from `.env`. Start with:

```bash
cp .env.example .env
```

Compose injects values into the relevant containers. Services read most settings at process startup, so recreate the affected service after a change:

```bash
docker compose up -d --force-recreate <service>
```

Use `--build` as well when source or dependencies changed.

## Database and restricted roles

| Variable | Default | Used by | Purpose |
|---|---|---|---|
| `POSTGRES_USER` | `oraiflow` | PostgreSQL and migration bootstrap | Local bootstrap/owner login |
| `POSTGRES_PASSWORD` | local placeholder | PostgreSQL | Local database password |
| `POSTGRES_DB` | `oraiflow` | PostgreSQL and derived URLs | Database name |
| `DATABASE_URL` | `postgresql+psycopg://...` | API, worker, migration | Banking Core connection |
| `MCP_DATABASE_PASSWORD` | local placeholder | migration and derived URL | Password for the read-only MCP login |
| `MCP_DATABASE_URL` | derived when blank | MCP | Optional explicit MCP database URL |
| `AGENT_CHECKPOINT_PASSWORD` | local placeholder | migration and derived URL | Password for the checkpoint-only login |
| `AGENT_CHECKPOINT_DATABASE_URL` | derived when blank | Agent Runtime | Connection restricted to `oraiflow_agent` schema |
| `LANGGRAPH_STRICT_MSGPACK` | `true` | Agent Runtime / LangGraph checkpoint dependency | Restricts checkpoint deserialization; leave enabled |

Keep `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` and `DATABASE_URL` synchronized. Migrations provision the dedicated MCP and checkpoint principals. If you override either complete connection URL, keep its credentials/database consistent with the corresponding role. URL-encode reserved characters in connection URL credentials.

The `postgres-data` volume persists. Seed SQL in `db/init/` runs when a new PostgreSQL data directory is initialized; the migration job applies additive changes to fresh or existing databases. Do not delete the volume just to apply application changes.

## Demo identity, workspace label and profile seed

| Variable | Default | Purpose |
|---|---|---|
| `DEMO_USER_ID` | `user-001` | Fixed demo subject used by API, MCP, Agent Runtime and Copilot Runtime thread scoping |
| `DEMO_TENANT_ID` | `tenant-demo` | Fixed server-derived demo tenant used by API and MCP |
| `DEMO_DISPLAY_NAME` | `Alex Morgan` | Initial display name for a newly bootstrapped profile; does not overwrite existing profile edits |
| `DEMO_TRANSACTION_HISTORY_ENABLED` | `true` in local Compose; otherwise `false` | Migration | Adds 28 stable-ID synthetic activity rows over about 63 days to the local sample accounts; idempotent and does not change account balance snapshots |
| `DEMO_WORKSPACE_NAME` | `Atlas Fintech` | Presentation-only workspace switcher label returned by `GET /api/config` |
| `DEMO_SALARY_EMPLOYER` | `Northstar Labs` | Employer label in the synthetic salary transaction `txn-001` |
| `DEMO_SALARY_AMOUNT_MINOR` | `1100000` | Synthetic monthly EUR salary credit in minor units; migration adjusts only `txn-001` and its matching account balance by the exact delta |

These are local demo configuration values, not customer authentication. Browser or model-supplied customer/tenant IDs are never accepted as identity. Editing a profile cannot change the demo subject or tenant.

## Transfer preview and local confirmation

| Variable | Default | Purpose |
|---|---|---|
| `DEMO_SCA_APPROVED` | `true` | Enables the local demo confirmation flow |
| `DEMO_SCA_CODE` | `123456` | Visible local challenge; not real SCA |
| `DEMO_MAX_TRANSFER` | `1000.00` | Synthetic transfer cap |
| `DEMO_POLICY_VERSION` | `demo-transfer-policy-v1` | Policy version included in preview context |
| `PREVIEW_TTL_MINUTES` | `5` | Expiring preview lifetime, clamped to 1–30 minutes |
| `PREVIEW_SIGNING_SECRET` | local placeholder | HMAC key for the exact preview payload; replace outside local development |

The API derives identity, validates the transfer, persists the preview and verifies the separate customer confirmation. The model cannot preview, approve or submit a transfer. The local code is a demo control, not regulated SCA.

## CopilotKit, Agent Runtime and MCP

| Variable | Default | Used by | Purpose |
|---|---|---|---|
| `AGENT_AGUI_URL` | `http://agent-runtime:8101/agui` | Copilot Runtime | Internal AG-UI agent endpoint |
| `AGENT_RUNTIME_INTERNAL_TOKEN` | local placeholder | Copilot Runtime, API, Agent Runtime | Service bearer token; Copilot Runtime uses AG-UI and API uses the retained diagnostic proxy |
| `AGENT_RUNTIME_URL` | `http://agent-runtime:8101` | API | `/internal/config` plus the direct diagnostic `/api/chat` proxy; not normal browser chat |
| `AGENT_SCRIPTED_STREAM_CHUNK_CHARS` | `12` | Agent Runtime | Target character size for progressive publication of completed assistant/resource messages (default delivery cadence) |
| `AGENT_SCRIPTED_STREAM_DELAY_MS` | `35` | Agent Runtime | Delay between progressive chunks; provider-generated smaller deltas remain unchanged. The chat UI also progressively reveals a batched completed assistant message. |
| `AGENT_RUNTIME_TIMEOUT_SECONDS` | `120` | API | Direct diagnostic chat timeout |
| `MCP_URL` | `http://mcp:8100/mcp` | Agent Runtime | MCP Streamable HTTP endpoint on the Docker network |
| `MCP_ACCESS_TOKEN` | local placeholder | Agent Runtime and MCP | Shared local service bearer token |

The normal customer chat path is React/CopilotKit → Nginx `/api/copilotkit/` → TypeScript Copilot Runtime → AG-UI → LangGraph Agent Runtime → MCP. `POST /api/chat` on the Banking Core API is deliberately retained as a separate developer diagnostic route; the normal customer UI does not use it.

MCP exposes seven registered agent-invocable read capabilities: account list, account balance, recent transactions, transaction analysis, saved-contact search, illustrative loan overview and recorded Financial Health overview. Loan data remains opt-in (`DEMO_LOAN_ENABLED=false` by default). The interpretation call is not given executable MCP tools; the Agent Runtime maps its validated assessment to an OCCF operation. MCP uses `oraiflow_mcp_reader`, a PostgreSQL role with `SELECT` privileges on `accounts`, `transactions`, `contacts`, `demo_loans` and `demo_loan_installments`. Agent Runtime does not receive the Banking Core `DATABASE_URL`; its LangGraph checkpoint URL is restricted to the `oraiflow_agent` schema.

These static tokens are local service guards. They are not OAuth 2.1, OIDC or customer authentication.

## Model provider settings

| Variable | Default | Applies to | Purpose |
|---|---|---|---|
| `LLM_PROVIDER` | `demo` | Agent Runtime | `demo`, `ollama`, `openai` or `anthropic` |
| `LLM_MODEL` | `gpt-oss:20b` | Agent Runtime | Selected provider's model name; deterministic demo does not load a model |
| `LLM_TEMPERATURE` | `0` | Agent Runtime | Model sampling temperature |
| `LLM_TIMEOUT_SECONDS` | `90` | Agent Runtime | Provider request timeout |
| `LLM_MAX_RETRIES` | `1` | Agent Runtime | Provider retry setting |
| `LLM_EXTERNAL_DATA_APPROVED` | `false` | Agent Runtime | Operator opt-in before OpenAI/Anthropic receive minimized banking context |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Agent Runtime | Native Ollama HTTP endpoint |
| `OLLAMA_NUM_CTX` | `8192` | Agent Runtime | Ollama context window |
| `OLLAMA_KEEP_ALIVE` | `5m` | Agent Runtime | ChatOllama model keep-alive period |
| `OPENAI_API_KEY` | blank | Agent Runtime | OpenAI or compatible gateway credential |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Agent Runtime | OpenAI-compatible API root |
| `ANTHROPIC_API_KEY` | blank | Agent Runtime | Anthropic credential |

The `demo` provider requires no model key but still calls MCP for supported banking reads. Provider credentials are passed only to Agent Runtime. Banking Core API, MCP and worker do not receive them. OpenAI and Anthropic are blocked unless `LLM_EXTERNAL_DATA_APPROVED=true`; that flag is an operator acknowledgement, not proof of provider suitability. Review provider retention, training, residency and subprocessors. Pattern redaction is a baseline, not a complete DLP system.

For Ollama on the host, use `host.docker.internal` from inside the Agent Runtime container, not `localhost`. `LLM_EXTERNAL_DATA_APPROVED` gates the OpenAI and Anthropic integrations; it does not determine whether an Ollama endpoint is local or remote. Treat a remote Ollama URL or private gateway as an external data destination and use it only after operator review. For an OpenAI-compatible gateway, set `OPENAI_BASE_URL` to its API root before `/chat/completions`.

## DNS and public CA trust

| Variable | Default | Purpose |
|---|---|---|
| `API_DNS_SERVER_1` | `1.1.1.1` | First DNS resolver applied to backend containers |
| `API_DNS_SERVER_2` | `8.8.8.8` | Second DNS resolver applied to backend containers |
| `CUSTOM_CA_FILE` | `./certs/internal-ca.crt` | Host-side filename for an optional public CA |
| `CUSTOM_CA_CONTAINER_FILE` | `/run/oraiflow/custom-ca.crt` | Runtime copy path inside backend containers |
| `CA_BUNDLE_PATH` | `/run/oraiflow/ca-bundle.pem` | Merged CA bundle path |

Place only a public PEM/X.509 CA certificate in `./certs/`. Compose mounts the directory read-only into the backend Python containers (`migrate`, `mcp`, `agent-runtime`, `api`, `worker`). `docker/entrypoint.sh` copies the chosen CA into the runtime directory and appends it to the system trust bundle. `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` point to that merged bundle.

This supports outbound HTTPS to endpoints such as a private model gateway. It does not implement bank mTLS, client-certificate authentication or a live institution adapter. Never put private keys in `./certs/`.

## Host bindings and runtime settings

| Variable | Default | Used by | Purpose |
|---|---|---|---|
| `WEB_ORIGIN` | `http://localhost:3000` | API | CORS origin for the local web app |
| `API_BIND_ADDRESS` | `127.0.0.1` | Compose | Host interface for the published API port |
| `API_PORT` | `8000` | Compose | Host-side API port; the container listens on 8000 |
| `LOG_LEVEL` | `INFO` | Python services | Runtime logging level |
| `TEMPORAL_ADDRESS` | `temporal:7233` | Worker | Internal Temporal gRPC address |
| `TEMPORAL_TASK_QUEUE` | `oraiflow-transfers` | Worker | Queue used by dispatcher and worker |
| `BANK_LEDGER_ADAPTER` | `postgres` | Worker / adapter factory | Selects the only implemented synthetic PostgreSQL ledger adapter |

The web app is published at `127.0.0.1:3000`, API at `127.0.0.1:8000` by default, Agent Runtime diagnostics at `127.0.0.1:8101`, and Temporal at `127.0.0.1:7233` / `127.0.0.1:8233`. MCP and Copilot Runtime are internal-only. Setting `API_BIND_ADDRESS=0.0.0.0` deliberately exposes the API on all host interfaces; do not do this without appropriate network and application controls.

## Temporal local persistence

Compose runs Temporal in development mode with history in `/tmp/temporal.db` inside its container. That history is disposable if Temporal is recreated. Synthetic ledger state, commands, profiles and LangGraph checkpoints live in the persistent PostgreSQL `postgres-data` volume. This is local development configuration, not a production Temporal deployment.


## Illustrative loan demo

Illustrative demo-loan data is disabled by default. Set `DEMO_LOAN_ENABLED=true` to opt into the synthetic EUR 5,000 demonstration liability (or configure `DEMO_LOAN_PRINCIPAL_MINOR`, `DEMO_LOAN_APR_BPS` and `DEMO_LOAN_TERM_MONTHS`). When disabled, existing demo rows are preserved in storage but hidden from the customer Loans API/MCP response and excluded from Financial Health calculations. These values are illustrative, not a real credit agreement or offer; payment status is not tracked and no reminder or repayment capability exists. See [Loan account and repayment education](LOAN-ACCOUNT.md).
