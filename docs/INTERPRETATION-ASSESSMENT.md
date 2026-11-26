# Interpretation Assessment — current protocol

**Current package:** `v0.7.7`  
**OCCF registry:** `1.5.17`

## Purpose and authority boundary

The LLM interprets the customer's language, current turn, permitted conversation references and minimized application context. It proposes registered semantic tasks. It does not choose executable tools, create capability arguments, select SQL, authorize actions or establish financial truth.

The Agent Runtime validates the proposed assessment, resolves it against the canonical OCCF registry, derives task classes, compiles capability inputs and validates the result before the owning boundary executes it.

## Model-facing assessment

The current `TurnInterpretationAssessment` is a strict, versioned schema. It carries:

- Top-level resolution and, when necessary, a customer-facing clarification message.
- Up to five proposed tasks, each with a registered `intent_id` and requested outcome.
- Task resolution, scalar slots and entities, field-specific evidence and applicable confidence dimensions.
- Allowed conversation-message references for supported contextual follow-ups.

Evidence items identify a field, source (`current_turn`, `conversation`, `assistant_context`, `application_context` or `capability_result`) and an exact source quote. Extra fields are rejected. Values are normalized only within the schema's documented scalar rules; arbitrary nested arguments are not accepted.

The provider-native Pydantic/JSON-schema structured-output path is preferred where supported. `oraiflow_assess_turn` remains a bounded compatibility transport for providers that need it. It is an assessment transport, not an executable capability. Private child model calls are isolated from customer-facing AG-UI publication.

## What the model can know about contracts

The prompt receives the full registered intent catalogue plus a compact OCCF-derived decision map. For intents that declare `semantic_inputs`, the model receives field location, meaning, allowed canonical values, defaults and their policy, and concise outcome semantics.

The following remain runtime-owned and are not exposed as executable instructions to the model:

- Capability IDs and MCP tool names as a selection authority.
- Semantic-to-capability argument mappings.
- Raw MCP/backend JSON Schemas and SQL.
- Customer/tenant identity and authorization context.
- Frontend action IDs and Kernel operations.

Lexical intent discovery (exact phrases, keywords, typo-tolerant matching and canonical deterministic routes) is a candidate/optimization mechanism. The full registered intent catalogue is still provided to the model in the model-backed path. Candidate matches do not prove intent and must not be treated as a supported-language whitelist.

## Runtime validation and planning

The runtime validates registry membership, task shape, field declarations, evidence-source rules, confidence policy and supported semantic values. For a field already declared by OCCF, the planner also canonicalizes its location when the model places it in `slots` instead of `entities` (or the reverse); conflicting duplicate values are still rejected. The planner resolves contract-declared defaults only when the customer has not supplied a conflicting value, derives required fields from OCCF schemas and workflow rules, and compiles only through registered mappings. It then validates the final argument object against the capability schema.

An LLM-proposed clarification is provisional. A read task may proceed when the registered contract supplies a safe default and all validation passes. Missing beneficiary, amount, currency, source-account or other consequential workflow requirements remain subject to their explicit workflow guards. Confidence is an interpretation-quality signal, never a financial authorization signal.

The `orai_*` adapter in `services/agent_runtime/interpretation.py` is retained as an internal compatibility bridge into the existing final OCCF validation/ToolNode path. It is not part of the model-visible contract and should only be removed after the downstream validation boundary is migrated and conformance-tested.

## Current semantic coverage and limitations

The registry contains 17 intents. Three currently declare the richer `semantic_inputs` model:

- `ACCOUNT_TOTAL_BALANCE` — explicit all-account scope and aggregation semantics.
- `TRANSACTION_EXPLANATION` — supported `latest` selector and default.
- `TRANSACTION_ANALYSIS` — bounded period, flow, measure, grouping and analysis-mode semantics.

Other intents still rely more heavily on required/optional slots, entities, descriptions, workflow rules and resource metadata. This is a meaningful incompleteness, not evidence that all intents share one fully developed semantic contract.

The deterministic candidate ranker remains useful for canonical fast paths and relevance hints, but unseen-language generalization has not been demonstrated with a held-out, configured-provider benchmark. For registered semantic fields that declare OCCF evidence phrases, the runtime recovers omitted or unevidenced fallback values from an unambiguous current-turn phrase before evidence validation. A field-level model proposal with contradictory evidence is not silently rewritten; it remains subject to the normal validation gate. Unmentioned optional semantic fields are removed from the model proposal so the planner can apply their registered defaults after evidence validation. Explicit but unsupported selectors remain unresolved and must not silently become defaults. This is bounded contract-based evidence repair, not a universal natural-language entailment solution; provider-backed evaluation remains required.

## Provider and verification caveat

`LLM_PROVIDER=demo` is the default in the sample Compose configuration. It uses a deterministic demo graph and is not semantically equivalent to the hosted/local model-backed interpreter. Semantic acceptance must record and verify the actual provider/model and exercise the customer path: React/CopilotKit → AG-UI → LangGraph → planner → MCP/browser → response receipt.

Automated unit and contract tests are useful for deterministic behavior, but they do not prove semantic quality with a configured model provider. A deployment should validate the complete customer path through React/CopilotKit, AG-UI, the Agent Runtime, planner, MCP and database using the provider and model that will actually be enabled. Unit results must not be presented as end-to-end acceptance.

## Source map

- Model schema, prompt, parsing: `services/agent_runtime/task_interpretation.py`
- Evidence/confidence validation and compatibility adapter: `services/agent_runtime/interpretation.py`
- Canonical semantic/capability definitions: `shared/contracts/registry.json`
- Registry validation: `shared/contracts/registry.py`
- Semantic-to-capability compilation: `services/agent_runtime/task_planner.py`
- Deterministic route/decision map: `shared/contracts/decision_routing.json`, `services/agent_runtime/decision_router.py`
- Behavioral lifecycle: [Interpretation and Contract Flow](INTERPRETATION-AND-CONTRACT-FLOW.md)
- Test execution and behavioural evaluation guidance: [Test Guide](../tests/README.md)
