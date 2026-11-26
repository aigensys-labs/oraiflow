# Transaction analysis — OCCF capability

**Current package:** `v0.7.8`  
**OCCF registry:** `1.5.17`  
**Intent contract:** `TRANSACTION_ANALYSIS` `1.2.0`  
**Capability contract:** `banking.transactions.analyze` `1.1.0`

## Purpose and ownership

`TRANSACTION_ANALYSIS` adds read-only analysis to the existing ORAIflow interpretation → OCCF planner → MCP path. It is not a separate analytics service, query engine, router or interpretation layer.

The model interprets the customer's analytical objective and proposes semantic fields. The deterministic planner validates those values against the registered OCCF contract, recovers omitted fields only from registered current-turn evidence, applies declared defaults and compiles the operation. MCP derives customer and tenant identity from authenticated server context, checks `transactions:read`, applies parameterized filters and computes per-currency aggregates. The final customer response is formatted from the validated capability result.

| Owner | Contract / component |
|---|---|
| Intent | `TRANSACTION_ANALYSIS` (`agent_assisted`) |
| Capability | `banking.transactions.analyze` |
| MCP tool | `analyze_customer_transactions` |
| Permission | `transactions:read` |
| Effect | Read-only and idempotent |
| Result mode | Grounded explanation from the validated result envelope |

The existing recent-transaction list/detail capabilities and presentations remain separate. Analysis is one generic capability, not a phrase-specific route for each question.

## Semantic inputs

| Field | Meaning | Registered behavior |
|---|---|---|
| `period` | Date range | `last_7_days`, `last_30_days`, `last_90_days`, `last_365_days`, `this_month`, `last_month`, `year_to_date`, `all_available`; ordinary activity defaults to 30 days, recurring-inflow screening to 365 days, and an unqualified month-by-month request to 90 days |
| `flow` | Direction | `all`, `inflows`, `outflows`; spending, purchases, costs and expenses resolve to outflows; received money/pay/income resolve to inflows |
| `measure` | Statistic | `sum`, `average`, `count`, `transactions`; defaults to sum |
| `group_by` | Breakdown | `none`, `merchant`, `category`, `account`, `month`; defaults to none except recurring-inflow screening defaults to recorded-description grouping |
| `analysis_mode` | Analysis purpose | `activity` or `recurring_inflows`; recurring-inflow wording invokes screening, not proof of salary |
| `transfer_scope` | Categorized transfers | Exclude rows with category `TRANSFER` by default; include only when expressly requested |
| `merchant_query` | Merchant/payer/description phrase | Literal, parameterized substring search against the recorded transaction description; not a canonical merchant identity |
| `category_query` | Spending category | Literal, parameterized substring search against the recorded transaction category; not a general-purpose classifier |
| `account_name_query` | Account name | Literal substring search within accounts owned by the authenticated customer and tenant |

The registry declares category terms separately from merchant descriptions. Thus “transport” is a `category_query`, not a description/merchant query. Merchant and category filters are echoed in the validated result metadata so the deterministic response can name the filter the customer asked about, including for a zero-match result.

## Month-by-month analysis

Phrases such as **“by month,” “by months,” “each month,” “every month,” “monthly,” “month by month”** and **“month-by-month”** select `group_by=month`. If the customer does not specify a date range, the registered conditional default selects `period=last_90_days` so the result can compare multiple calendar months. An explicitly requested date range takes precedence.

The capability computes a total for the selected window and separate monthly aggregates by currency. Monthly groups are returned in chronological order using the customer's configured business timezone. A month-by-month question should therefore produce a total plus a readable breakdown; it must not be reduced to a single ungrouped amount.

Month grouping is a deterministic SQL aggregation over `occurred_at`. It does not extrapolate a monthly average or forecast future spending. A partial first or last calendar month must be understood as a partial period, not a complete month's spending.

## Query and result guarantees

- Customer and tenant scope come only from server-side MCP identity; no customer ID or tenant ID is accepted as a model/browser argument.
- The MCP boundary checks `transactions:read`. The dedicated MCP database role has `SELECT` privileges on the transaction/account tables and no banking write authority.
- Filters are bound SQL parameters. Grouping expressions are selected from a static allowlist; customer/model text is never interpolated as SQL syntax.
- Date boundaries are calculated in `ORAI_BUSINESS_TIMEZONE` (sample default `Europe/Prague`). End timestamps are exclusive and current periods stop at the current customer-local date.
- Inflows are positive booked credits. Outflows are negative booked debits, reported as positive spend magnitudes. `all` preserves signed net movement.
- Aggregates remain separate per currency. There is no implicit FX conversion or cross-currency grand total.
- The default transfer exclusion covers recorded category `TRANSFER`. It cannot identify an uncategorized or misclassified own-account movement.
- Merchant grouping uses recorded descriptions; it does not establish a normalized merchant identity. Category filtering uses recorded category text; it does not classify an uncategorized transaction from its description.
- Pending, failed, reversed, cancelled and declined records are excluded.
- Summary metrics include count, sum and mean. Grouped output is capped at 100 rows. Truncation produces a `PARTIAL` outcome and `truncated=true`. A successful zero-match analysis remains a successful query with count zero and empty aggregate/group arrays.
- Row-level records are returned only for `measure=transactions`, within the validated limit of 1–100.

## Synthetic local activity

The local Compose demo can opt into an additional 28 stable-ID synthetic activity rows, bringing the intended base fixture from seven to 35 booked transactions over approximately 63 days. This gives the UX sample multiple months of coffee, transport and other categories. The migration is controlled by `DEMO_TRANSACTION_HISTORY_ENABLED`, defaults on only in local Compose and is disabled by default elsewhere. It is idempotent, but the additional analytics rows **do not modify or reconcile account balance snapshots**. See [Synthetic Demo Data Assumptions](DEMO-DATA-ASSUMPTIONS.md).

These records improve the breadth of local analytical demonstrations; they are not real financial history, do not validate real customer data completeness and must not be used for balance reconciliation or production financial conclusions.

## Supported customer examples

- “Did I receive any money in the last 30 days?” → 30-day inflow sum.
- “How much did I spend on coffee this month?” → this-month outflow sum filtered by recorded description `coffee`.
- “How much do I spend on coffee each month?” → 90-day outflow sum filtered by `coffee`, grouped by calendar month unless a different range is specified.
- “How much do I spend on coffee by months?” → the same monthly comparison; plural wording must not leak into the merchant filter.
- “Do I have any spendings on transport?” → outflow analysis filtered by recorded category `TRANSPORT`, not a merchant-description filter.
- “Show my average spending by category.” → outflow average grouped by recorded category.
- “Show incoming payments by month.” → inflows grouped chronologically by month.
- “How much did I spend from my Everyday account?” → account-name filter resolved only within authenticated customer scope.

These examples illustrate registered semantics; they are not an execution whitelist. Semantic discovery only surfaces a candidate. The model-backed path must still interpret the turn and the planner must validate the task before any capability runs. Strong transaction-analysis wording must not be preempted by weak Academy topic overlap; genuine general questions such as “Please explain budgeting and cash flow” remain Academy education.

## Acceptance and limitations

Automated regressions cover registered candidate discovery, Academy-route precedence, month-grouping/default-period compilation, category-vs-merchant extraction, authenticated owner/tenant filtering, parameterized SQL, chronological monthly grouping, per-currency aggregation, result metadata, customer-visible filter labels, empty results and partial outcomes. These are source/unit checks. The MCP test module uses a minimal SDK shim where necessary and does not replace live MCP/PostgreSQL acceptance.

The deterministic `LLM_PROVIDER=demo` path is not a semantic-quality test. Deployments should validate the configured model provider through the primary browser/CopilotKit/AG-UI path, Agent Runtime, MCP and PostgreSQL. The 35-row synthetic fixture is intended to exercise multi-month analytical UX; it does not establish comprehensive merchant identity, recurring-payment intelligence or a complete analytics product.
