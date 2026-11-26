# Workspace Knowledge Catalogue 1.0.0

## Contract ownership

Workspace identity, purpose, customer value, features, limitations and navigation aliases are declared in `shared/contracts/registry.json` under `application_policy.workspaces` and `application_policy.navigation`.

The OCCF registry is the source of truth. The frontend receives generated contract data through `apps/web/src/contracts.js`; `PageIntro` reads the same `WORKSPACE_CATALOGUE` used to describe workspace purpose/value. The Agent Runtime builds its system context and response validation from `workspace_catalogue()` in `services/agent_runtime/scope_policy.py`.

This avoids separate UI help text and agent-specific copies drifting apart. Add or amend workspace descriptions in the registry and its validation, not as another prompt-specific branch.

## Explanation versus navigation

These are different outcomes:

- **Explain:** “What is the Payments tab?” returns the registered purpose, value, features and limitations. It does not navigate.
- **Explore:** “What can I do in Payments?” explains the registered capabilities and restrictions.
- **Navigate:** “Take me to Payments” invokes the registered frontend navigation capability and requires a successful browser receipt before the agent claims the workspace opened.

A read/list request uses a registered data capability only when the page contract describes that supported data operation. A generic capability-help answer must not replace a page-specific explanation.

## Registered workspaces

The current catalogue contains Overview, Accounts, Payments, Loans, Cards, Contacts, Activity, Academy, AI assistant and Controls & audit. Each entry has a purpose, customer value, feature list and limitations.

Loans are separate from Payments and deposit Accounts. A loan is a liability, never spendable balance or a transfer source. The Overview can surface its summary, while the dedicated Loans workspace contains its illustrative terms/schedule.

## Validation requirements

`shared/contracts/registry.py` verifies that:

- Every allowed navigation page has exactly one workspace catalogue entry.
- Aliases resolve only to registered pages.
- Every workspace has purpose, value, features and limitations.
- Catalogue versions are present.

Tests must verify both agent explanations and frontend usage against the same registry metadata.
