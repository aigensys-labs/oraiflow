# Transfer preparation workflow — current contract (v0.7.7)

## Scope and authority

This workflow prepares an editable transfer draft only. It does not create a payment preview, approve a payment, complete SCA, submit a command or execute a transfer. The Banking Kernel retains the financial authorization and execution boundary.

The LLM interprets the customer request and its follow-up language. The runtime owns the typed workflow state, validates every transition and constrains all entity selections to trusted candidates or authenticated account data.

## Workflow progression

The runtime preserves a single workflow record containing a workflow ID, action ID, stage, amount, currency, beneficiary query, resolved contact/candidate list, source-account preference and source-account ID.

Typical progression:

```text
CONTACT_SEARCH_PENDING
  → AWAITING_CONTACT (only when multiple saved contacts match)
  → AWAITING_SOURCE_ACCOUNT (when no unique funding account is known)
  → RESOLVING_SOURCE_ACCOUNT (comparative preference requires verified balances)
  → READY_FOR_DRAFT
  → DRAFT_REQUESTED
  → DRAFT_OPENED
  → DISMISSED or COMPLETED
```

The workflow is attached to graph messages and recovered from conversation state on later turns. Existing amount, currency, recipient and account choices are retained during clarification. A new explicit transfer request can establish a new workflow.

## Recipient resolution

The initial `search_contacts` capability is authenticated and customer-scoped. The runtime accepts only active returned candidates. A unique candidate can be resolved directly; multiple matches require selection from the exact set shown to the customer. Exact full-name, unique surname, bank, masked reference or ordinal choices may be used as bounded selection shortcuts. A reply that does not uniquely identify a displayed candidate is not forced into a selection.

A model-proposed contact ID or arbitrary name cannot bypass the MCP candidate set. The draft is revalidated against the current read-only application context before the browser opens it.

## Source-account selection

An explicit account name or unambiguous short alias such as `Everyday` is resolved against the current account catalogue. The resolver never defaults to the first account. An existing UI selection may be used when the workflow is first created, but an acknowledgement such as `ok` is never reinterpreted as that selection during a pending clarification.

Comparative language such as “the highest amount” remains an LLM interpretation. The model proposes the bounded semantic slot `source_account_preference=highest_balance` (or `lowest_balance`) with exact current-turn evidence. The runtime then calls the authenticated `banking.accounts.list` capability. It ranks only currency-compatible account IDs that are also present in the current application catalogue, using `balance_minor` from the verified MCP result. A tie or missing verified balance requires a customer choice.

Balances supplied only by the browser or invented by the model are never used for ranking.

## Draft action lifecycle and idempotency

The runtime, not the LLM, issues `prepare_transfer_draft` after all required values are validated. Every action carries a workflow ID and action ID. The browser maintains an in-session action receipt and treats a repeated open request for the same action as idempotent while the draft is open; dismissed or completed actions cannot be replayed.

The browser returns a `draft_opened` result whose workflow and action IDs must match the pending request. The Agent Runtime acknowledges it and does not feed the result back to the model as a request to recreate the draft. The replay guard validates the browser ToolMessage against its originating frontend tool call and action/workflow IDs; it remains effective when AG-UI/CopilotKit serialization drops custom assistant metadata, and a later same-turn contact-search result cannot replace the transfer acknowledgement. Pending-task and transfer ownership recovery read both native LangChain messages and serialized `role`/`metadata` transcripts. Closing the modal reports `dismissed` for the exact workflow/action pair. A dismissed action is terminal and cannot reopen. An open draft does not block unrelated customer questions or read-only requests.

Draft dismissal is not a payment cancellation because no payment has been submitted. Preview, explicit customer approval, SCA and Kernel command submission remain separate operations governed by their existing contracts.


## Comparative source-account preferences

While the workflow is awaiting a source account, registered comparison aliases such as “highest balance”, “highest amount”, “largest balance” and “most money” may resolve to a `highest_balance` preference; corresponding “lowest/smallest” aliases resolve to `lowest_balance`. These aliases are workflow-slot vocabulary stored in OCCF, not general-purpose phrase routing. The workflow must retrieve current authorized accounts through the MCP account-list capability, filter to active accounts in the transfer currency, require a numeric minor-unit balance, and select only a unique maximum/minimum. A tie or incomplete balance data requires a customer choice. Never rank from frontend context or model-invented balances.


## Combined currency and source-account clarification

When a saved recipient has a verified account currency and the customer omitted currency, the workflow proposes that recorded currency and asks for the funding source in the same clarification. The wording tells the customer that choosing a source account confirms the proposed currency; the customer can instead state a different supported currency. This avoids a redundant standalone currency-confirmation turn.

A source-account name or registered balance preference (for example, `Everyday` or `highest balance`) in the reply is sufficient to confirm the proposed currency because that choice is explicitly requested in the same message. A plain acknowledgement such as `OK` confirms the currency only; the workflow then asks for the still-missing source account. The source selection is always checked against a fresh authenticated MCP account result before a draft can open.

The runtime-owned contact-search and account-read tool-call messages include truthful progress text; they must not emit empty assistant messages that the UI could display as `None`. A successful `DRAFT_OPENED` receipt is represented as a completed `PendingTaskContext`, not an active clarification. The draft-opened acknowledgement is terminal for that preparation task; a later customer message starts a new task and is not followed by another transfer clarification.

## Browser receipt finalization and one-shot termination

A successful `prepare_transfer_draft` browser result is an execution receipt, not
itself the customer's final response. The Agent Runtime must first validate the
receipt against the originating tool call, workflow ID and action ID, then emit
one durable assistant acknowledgement. Only that assistant acknowledgement is
a terminal signal for AG-UI/LangGraph replay guards.

The replay guard must not stop on the raw browser `ToolMessage`: doing so skips
the acknowledgement step, can leave the chat turn unresolved, and may allow a
later continuation to reinterpret the original transfer request. Both the
model-backed graph and deterministic demo AG-UI adapter follow the same order:

1. Receive the browser `ToolMessage` for `prepare_transfer_draft`.
2. Validate its result envelope and correlate workflow/action identifiers.
3. Emit the customer-facing draft-opened acknowledgement with a stable action ID.
4. Stop further graph/model interpretation for that user turn.

A later user message is a new turn and is not blocked by the previous
acknowledgement. Closing a draft does not authorize or submit a transfer.

