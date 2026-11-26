# ORAIflow Interpretation and Contract Flow

## Purpose

This document describes the **semantic and conversational side** of ORAIflow: how a customer message becomes a validated interpretation, how that interpretation becomes a deterministic task plan, how the plan is translated into a registered operation contract, and how the relevant backend or application boundary accepts and returns that operation.

It complements rather than replaces the technical architecture documentation:

- [Architecture and module guide](ARCHITECTURE.md) explains the services, modules and technical boundaries.
- [Capability Contract Framework](CAPABILITY-CONTRACTS.md) explains the canonical OCCF registry and the complete capability lifecycle.
- [Interpretation Assessment](INTERPRETATION-ASSESSMENT.md) defines the model-to-runtime assessment and its evidence/confidence rules.
- [Decision Routing Policy](DECISION-ROUTING-POLICY.md) defines the deterministic routing policy.
- [Transfer workflow](TRANSFER-WORKFLOW.md) describes the transfer-preparation state machine in detail.

The intended audience is anyone designing, implementing, reviewing or evaluating the behavior of the Agent. This is the behavioral contract: what each stage is allowed to understand, decide, carry forward and execute.

**Current package baseline:** `v0.7.7`, OCCF registry `1.5.17`. This document describes the current implementation at that baseline, including its limitations and integration requirements. It is not a claim that every configured provider, browser or institution integration has passed end-to-end acceptance.

## 1. The governing idea

> **The model proposes what the customer means. The runtime decides what that meaning is allowed to become. OCCF defines the operation. The owning system validates and executes it.**

This separation is the foundation of ORAIflow's conversational architecture.

The LLM is good at understanding natural language, contextual references, paraphrases, entities and incomplete instructions. It is not the authority for application capabilities, executable parameters, customer identity, financial truth or financial authorization.

The deterministic Agent Runtime owns state progression, validation, task planning, required-field checks, capability mapping, execution scheduling, receipt correlation and terminal outcomes. OCCF is the canonical source for registered intents, capability schemas, presentations and Kernel operations. The receiving boundary independently validates the specific operation it is asked to perform.

The complete flow is therefore not:

```text
Customer prompt -> LLM chooses a tool -> backend trusts the tool call
```

It is:

```text
Customer message and conversation state
  -> controlled context assembly
  -> semantic interpretation (or an approved bounded fast path)
  -> strict assessment parsing
  -> evidence, reference and confidence validation
  -> OCCF-driven task classification and planning
  -> required-field derivation and argument construction
  -> capability-schema validation
  -> owning boundary's independent validation
  -> typed result receipt and task outcome
  -> grounded response composition and AgentTurnResponse
```

The interpretation assessment is **not** an executable backend request. It is an internal proposal consumed by the Agent Runtime.

## 2. The vocabulary: do not collapse these concepts

| Concept | Meaning | Owner |
|---|---|---|
| **Message / utterance** | The customer's actual words in the current turn, with stable message identity | UI / Agent transport |
| **Context** | Permitted recent conversation, assistant-visible references, limited application state and validated prior results | Agent Runtime assembly |
| **Intent** | A registered semantic purpose, such as account overview, workspace navigation or transfer preparation | OCCF registry; proposed by interpreter |
| **Task** | One independently trackable piece of work expressed by a message | Interpreter proposes; runtime owns identity and lifecycle |
| **Requested outcome** | Whether the customer is asking for information, explanation, education, navigation, action or an unknown outcome | Interpreter proposes; runtime validates |
| **Slot** | A scalar parameter that belongs to an intent/workflow, such as amount or source-account preference | Interpreter proposes; OCCF declares allowed fields |
| **Entity** | A referenced domain object or named thing, such as beneficiary, account or workspace | Interpreter proposes; runtime resolves against trusted catalogues/results |
| **Evidence** | A quote and source that supports a proposed interpreted value | Interpreter proposes; runtime verifies |
| **Task class** | Runtime-derived execution family: resource, MCP read, presentation, workflow, frontend action, clarification or unsupported | Decision Router / runtime |
| **Capability** | A registered bounded operation with input/output schema and authority declaration | OCCF |
| **Capability arguments** | The exact object satisfying one capability's input schema | Agent Runtime constructs; OCCF validates |
| **Result envelope** | A versioned, typed result describing data, provenance, scope, freshness/completeness, trace and outcome | MCP or frontend boundary; OCCF validates |
| **Task outcome** | What happened to one interpreted task, correlated with its receipts | Runtime derives as `TurnTaskOutcome` |
| **Turn outcome** | Consolidated execution status for the current customer turn | Runtime derives as `TurnOutcome` |
| **Kernel operation** | A typed financial API operation with independent policy and authorization | Banking Kernel; not agent-callable |

A model-proposed `intent_id` is not a capability ID. A slot/entity is not yet a backend argument. A capability result is not automatically a customer-facing claim. A transfer draft is not a payment authorization.

## 3. The separate contracts

There are several contracts because each boundary answers a different question. They should not be merged into one giant model/backend schema.

| Contract | Direction / use | What it carries | What it does not do |
|---|---|---|---|
| `TurnInterpretationAssessment` **1.0.0** | Interpreter -> Agent Runtime | Up to five proposed semantic tasks, registered intent IDs, requested outcomes, slots, entities, evidence, applicable confidence values, resolution and exact conversation references | Does not choose tools/capabilities, set executable task classes, provide trusted identity, authorize actions or issue backend calls |
| `TurnTaskPlan` **1.0.0** | Internal Agent Runtime state | Runtime-created task IDs, intent, derived task class/status, missing fields, registered capability IDs, normalized arguments/tool call references and task-to-receipt correlations | Is not sent as one object to MCP or the Banking Kernel |
| OCCF capability input schema | Agent Runtime -> MCP / browser action | Only the normalized argument object for the one registered capability being invoked | Does not carry the original prompt or the whole interpretation assessment |
| OCCF result envelope **1.0.0** | MCP / frontend -> Agent Runtime | Capability identity, outcome, schema-validated data, evidence source/scope/retrieval time/completeness, trace ID, normalized error and trust class | Does not itself prove a model-generated explanation is claim-grounded; it is validated operation evidence |
| `TurnTaskOutcome` **1.0.0** and `TurnOutcome` **1.2.0** | Internal result derivation | Per-task result status and aggregate current-turn status correlated to actual tool receipts | Are not authored by the LLM and do not grant execution authority |
| `AgentTurnResponse` **1.1.0** | Agent Runtime -> UI / AG-UI | Stable message identity and the versioned customer-turn response/receipt used by the UI | Does not infer success from the presence of a message or presentation |
| Kernel operation request/response contracts | Customer application -> Banking Kernel API | Typed preview, confirmation and status payloads | Are not exposed as Agent tools and are not accepted from model confidence or assessment output |

The version identifiers above refer to the current package contracts. The registry's OCCF definitions are authoritative; this guide explains how they are used together.

### 3.1 What the model is asked to produce

The strict `TurnInterpretationAssessment` schema currently contains:

- `schema_version` (`1.0.0`) and a top-level resolution: `RESOLVED`, `NEEDS_CLARIFICATION`, `OUT_OF_SCOPE` or `SOCIAL`;
- at most five `tasks`;
- for each task: `intent_id`, `requested_outcome`, `resolution`, `slots`, `entities`, `evidence`, `intent_confidence`, `slot_confidence`, `entity_confidence`, `clarification_question` and `conversation_references`;
- an optional top-level `clarification_message`.

Each evidence item has a field name, an allowed source (`current_turn`, `conversation`, `assistant_context`, `application_context` or `capability_result`) and an exact quote. Extra fields are rejected. Slot/entity values are scalar strings after limited normalization; booleans, nested objects and arrays are not silently accepted as structured parameters.

Notably absent are `capability_id`, MCP tool name, frontend action name, arbitrary tool arguments, database identity and authorization fields. Capability-selection confidence is not part of this assessment because capability selection belongs to the deterministic runtime.

Provider-native Pydantic/JSON-schema structured output is preferred. The compatibility route binds only the assessment operation `oraiflow_assess_turn`; that operation is intercepted as model output and is never executed as an MCP or frontend action. A malformed assessment may receive one bounded format-recovery attempt. A provider outage or timeout is not reclassified as a formatting problem.

### 3.2 What the runtime stores

The runtime validates and transforms the proposal. It creates its own task IDs and plan records. In the current package, `TurnTaskPlan` and `TurnTaskOutcome` are typed contracts; task outcomes are instantiated when the current-turn result receipt is derived.

A plan can record `missing_fields`, capability IDs, planned tool names/arguments and tool-call IDs. The plan is internal orchestration state. It is not a wire contract that the MCP service accepts wholesale.

## 4. Step-by-step interpretation and planning

### Step 0 — Receive the customer turn and recover authoritative state

The Agent receives the current customer text, conversation/thread identity, message IDs, application context, and any pending task/workflow state.

Before interpreting a new semantic request, the runtime reconciles existing state and current-turn receipts. A pending transfer clarification, navigation confirmation or other registered continuation must not be lost simply because the latest message is short (for example, “the latter”, “Everyday”, or “yes”). The typed `PendingTaskContext` is the authoritative current pending-task wrapper. It carries a versioned state contract; some nested workflow state remains domain-specific and flexible, so this is not yet a claim that every persisted nested state object has a fully typed migration schema.

Stable conversation references are resolved against the actual message IDs available to the runtime. A reference to a message that does not exist in the allowed conversation is not accepted as a valid source.

### Step 1 — Apply only approved pre-assessment routes

Not every turn needs an LLM assessment. The runtime may handle a narrow, registered case before invoking the semantic interpreter. The current decision policy orders these routes by priority:

| Priority | Route | Purpose |
|---:|---|---|
| 100 | `academy_personal_data` | Schedule a registered personal-data read when a declared Academy topic requires it |
| 95 | `explicit_workspace_navigation` | Resolve a clear request to open a registered workspace |
| 90 | `workspace_knowledge` | Explain a workspace using its maintained catalogue metadata |
| 85 | `academy_general_education` | Answer a general educational question from the Academy catalogue |
| 80 | `connector_result_status` | Preserve a real connector failure as this turn's outcome |
| 75 | `canonical_read_fast_path` | Execute an unambiguous registered read through its OCCF plan |
| 70 | `assistant_capability_guide` | Explain the registered assistant capability catalogue |
| 10 | `simple_social` | Handle a genuinely simple social turn after application resources have been considered |

Existing pending-task/workflow recovery and receipt reconciliation happen as part of the turn lifecycle around these routes. The route policy is data-driven in `shared/contracts/decision_routing.json`; its handlers are a fixed runtime allowlist, not executable handler names supplied by the model.

There is also a narrow explicit-transfer entry route described in Step 8. These exceptions are bounded and documented; they do not turn the runtime into a general regex-driven parser.

### Step 2 — Discover candidate intents and assemble a limited context

When a semantic assessment is needed, the runtime assembles a focused context. It can include:

- the current customer message;
- a limited, role-labelled recent conversation excerpt with stable message IDs;
- prior assistant customer-visible prose where needed to resolve a reference such as “take me there”;
- the current workspace and selected account name;
- the available workspace catalogue and account names/currencies (not account IDs or balances as free-form model authority);
- OCCF-derived intent/capability relationships, task classes and catalogue metadata through the compact decision map;
- bounded candidate-intent hints from the registry's semantic phrases, keywords and deterministic recognition patterns;
- already validated capability results when relevant to the current task.

The decision map is built from the current OCCF registry and Decision Routing Policy. It tells the interpreter what registered intents and resources mean and which task families exist. It does not hand the model raw database records, unrestricted MCP schemas, credentials, customer identifiers, or an executable menu of tools.

The intent-candidate discovery helper is a **shortlisting aid**, not a classifier with authority. Empty or weak candidate hints do not prove the request is out of scope. The LLM still interprets ordinary paraphrases; deterministic patterns are bounded optimizations for known canonical forms.

### Step 3 — Interpret meaning, not execution

The model is asked to describe the customer request as one or more registered tasks. It should extract:

1. the best matching registered `intent_id`;
2. the customer's objective and requested outcome (information, explanation, education, navigation, action or unknown);
3. semantic scope and selectors (for example all accessible accounts versus the selected account, or the latest transaction versus a specifically referenced older item);
4. scalar slots and entities actually supported by the message/context;
5. exact evidence quotations and their declared sources;
6. confidence estimates for the applicable intent, slot and entity dimensions;
7. a provisional view of whether information appears to be missing, or whether the interpreter must abstain;
8. exact prior message IDs for any conversation references it relies on.

The interpreter must preserve independent supported tasks in a compound message. An educational question should not be discarded just because a sibling transfer task lacks a recipient or amount.

It must not choose a capability, invent an argument schema, decide which account or contact record is authoritative, or select a handler. It must not infer financial facts that are not in validated evidence. For semantic fields declared in the intent contract, it should map natural language to one allowed canonical value and provide a field-specific exact customer quotation. It must reason from meaning, not require the customer's wording to match a registered phrase example.

### Step 4 — Parse and validate the assessment

The runtime parses the provider result into the strict Pydantic assessment schema. It rejects unknown fields and invalid types. It validates that:

- the schema version and top-level/task enumerations are allowed;
- each `intent_id` exists in OCCF;
- the task list respects the maximum count;
- slots/entities are scalar strings and are declared by the registered intent where that intent declares its allowed fields;
- semantic input values are constrained to the selected intent's registered `semantic_inputs.allowed_values`;
- conversation references resolve to current allowed message IDs;
- evidence quotes are supported by the source they claim to quote;
- confidence values are within `[0, 1]` and only relevant dimensions are required;
- the task resolution does not conceal missing required information.

Evidence is not accepted merely because the model supplied a plausible quotation. Every quotation must exist in its claimed source. Numeric and financial values continue to require their stricter provenance checks; numeric matching uses token boundaries so a quote supporting `50` does not support `500`. For a semantic field explicitly declared by OCCF, the model performs the open-language interpretation, while the runtime checks that its field-specific customer quote is real and its canonical value is in the contract's allowlist. The runtime does not run a second phrase/regex classifier over those semantic values. Unsupported, undeclared or invalid values are rejected.

Evidence has different weight and purpose:

| Source | What it may support | What it cannot establish by itself |
|---|---|---|
| `current_turn` | Current request, instruction, amount/currency explicitly stated, customer preference | Account ownership, retrieved balance, recipient verification or approval |
| `conversation` | A prior customer instruction or preference still valid in the current workflow | A new authorization or a fact that has gone stale |
| `assistant_context` | A deictic reference to a workspace or item explicitly named by the assistant | Customer intent by itself, financial facts, beneficiary identity, account ownership or approval |
| `application_context` | Validated UI context/catalogue facts such as visible workspace/account names | Financial authorization or proof that an account belongs to an external institution |
| `capability_result` | Facts present in a successfully validated current-turn capability result | Instructions to the model or authority beyond the capability's declared scope |

If interpretation is malformed, abstains, proposes an undeclared value or cannot be compiled into a valid operation, the runtime clarifies or safely falls back to a bounded canonical path where one exists. A task-level `NEEDS_CLARIFICATION` is provisional: it is not an unconditional stop signal. The planner first applies only the defaults declared by the selected OCCF intent and checks the real required semantic fields and capability schema. It proceeds only if that full reconciliation succeeds; otherwise the planner returns the concrete missing/unsupported fields. `ABSTAIN` remains a stop signal.

### Step 5 — Apply confidence policy without confusing confidence with authority

The current confidence configuration defaults to `observe`.

- In `observe`, structure, registry membership, evidence, required fields and operation contracts are still validated. Uncalibrated model scores are recorded but do not block otherwise grounded work.
- In `enforce`, explicitly configured thresholds apply only to relevant dimensions and only when the configuration is valid. Missing/low applicable scores lead to targeted clarification.

Thresholds are intentionally not assigned arbitrary universal values. They must be calibrated by intent/task class and provider against labelled examples before enforce mode is relied upon. The capability dimension is not an interpreter-selected dimension in the current protocol: the runtime derives capability from OCCF. **No confidence value authorizes a banking operation.**

### Step 6 — Derive task class and registered execution plan

For each validated task, the planner resolves the registered intent's `execution_plan` and derives the task class from OCCF metadata. The interpreter does not supply the task class or handler.

Current dispatch classes are:

| Derived task class | Runtime handler | Meaning |
|---|---|---|
| `resource` | `registered_resource` | Read a maintained Academy, workspace or assistant capability catalogue resource |
| `mcp_read` | `occf_read` | Execute a registered authenticated read-only MCP capability |
| `presentation` | `registered_presentation` | Run registered data preflight and render verified result data |
| `workflow` | `registered_workflow` | Continue a registered deterministic workflow from typed state |
| `frontend_action` | `registered_frontend_action` | Invoke an approved browser-side action when current session/AG-UI registration and OCCF preconditions permit it |
| `clarification` | `preserve_and_clarify` | Preserve task identity and valid values; ask for missing/ambiguous information |
| `unsupported` | `explain_supported_scope` | Explain a supported boundary without inventing an operation |

The runtime resolves the capability from the intent plan, including contact-resolution or supporting capabilities, and validates that the capability is registered and eligible for the current execution path. For example, account overview may require a presentation preflight over registered account reads, while an Academy explanation may use a registered content resource without an MCP call.

### Step 7 — Derive required fields and fill capability arguments

This is the central contract-filling step.

1. **Check semantic fields.** The runtime reads `required_slots`, `optional_slots`, `required_entities`, `optional_entities`, `slot_values` and `semantic_inputs` from the registered OCCF intent execution plan. Semantic inputs define their location (`slots` or `entities`), description, allowed canonical values and any explicitly registered default. Unregistered fields and unsupported values are rejected or clarified.
2. **Resolve the capability.** The intent plan identifies its primary capability, supporting capabilities, contact-resolution capability or registered presentation. The model cannot substitute a different tool.
3. **Apply declared semantic defaults.** A default is used only when the corresponding semantic value is absent and OCCF declares `default_policy: when_unspecified`. A customer-provided but unsupported value is never replaced by the default. The model's clarification suggestion is reconciled against the result: complete contract means the task may proceed; a real missing field remains a clarification.
4. **Start with declared plan arguments.** Static registered arguments come from the OCCF intent plan—not the model. For semantic fields that compile to capability inputs, only the intent's registered `argument_mappings` may populate those arguments.
5. **Map extracted values.** Only a slot/entity whose name is an input property in the selected capability schema can be copied directly. A small set of semantic aliases (for example `beneficiary`/`recipient` to a declared `name_query`) is allowed only when the destination property exists in that schema. A few domain-specific mappings (such as workspace alias resolution or account selection against current application context) are implemented in their owning planner path.
6. **Resolve records against trusted results.** A natural-language account or beneficiary name is not an account/contact ID. The runtime uses authenticated MCP results or validated current application context to resolve a unique record. If zero or multiple matches remain, it asks the customer; it does not choose the first result.
7. **Derive API required fields.** The runtime reads `input_schema.required` from the exact OCCF capability. Missing values are recorded as `missing_fields` and produce a targeted clarification rather than a speculative argument.
8. **Validate the complete argument object.** `validate_capability_input` applies the registered JSON Schema, including required properties, types, enums, formats, additional-properties policy and semantic checks. An invalid object does not proceed to execution.

The resulting arguments are not a new schema invented by the LLM. They are a concrete instance of the selected capability's registered input contract.

#### Example: recipient name is not a beneficiary ID

For transfer preparation, the intent's required semantic information includes the amount and beneficiary. The model may extract a recipient name from the message, but this does not make that name a verified saved contact. The runtime first calls the registered `banking.contacts.search` capability using the recipient name. Only the authenticated result can provide the canonical contact ID. A unique match can continue; multiple candidates require a customer choice; no match does not silently fall back to arbitrary text.

The same read capability also serves the separate `SAVED_CONTACT_LOOKUP` intent. A request such as “Find John in my saved contacts” is complete when `contact_name` is grounded in the customer message. The planner maps it to `banking.contacts.search` and returns the verified matching details through the normal grounded response path. It does not create `TRANSFER_PREPARATION` state, ask for an amount or currency, or imply that the customer intends to pay the contact. Capability ownership is not a substitute for intent ownership: the intent determines what the read means.

Similarly, “use my highest-balance account” is a preference, not an account ID. The workflow requests a fresh authenticated account/balance result, filters eligible active accounts by currency, and proceeds only when the comparison identifies one unique source. Ties, absent evidence or incompatible accounts require clarification.

### Step 8 — Execute through the correct owner

Once the task plan and arguments validate, the runtime dispatches to the operation's owning boundary.

#### Bounded explicit-transfer entry path

A bounded deterministic entry route handles explicit forms such as “send/transfer/pay [amount] [currency] to/for [recipient]”. It extracts only explicit draft inputs and starts the **same canonical typed transfer workflow** used by the model-backed Agent path. Educational questions such as “how do I transfer?” or “what if I transfer?” are not treated as transfer commands.

This is a bounded transition helper, not a universal natural-language parser. Its values remain untrusted draft inputs; the workflow still performs authenticated contact resolution, amount/currency checks, source-account resolution and draft preparation. It cannot choose identity, authorize a payment or submit one.

#### Current execution owners

| Task / operation | Owner and contract | Key boundary |
|---|---|---|
| Academy/workspace/assistant resource | Maintained resource catalogue and registered intent metadata | Only registered knowledge and product claims are presented |
| Account, balance, transaction, contact, loan or Financial Health read | Registered read-only MCP capability over Streamable HTTP | Agent and MCP each validate the operation; MCP derives identity and scopes data server-side |
| Account/transaction presentation | OCCF presentation contract with required preflight reads | Renderer receives validated current-turn data; model-authored financial props are replaced |
| Navigation/account selection/filter | Registered frontend capability and current AG-UI browser handler | Browser action is allowlisted presentation state, not banking authority |
| Transfer draft preparation | Runtime-owned typed transfer workflow and registered browser draft action | Opens an editable draft only; does not preview, approve, authenticate or submit a payment |
| Financial preview/confirmation/status | Separate Banking Kernel HTTP API and Kernel operation contracts | `kernel.*` operations are non-agent-callable; server-side policy and customer approval remain mandatory |

Current agent-invocable read-only MCP tools are:

- `list_customer_accounts`
- `get_account_balance(account_id)`
- `get_recent_transactions(limit)`
- `search_contacts(name_query)`
- `get_customer_loan_overview` (illustrative; disabled by default through `DEMO_LOAN_ENABLED=false`)
- `get_customer_financial_health_overview` (recorded leverage methodology; not a bureau credit score)

The browser-side capability set includes `navigate_workspace`, `select_account`, `clear_account_selection` and `prepare_transfer_draft`. The last action is runtime-owned and `model_invocable:false`: the model does not directly create a transfer draft by choosing its tool. The runtime invokes it only after the workflow has resolved and validated the required data and the current UI session exposes the registered handler.

### Step 9 — How the MCP backend accepts the operation

For a read capability, the backend receives a tool-specific call containing only the registered tool name and normalized argument object. It does **not** receive the entire `TurnInterpretationAssessment`, original prompt history or a model-proposed arbitrary execution plan.

The current acceptance path is:

1. Agent Runtime resolves the tool name back to the canonical OCCF MCP capability.
2. Runtime calls `validate_capability_input` against that capability's `input_schema` before sending the request.
3. Runtime invokes the configured MCP Streamable HTTP client with the operation arguments and trace context. The local demo uses an internal static bearer token; this is a development guard, not production customer authentication.
4. MCP authenticates the service request, derives the demo identity/tenant from server configuration, and checks the capability required by the registered declaration. A customer/tenant identity supplied by the model is not authoritative.
5. MCP validates the incoming arguments again against OCCF. It executes the query using server-derived owner/tenant scope and the dedicated read-only PostgreSQL role.
6. MCP validates output data against the registered output schema and constructs the common OCCF result envelope with capability ID, outcome, evidence source/scope/retrieval time/completeness, trace identity, error and trust class.
7. Agent Runtime validates the returned envelope again against the exact capability contract before using it. Invalid data/envelopes become failures, not successful facts.

The double validation is intentional: the producer validates what it emits, and the consumer validates what it accepts. The MCP result is classified as `untrusted_banking_data`—it is trusted as a validated response from the declared capability and scope, but its content remains data, not instructions.

The local MCP PostgreSQL role has `SELECT` privileges on the current read tables, including accounts, transactions, contacts, demo loans and loan installments. It is not the Banking Core application database role, and Agent Runtime does not receive the Banking Core `DATABASE_URL`. Demo identity and static service tokens remain limitations; they are not OIDC, OAuth 2.1 or production identity controls.

### Step 10 — How frontend and Kernel paths differ

**Frontend capability.** The Agent may only dispatch a registered browser operation available in the current AG-UI run and permitted by the OCCF declaration. The browser action validates its input and current application context, runs registered preconditions, invokes the actual handler and returns a result envelope. The Agent validates that receipt. A receipt that is missing, malformed or reports a non-success status is not success. Frontend context is for application consistency; it is not a substitute for authenticated banking data or identity.

**Banking Kernel operation.** The Kernel is deliberately not in the model's callable tool vocabulary. `kernel.transfer.preview`, `kernel.transfer.confirm` and `kernel.transfer.status` are registered as `agent_callable:false` and are exposed through separate API routes. After the customer has reviewed/edited a draft, the customer application requests a preview; the Kernel independently derives identity, checks ownership and policy, persists a preview bound to its payload digest, requires explicit customer confirmation and demo challenge, and records approval/command/audit/outbox state before Temporal and the synthetic ledger adapter perform the demo execution. LLM interpretation, capability confidence, MCP access and a draft-opened browser receipt cannot bypass these steps.

### Step 11 — Correlate results and preserve multi-task outcomes

A customer message may contain multiple tasks. The interpretation contract allows up to five tasks and plans supported tasks independently. Each receives its own task ID, status, missing fields, capability references, tool-call correlation and eventual outcome.

`TurnTaskOutcome` distinguishes `PLANNED`, `SUCCEEDED`, `PARTIAL`, `EMPTY`, `CLARIFICATION_REQUIRED`, `REJECTED`, `FAILED`, `UNAVAILABLE` and `NOT_EXECUTED`. `TurnOutcome` is derived from actual current-turn tool receipts after the latest customer message; old tool results are not carried forward as current success.

A successful read must not be erased because another task needs clarification. Partial, empty, failed, rejected and unavailable results remain distinct. A malformed MCP or browser receipt cannot be upgraded into success merely because the model says it worked.

There is one material workflow limitation: only one serialized pending transfer workflow is active at a time. A second transfer task in the same turn is not durably queued for later execution. This is not yet the multi-transfer queue implied by a fully collaborative workflow system.

### Step 12 — Compose and publish the customer response

The response composer is a separate model phase. It receives the customer request, limited semantic task metadata, validated capability result envelopes and task outcomes. It does not receive executable MCP/frontend tools. Its output is returned through the runtime-only `oraiflow_customer_response` envelope and validated before customer-facing prose is published.

The Agent publishes a versioned `AgentTurnResponse` with stable message identity and the current turn receipt. The UI must reconcile updates using that contract, not infer final success from the presence of a card, message or streaming event. Provider and outer-stream failures must result in a visible terminal outcome when transport remains available.

Current envelopes and outcomes provide capability-level evidence and task-level status, but do not yet bind **every individual customer-specific financial assertion** to its own claim record, evidence reference, scope, currency, freshness and trace. There is also no complete labelled evaluation dataset or repeatable groundedness metric suite. Integrators must not interpret capability-level receipts as claim-level provenance.

## 5. Worked examples

### Example A — “What is the Activity workspace for?”

1. The request is considered against the workspace knowledge pre-assessment route.
2. The runtime resolves the workspace through the registered workspace catalogue.
3. No financial read or executable browser action is needed to explain its purpose.
4. A customer-facing explanation is generated from catalogue metadata; the LLM may phrase it naturally but must not invent unregistered functionality.

This is a resource answer, not a navigation action.

### Example B — “Show my recent transactions.”

1. The canonical read fast path can recognize this unambiguous supported read without an interpretation model call.
2. OCCF intent `RECENT_TRANSACTIONS` maps to `banking.transactions.recent` and its registered `get_recent_transactions` capability, with the declared limit.
3. Agent Runtime validates the normalized arguments, invokes MCP, and validates the returned envelope.
4. If the registered transaction-list presentation is used, the renderer receives the validated current-turn transactions; it cannot use model-generated transaction amounts.
5. The task and turn outcomes are derived from the actual receipt and the response describes only what the result supports.

If the phrasing is ambiguous (for example, it could refer to a specific singular transaction), the semantic interpreter resolves the intent rather than adding a phrase-specific route.

### Example C — “Explain the payment I made most recently.”

1. The interpreter proposes `TRANSACTION_EXPLANATION`, `EXPLANATION`, and the semantic slot `transaction_selector=latest`, with an exact quotation from the customer turn. The model does not choose the MCP tool or its arguments.
2. OCCF declares `latest` as the supported selector and declares `latest` as the default when no selector is supplied. The planner also knows that an unsupported selector such as `first` is not equivalent to `latest`.
3. The registered argument mapping compiles `latest` to the existing recent-transactions capability with `limit: 1`. The backend query sorts by `occurred_at DESC` and scopes the result to the authenticated customer and tenant.
4. If the model incorrectly marks the task `NEEDS_CLARIFICATION`, the planner does not blindly repeat its question. It validates the evidence, applies the registered default and checks the capability schema. A successful check normalizes the validated internal assessment to `READY`; a genuinely missing or unsupported field still returns a clarification.
5. The Agent explains only the validated returned record. The read remains customer-scoped and read-only.

### Example D — “How much did I spend on coffee this month?”

1. The message is handled by the existing semantic assessment path and the interpreter proposes `TRANSACTION_ANALYSIS` with a registered period, outflow direction, sum measure and customer-supplied merchant/description filter.
2. The OCCF intent declares the meaning and allowed values. The model does not choose `banking.transactions.analyze`, SQL fields or executable argument mappings.
3. The existing planner validates evidence and allowed values, applies declared defaults and compiles the query into the registered `banking.transactions.analyze` input schema.
4. MCP checks `transactions:read`, derives customer and tenant from authenticated server identity, then runs parameterized filters and deterministic aggregation against the existing transactions/accounts tables.
5. The result reports the matching count and amount separately for each currency. Merchant matching is against recorded descriptions, not a canonical merchant identity; `TRANSFER` rows are excluded only when their recorded category says `TRANSFER`.
6. The response composer explains the validated result. If no rows match, it says so; it does not invent a spend total. If grouped output is truncated, the response must describe the result as partial.

### Example E — “What is my salary?”

1. The interpreter proposes `TRANSACTION_ANALYSIS` with `analysis_mode=recurring_inflows` and inflow direction; this does not add a salary-specific route or capability.
2. The planner compiles the semantic values into the same registered transaction-analysis operation.
3. MCP screens positive booked credits over the registered 365-day period and groups them by recorded description. A description group is marked `potentially_recurring` only when it has at least three observations across at least three distinct months.
4. The result is a screening signal. It cannot prove employment, payroll, payer identity or a contractual salary. One salary-labelled credit is not enough to assert a recurring salary.

### Example F — “Transfer 50 EUR to Alex.”

1. The narrow explicit-transfer entry route may extract amount, currency and recipient text, then start the canonical transfer state machine. The extracted values are still untrusted.
2. The workflow uses `search_contacts` against the authenticated customer's saved contacts. If Alex is not unique, the Agent asks which returned candidate the customer means.
3. The workflow verifies recipient currency and obtains/validates an eligible source account. If a choice is needed, it asks; it does not silently select the first account.
4. The runtime fills the registered transfer draft action only after the recipient contact ID and source account are resolved and amount/currency are validated.
5. `prepare_transfer_draft` opens an editable browser draft. A successful `DRAFT_OPENED` receipt ends the transfer-preparation task.
6. No preview, approval, SCA, payment command or ledger write has occurred. Those require the separate customer-driven Banking Kernel flow.

## 6. Current implementation and known limitations

The current `v0.7.7` package includes the generic transaction-analysis capability, registry-backed monthly/category semantics, the Academy-vs-analysis route precedence guard, extended synthetic local activity fixtures, current-turn AG-UI presentation/action handling, verified saved-contact summaries and transfer replay protection that recognizes a verified draft receipt even when assistant acknowledgement metadata is stripped during transcript serialization. It preserves the rule that browser draft preparation is separate from Banking Kernel preview, customer confirmation, SCA and execution.

The table below distinguishes what exists in source from the verification or maturity still required before relying on it in a real institution deployment.

| Area | Current implementation | Known limitation / integration requirement |
|---|---|---|
| Interpretation schema | Strict, versioned, interpretation-only assessment with at most five tasks | Semantic quality across paraphrases and configured providers requires measured evaluation |
| Evidence and references | Source-labelled quotes, runtime re-anchoring and exact message-ID validation | Broader adversarial and conflicting-context evaluation is required |
| Required fields | Intent semantic requirements, declared defaults and capability `input_schema.required` are reconciled; model clarification is provisional when a task is selected | Semantic coverage varies by intent and unseen-language completion requires provider-backed evaluation |
| Capability planning | Runtime derives task class/capability and compiles bounded semantic values/defaults from OCCF before capability-schema validation | Complete live integration verification of all task families and false-positive completion measurement are outstanding |
| Task outcomes | Typed task plans/outcomes and current-turn receipt correlation exist | Mixed outcomes, browser recovery and provider failures require deployment-level testing |
| Confidence | Observe mode is the sample default; enforce mode requires explicit thresholds | Confidence thresholds need labelled provider/task-class calibration |
| MCP acceptance | Agent and MCP validate capability arguments/results; identity/capability checks are server-side | Demo identity and static-token model must be replaced before institution use |
| Transfer preparation | Typed, candidate-bounded workflow; editable draft only | Serialized AG-UI message recovery and verified-receipt replay guards have focused regression coverage; live browser/Copilot Runtime verification remains required, and no durable multi-transfer queue exists |
| Grounded response | Validated capability envelopes and task-level outcomes feed the composer | Claim-level provenance, calculation contracts, claim classification and labelled evaluation are not implemented completely |
| Latency/streaming | Completed deterministic/resource responses are progressively emitted; model phases remain buffered until validation | Provider-token streaming and configured first-token/end-to-end performance targets are not established |
| Integrated system | Source-level tests and frontend behavior tests exist | Configured provider, browser/Copilot Runtime/AG-UI, live MCP, PostgreSQL and Temporal integration must be demonstrated in the target deployment |

### 6.1 Interpreting verification evidence

Source-level unit tests, contract checks and component tests provide different evidence from a configured-provider browser journey. A successful helper or isolated service test does not establish that a real customer turn has passed through every service. Validate the complete React/CopilotKit, AG-UI, Agent Runtime, MCP, database and workflow path in the target deployment environment.

## 7. Rules for future changes

1. **Do not ask the LLM to select executable capabilities.** It proposes registered meaning; runtime and OCCF own execution mapping.
2. **Do not send the interpretation assessment to MCP as an execution contract.** Construct the one capability-specific argument object and validate it first.
3. **Do not duplicate capability schemas in prompts, UI code or backend adapters.** Extend the canonical registry and regenerate derived contracts.
4. **Do not make confidence a permission.** Identity, capability authorization, approvals, SCA and Kernel policy remain separate.
5. **Do not promote names into IDs.** Resolve customer/account/contact references against authenticated candidate sets and clarify ambiguity.
6. **Do not add phrase-specific parser branches as a substitute for semantic interpretation.** Restrict deterministic shortcuts to canonical reads, registered catalogue/resource routing and explicitly bounded workflow transitions.
7. **Do not flatten task results.** Preserve each task's identity, status, evidence/receipt references and partial/failed outcomes.
8. **Do not treat an editable draft as a financial command.** Kernel preview and customer confirmation remain independent.
9. **Do not let response composition change operation truth.** It may explain validated results; it cannot turn a failure into success or manufacture a financial fact.
10. **Do not claim complete claim-level groundedness until individual financial assertions have traceable evidence and repeatable evaluation demonstrates that unsupported claims are blocked.**

## 8. Implementation source map

| Responsibility | Current source of implementation |
|---|---|
| Canonical contracts and registry | `shared/contracts/registry.json`, `shared/contracts/registry.py` |
| Decision policy | `shared/contracts/decision_routing.json`, `shared/contracts/decision_routing.py` |
| Interpretation schema and prompt | `services/agent_runtime/task_interpretation.py` |
| Candidate intent hints | `services/agent_runtime/intent_contracts.py` |
| Evidence/confidence validation | `services/agent_runtime/interpretation.py` |
| OCCF-driven task planner and argument filling | `services/agent_runtime/task_planner.py` |
| Decision-map assembly and task classification | `services/agent_runtime/decision_router.py` |
| Agent orchestration and execution adapters | `services/agent_runtime/agent.py` |
| MCP Streamable HTTP client | `services/agent_runtime/mcp_client.py` |
| MCP authentication, input/output validation and queries | `services/mcp/main.py` |
| Result envelope and capability validators | `shared/contracts/registry.py` |
| Task plan/outcome contracts | `shared/task_contract.py` |
| Turn receipt derivation | `shared/turn_contract.py` |
| Agent/UI request and response contracts | `shared/agent_turn_contract.py` |
| Pending task wrapper | `shared/pending_task.py` |
| Transfer workflow and contact resolution | `shared/transfer_workflow.py`, `shared/contact_resolution.py` |
| Banking Kernel route contracts and authority | `shared/contracts/registry.json`, Banking Core API transfer routes/services |

The implementation is authoritative when code and prose disagree. A documentation mismatch should trigger a source check and then a deliberate correction to the owning document; it should not be resolved by adding another prompt rule or parallel contract.


## Current documentation and acceptance ownership

Use [the documentation index](README.md) for the stable product and technical references. The implementation is authoritative when code and prose disagree; verify behavior with the automated tests and the configured deployment path.
