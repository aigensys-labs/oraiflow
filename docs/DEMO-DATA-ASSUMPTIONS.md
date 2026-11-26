# Synthetic demo activity — data assumptions and limits

> **DEMO ONLY — ALL DATA IS SYNTHETIC.** This project is a simulation and educational reference framework, not a bank or financial adviser. Every identity, account, balance, transaction, contact, loan and payment flow described here is artificial. No live institution or payment rail is connected. Outputs are not financial, investment, tax or legal advice and must not be used for real financial decisions or transactions. Never enter real personal or financial data. See the root [NOTICE](../NOTICE.md).

## Purpose

The local demo needs enough historical activity to exercise the transaction-analysis experience: merchant filters, category filters, period selection and month-by-month aggregation. The fixture is synthetic test data. It is not evidence about a real person, a market benchmark, or a typical income or spending pattern.

## What the local demo seeds

A fresh database starts with the seven booked activity rows in `db/init/001-schema.sql`. When `DEMO_TRANSACTION_HISTORY_ENABLED=true`, the additive runtime migration inserts 28 more booked rows with stable IDs (`txn-demo-008` through `txn-demo-035`). The intended fixture therefore contains **35 booked transactions** over approximately **63 days**, spanning at least two months and usually three calendar months.

The extended records deliberately include:

- repeated coffee purchases across separate months;
- several transport-category purchases, including transit and fuel descriptions;
- grocery, dining, entertainment, shopping, health, subscription, travel and bill examples;
- two additional salary-labelled credits, plus the original seeded salary-labelled credit;
- a paired, categorized transfer example that is excluded from analysis by default.

Amounts and descriptions are illustrative. The default salary-labelled credit is configured by `DEMO_SALARY_AMOUNT_MINOR` and `DEMO_SALARY_EMPLOYER`; it is not verified payroll, proof of employment or a salary benchmark. Repeated credits may support a *potentially recurring* screening result, but never prove salary or payer identity.

## Migration and persistence behavior

- `DEMO_TRANSACTION_HISTORY_ENABLED` defaults to `true` in the local Docker Compose demo and `false` when not configured. Set it to `false` for any environment that is not an isolated synthetic demo.
- Existing persistent PostgreSQL volumes receive the additional rows when the runtime schema migration runs. There is no need to delete `postgres-data` just to add this history.
- IDs are stable and inserts use `ON CONFLICT DO NOTHING`. Restarting services does not shift timestamps or insert duplicate copies of the same fixture rows.
- The 28 additional records are analytics fixtures. **They do not change account balance snapshots and are not a reconciled ledger migration.** Do not use the resulting activity/balance combination to demonstrate statement reconciliation, available-funds calculations or ledger integrity.
- The demo seed is scoped to the configured demo user and the sample account IDs. It must not be enabled against real customer data.

## What this fixture can and cannot demonstrate

It can demonstrate that the registered analysis capability filters and groups a modest amount of historical activity over multiple calendar months. It can exercise the customer path for questions such as:

- “How much do I spend on coffee each month?”
- “Do I have any spending on transport?”
- “Show my spending by category over the last 90 days.”

It cannot establish realistic spending behavior, meaningful statistical trends, comprehensive transaction coverage, a normalized merchant identity, verified payroll, an exhaustive transaction taxonomy, or the completeness of a real institution's history. A 63-day fixture is a UX and integration sample, not a financial-model validation dataset.

The analysis capability must always report only rows actually present in the authenticated query result. It must preserve date range, currency, direction, filters, empty/partial outcomes and transfer-category limitations. It must not infer that missing records do not exist outside the queried history.
