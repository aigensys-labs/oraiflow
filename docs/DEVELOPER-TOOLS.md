# Local developer tools and diagnostic paths

Run the stack locally before using these endpoints. Compose publishes the UI, Banking Core API, Agent Runtime diagnostics and Temporal UI on loopback by default.

| Tool | URL / command | Purpose |
|---|---|---|
| ORAIflow UI | http://localhost:3000 | Normal customer application and CopilotKit chat |
| Banking Core API docs | http://localhost:8000/docs | Inspect API request/response contracts |
| Temporal UI | http://localhost:8233 | Inspect workflow execution history |
| Agent Runtime docs | http://localhost:8101/docs | Loopback-only Python Agent Runtime diagnostics |
| CopilotKit primary-path smoke test | `./scripts/smoke-copilot.sh` | Tests the browser-facing runtime endpoint through AG-UI to LangGraph/MCP |
| Direct API chat smoke test | `./scripts/smoke.sh` | Tests the separately retained `POST /api/chat` diagnostic route |
| End-to-end transfer smoke test | `./scripts/smoke-transfer.sh` | Books a synthetic EUR 1.00 transfer and validates workflow/ledger/audit/MCP read-back |

## Primary customer assistant path

The application uses CopilotKit/AG-UI for normal chat:

```text
Browser React ORAI chat
  → Nginx /api/copilotkit/
  → TypeScript CopilotKit Runtime
  → AG-UI
  → Python LangGraph Agent Runtime
  → MCP client and server
  → read-only PostgreSQL role
```

The custom ORAI chat surface is retained; CopilotKit is the runtime/transport and frontend tool lifecycle, not a replacement chat UI. Frontend tools such as workspace navigation or account-filter selection execute in the browser and are allowlisted presentation actions. Financial preview and commitment remain API/Kernel operations.

`./scripts/smoke-copilot.sh` calls `/api/copilotkit/info`, starts an AG-UI agent run, checks for a streamed account/balance response and requires the terminal `RUN_FINISHED` event. It also prints manual browser checks for frontend navigation and structured account/transaction components. It does not create a transfer.

## Retained direct `/api/chat` diagnostic path

`POST http://localhost:8000/api/chat` is deliberately preserved for isolated API-to-Agent Runtime read/agent/MCP diagnostics. The Banking Core API forwards it internally to the Agent Runtime `/api/chat` endpoint using `AGENT_RUNTIME_INTERNAL_TOKEN`. The React customer UI does not use this route for normal chat. This diagnostic surface does not synthesize a transfer draft; transfer preparation belongs to the primary CopilotKit/AG-UI flow, where Contacts resolution and source-account selection are enforced.

Use:

```bash
./scripts/smoke.sh
```

This checks API health/config/overview and performs one direct chat request. The route does not have CopilotKit's AG-UI stream or browser frontend-tool lifecycle. It helps separate model/MCP/API issues from CopilotKit/AG-UI/browser issues.

The transfer smoke script also uses the direct endpoint only for its final assistant read-back, after the transfer has already settled. That does not change the primary browser route.


### MCP-before-GenUI contract

For `render_account_summary` and `render_transaction_list`, the Agent Runtime enforces a read-before-render precondition. If the model requests one of these display-only components without a current-turn result from the matching read-only MCP capability, the LangGraph schedules that MCP read first. The model is invoked again only after the result is available, and the component arguments are overwritten with validated data from the MCP response envelope. Model-authored financial values are never accepted as component data. A normalized connector error terminates the turn with a customer-safe unavailable message rather than asking the model to infer missing facts.

The demo-mode graph also performs its MCP read before returning these components. This precondition currently covers the two registered financial GenUI components; broader grounding of every natural-language financial claim is part of the subsequent connector-contract work.

## CopilotKit Inspector

The TypeScript CopilotKit Runtime in this project is a protocol/runtime gateway, not a standalone administration dashboard. Its `/healthz` and `/api/copilotkit/info` endpoints are service/metadata checks, not a user-facing dashboard.

CopilotKit Inspector is an in-app development overlay. The React integration can display it in development when the app is opened on localhost/loopback. The Docker web image is a Vite production build, so it does not publish an Inspector dashboard URL. To run the Vite development server:

```bash
cd apps/web
npm install --no-audit --no-fund
npm run dev
```

Then open the development URL printed by Vite (normally http://localhost:5173). Keep Inspector restricted to a local development environment because it may expose conversation content and AG-UI events.

CopilotKit Intelligence (threads, analytics and hosted dashboard capabilities) is a separate service/deployment. It is not included in this repository's Copilot Runtime container.

## Assistant lifecycle, capability contracts and run latency

The assistant composer remains editable while a run is active. Up to five follow-up messages can be queued and are submitted sequentially after each preceding AG-UI run completes; they are never started concurrently. The header and composer footer continue to show a responding state even after the first assistant content appears. Display-only account/transaction GenUI components use `followUp: false`, so rendering a completed verified result does not request an unnecessary model continuation. Navigation, account selection and transfer-draft frontend actions retain their follow-up behavior because the agent needs their browser execution result.

To measure one slow request, open the browser developer console, ask ORAI a read-only question such as “What are my recent transactions?”, and compare the privacy-safe `[ORAI chat timing]` entries:

- `message submitted` marks the browser submission.
- `first assistant output` measures time until the first assistant message/tool output becomes visible to the agent hook.
- `runAgent promise resolved` measures the CopilotKit client promise duration.
- `composer ready` measures the actual `agent.isRunning` lifecycle and the delay from first output to the point at which Send is re-enabled.

The server logs split the same request into stages:

- `copilot-runtime` logs `agent_run_response` duration and detects premature client disconnects.
- `agent-runtime` logs `agui_run_started` and `agui_run_finished`, emitted event counts, first message/tool event, total duration and whether `RUN_FINISHED` was emitted. Its model/MCP/AG-UI application loggers are explicitly configured to write to stdout; events include a per-AG-UI `trace_id`.
- `agent-runtime` logs `llm_stream_finished` with provider, first-chunk and total model-generation time when a hosted/local model is used.
- `agent-runtime` logs `mcp_call_started` and `mcp_call_finished` with the MCP tool name and full session/initialization/tool-call duration. It does not log tool arguments or returned banking data. Connector exceptions are converted to a typed `connector_error` envelope and a customer-safe response; raw connector details are not returned to the model or customer.

Example log capture (Bash):

```bash
docker compose logs --since=5m --follow copilot-runtime agent-runtime mcp
```

How to interpret the result:

| Observation | Likely area to investigate |
|---|---|
| Long `llm_stream_finished` | Model/provider latency or cold start |
| Long `mcp_call_finished` | MCP connection/session initialization, MCP service or PostgreSQL query |
| Fast MCP/model timings but long `agui_run_finished` | LangGraph state/checkpointing or AG-UI event finalization |
| AG-UI logs `RUN_FINISHED`, but browser `composer ready` is much later or absent | Copilot Runtime/browser event consumption or React agent state |
| Browser promise resolves but `agent.isRunning` remains true | CopilotKit frontend agent lifecycle/state issue |
| AG-UI stream lacks `RUN_FINISHED` or logs an exception/client close | Server-side run completion or stream interruption |

A recent-activity query is read-only and does not invoke Temporal. Do not use Temporal timing to explain a read-only chat delay. Compare `./scripts/smoke-copilot.sh` with `./scripts/smoke.sh` to separate the CopilotKit/AG-UI path from the retained direct diagnostic path.


## Capability contract development

`shared/contracts/registry.json` is the single language-neutral source for intent, MCP/frontend capability, GenUI presentation and Banking Kernel operation contracts. After editing it, regenerate and check the frontend schema bundle:

```bash
uv run --locked python scripts/generate_contracts.py
uv run --locked python scripts/generate_contracts.py --check
```

Run the focused cross-registry and source-inventory checks:

```bash
uv run --locked pytest -q tests/test_capability_registry.py tests/test_intent_contracts.py tests/test_contract_integration_inventory.py
```

The checks fail if a declared MCP tool, frontend action, renderer or Kernel route is missing from source; if a registry reference is unresolved; if a retry policy is incompatible with effect/idempotency; or if a result envelope violates outcome/evidence semantics. See [Capability contract framework](CAPABILITY-CONTRACTS.md) for the lifecycle, envelope semantics, security boundary and checklist for adding a capability.

## Useful log commands

```bash
docker compose ps -a
docker compose logs -f web copilot-runtime
docker compose logs -f agent-runtime mcp
docker compose logs -f api worker temporal
```

For assistant response problems, compare `./scripts/smoke-copilot.sh` with `./scripts/smoke.sh`. If the direct API diagnostic succeeds but CopilotKit fails, focus on the Copilot Runtime, AG-UI stream, frontend tool lifecycle and browser state. A read-only assistant request does not start a Temporal workflow; inspect Temporal only when a financial command has been accepted.

## References

- [CopilotKit Inspector](https://docs.copilotkit.ai/inspector)
- [CopilotKit Runtime endpoints](https://docs.copilotkit.ai/backend/runtime-endpoints)
- [AG-UI protocol](https://docs.ag-ui.com/)
