# ORAIflow Decision Routing Policy

**Policy:** `oraiflow.default`  
**Schema:** `1.0.0`  
## Purpose

The Decision Routing Policy describes how the Agent enters the application’s existing capabilities and resources. It is a small, human-editable decision map—not a second capability registry, a customer-phrase parser, an executable workflow engine or an authorization system.

The policy answers:

- Which approved deterministic/resource routes should be attempted before semantic interpretation?
- Once an intent has been validated, which existing runtime owner handles that task class?
- When can a response be served from maintained resource content, and when is an authenticated read or a workflow required?

The policy does **not** define the actual capability schemas, arguments, customer scopes, presentation schemas, authorization requirements or banking effects. Those remain owned by OCCF and the existing runtime/domain modules.

## Ownership boundaries

| Concern | Canonical owner | Policy role |
|---|---|---|
| Intent IDs and semantic meaning | OCCF `intents` | References registered intent IDs via the compiled map |
| Capability IDs, schemas, effects, permissions | OCCF `capabilities` | Describes the safe capability surface; never duplicates schemas |
| Presentation and frontend action contracts | OCCF `presentations` / `capabilities` | Describes the runtime owner, not UI implementation |
| Academy and workspace content | OCCF application catalogues | Identifies resource classes; content is retrieved from its owner |
| Route order and enablement | `decision_routing.json` | Canonical owner |
| Task-class dispatch strategy | `decision_routing.json` | Canonical owner for approved runtime handlers |
| Workflow stages and transitions | Existing typed workflow modules | Policy points to the workflow owner; it does not repeat stages |
| Financial authorization and approval | Banking Kernel / existing security boundary | Never configurable or overridable through this policy |
| Outbox-backed orchestration | PostgreSQL outbox + Temporal | Remains independent of interpretation and route ranking |

## Turn lifecycle

1. Restore pending workflow/task state and reconcile any execution receipts.
2. Run enabled deterministic/resource fast routes in configured priority order. A route that does not apply returns no match and does not block later routes.
3. If no fast route owns the turn, perform one semantic assessment. The interpreter receives the compiled decision map with the new turn context.
4. The interpreter returns registered intent IDs, requested outcomes, user-supported slots/entities, evidence and applicable confidence values. It does not choose capability IDs, tools, arguments, renderers or authorization decisions.
5. The runtime validates the assessment and derives the task class and exact execution plan from OCCF.
6. Existing resource, MCP, presentation, frontend or workflow owner handles the task. A workflow advances through deterministic typed transitions; the LLM is not asked to choose the next tool at each stage.
7. One authoritative outcome is finalized and returned to the UI.

A new customer message can require a new semantic assessment. Internal workflow stages and backend receipts do not.

## Policy file

The policy lives at `shared/contracts/decision_routing.json` and is validated by `shared/contracts/decision_routing.py` using strict Pydantic models (`extra="forbid"`). Its human-facing JSON Schema is published at `shared/contracts/decision_routing.schema.json`, generated from the same Pydantic model rather than maintained as a second contract.

### `pre_assessment_routes`

This ordered list controls existing fast/resource routes. Each item has a stable ID, unique priority, enablement flag, allow-listed handler and human-readable purpose. The list must be sorted by descending priority.

A human may:

- Set `enabled` to `false` to disable an approved route.
- Change the priority of an approved route, keeping priorities unique and the list sorted.
- Change the policy description/purpose for documentation clarity.

The handler is allow-listed in code. JSON cannot introduce Python functions, expressions, imports or arbitrary execution. A genuinely new route requires a reviewed runtime handler, schema update and tests.

### `task_dispatch`

The map defines one dispatch strategy for each task class:

| Task class | Handler | Meaning |
|---|---|---|
| `resource` | `registered_resource` | Serve maintained Academy, workspace or assistant-catalogue knowledge without inventing an MCP capability |
| `mcp_read` | `occf_read` | Resolve the registered intent to an authenticated read-only OCCF/MCP capability |
| `presentation` | `registered_presentation` | Execute registered presentation preflight reads and render validated results |
| `workflow` | `registered_workflow` | Continue an existing typed deterministic workflow |
| `frontend_action` | `registered_frontend_action` | Run an allowed frontend action and validate its receipt |
| `clarification` | `preserve_and_clarify` | Preserve task identity and known slots; ask for only missing/ambiguous information |
| `unsupported` | `explain_supported_scope` | Explain useful registered resources or the boundary; do not invent an operation |

Task class is derived from the canonical OCCF execution plan. The model cannot set it.

### `interpreter`, `confidence` and per-task confidence profiles

Each task-dispatch class declares which confidence dimensions may apply and what to do when confidence is insufficient (for example, preserve a workflow and ask for only its missing field). The runtime intersects the configured dimensions with the actual task slots/entities; it does not demand irrelevant confidence scores. Thresholds remain in the existing calibration configuration, not copied into each route.

The interpreter policy documents the one-assessment-per-new-turn rule, maximum one format recovery, and the prohibition on choosing tools or authorizing actions. Confidence mode continues to come from the existing `ORAI_CONFIDENCE_GATE_MODE` configuration. Thresholds, when enforcement is enabled, must be explicitly calibrated for the relevant dimension/task class.

Confidence is never financial authorization, customer consent, beneficiary verification, SCA or a replacement for Banking Kernel policy.

## Compiled Agent decision map

`services/agent_runtime/decision_router.py` joins the human policy to the live OCCF registry and builds the prompt-ready map once when the Agent graph starts. The same compiled map is injected into each new semantic-assessment prompt; the Agent does not rediscover tools or rebuild the application catalogue per turn. The policy ID, revision, registry version and compiled-map size are recorded in startup logs. This avoids copying capability definitions into a second file.

The compact map includes:

- Registered intents, their purpose, derived task class, runtime owner and required/optional slots and entities.
- For intents with registered `semantic_inputs`, a safe semantic projection: field location, description, allowed canonical values, default and default policy, plus at most one curated customer-language example and a concise outcome description. This tells the interpreter what the registered fields mean without giving it executable mappings.
- Agent-invocable capabilities and their intent relationships, without duplicating their JSON Schemas.
- Workspace names and short purposes.
- Academy topic IDs, titles and personal-data intent relationships.
- Assistant capability catalogue references.
- Route ordering, task-class dispatch and authority rules.

The map deliberately omits the complete phrase inventory, executable semantic-to-argument mappings, full learning content, raw customer balances, account identifiers, contact records, API credentials and full capability schemas. Those belong to their respective owners and are supplied only when needed. Curated language examples are illustrative, not required phrases; the interpreter must generalize from meaning. The model needs to understand the decision surface, not re-discover the whole application or memorize a list of customer phrasings.

## Resource tasks are first-class

Not every useful request requires an executable tool. For example:

- “What is credit scoring?” → `ACADEMY_EDUCATION` → `academy_topics` resource.
- “What is Academy for?” → `ACADEMY_EDUCATION` or `WORKSPACE_INFORMATION` → registered catalogue.
- “What can you help me with?” → `ASSISTANT_CAPABILITY_HELP` → registered assistant capability catalogue.
- “What are my current loans?” → `LOAN_INFORMATION` → authenticated OCCF/MCP read.
- “How would another loan affect my badge?” → registered personal Financial Health read plus maintained explanation.
- “Open Audit” → `WORKSPACE_NAVIGATION` → registered frontend action, if available and permitted in the current UI.

General education does not require an MCP capability. Personalized financial facts do.

## Validation and safe editing

The runtime validates the policy at load time. It rejects unknown fields, unsupported handlers, duplicate route IDs/priorities, invalid task-class mappings and incomplete task-class coverage. The compiled map also checks that intent resource IDs and runtime task owners resolve against the live OCCF registry.

After editing the JSON:

1. Keep the JSON syntactically valid.
2. Validate against the published schema at `shared/contracts/decision_routing.schema.json`.
3. Run `python -c "from shared.contracts.decision_routing import load_decision_routing_policy; print(load_decision_routing_policy())"`.
4. Run `uv run --locked pytest -q tests/test_decision_routing.py tests/test_capability_registry.py`.
5. Restart the Agent Runtime so its cached policy is reloaded.
6. Verify the route selected and its outcome in structured turn logs.

Do not edit generated frontend contracts by hand. If OCCF changes, run `python scripts/generate_contracts.py` and verify with `python scripts/generate_contracts.py --check`.

## Non-goals

- No list of 100 customer phrase variants.
- No recursive LLM selection of MCP tools.
- No duplicate capability/schema registry.
- No model-controlled workflow transitions.
- No confidence-based authorization.
- No route configuration capable of bypassing customer scope, MCP validation, frontend preconditions, Banking Kernel approval or Temporal ownership.
- No claim that this source-level policy test replaces live-provider/browser/AG-UI/MCP integration testing.
