# ORAIflow Academy 1.0.0

## Experience

Academy is a dedicated Workspace entry containing the existing ORAI assistant conversation surface and a set of guided financial-literacy topics. It reuses the supported Agent Runtime; it is not a second independent agent or an alternate path to banking authority.

Topic definitions are declared in the OCCF registry under `application_policy.academy_topics`. The UI renders cards from generated `ACADEMY_TOPICS` metadata, and the Agent Runtime uses the same catalogue for topic IDs, relevance metadata, learning points and limitations.

## Initial learning paths

1. **Understand your Financial Health badge** — recorded assets, liabilities, equity, leverage and what the indicator includes/excludes.
2. **Borrowing and leverage** — how borrowing changes liabilities, equity and flexibility.
3. **Credit scores and credit reports** — distinction between a bureau score/report and ORAIflow's recorded leverage indicator.
4. **Saving with a purpose** — emergency reserves, liquidity, account access and interest-crediting terms.
5. **Loan repayments and APR** — principal, interest, instalments, amortization and total repayment using recorded/illustrative loan terms.
6. **Budgeting and cash flow** — income, expenses and affordability concepts.

## Personal data and advice boundary

General financial education can be provided without personal balance claims. When a topic asks about the customer's current badge, recorded leverage or customer-specific loan terms, the agent must retrieve the corresponding authenticated read-only capability. It must not invent rates, amounts, repayment status or credit-score outcomes.

Academy content is educational information, not personalized regulated financial, legal or tax advice. Credit-score systems and consumer-credit rights depend on jurisdiction and provider. ORAIflow does not retrieve bureau reports or guarantee a credit-score change.

The agent should be friendly, non-judgmental and practical. It may explain how the arithmetic changes, but must not encourage customers to drain emergency reserves, take credit to manipulate a badge, or treat a heuristic as an instruction.


## Response and retrieval behavior

General questions comparing credit scores with the ORAIflow badge are educational. They must be distinguished from requests to retrieve a personal financial snapshot. The CREDIT_SCORE_BASICS catalogue entry supplies the explanation and practical habits; it does not trigger a personal financial snapshot read merely because the customer mentions “my badge” in a conceptual comparison.

When a question asks how a new loan or another personal borrowing decision affects the customer's recorded badge, the topic metadata maps to the registered `FINANCIAL_HEALTH_EXPLANATION` intent. The Agent Runtime schedules `banking.financial_health.overview` through the normal OCCF/MCP read path before producing the personalized explanation, at most once in the current turn. It must not stop after telling the customer that a read is required.


## Current transaction-analysis connection

Academy explanations may describe spending, income and cash-flow concepts generally. When a customer asks for their own spending or inflow figures, the Agent must use the registered `TRANSACTION_ANALYSIS` intent and authenticated `banking.transactions.analyze` capability. The capability's available history, recorded descriptions/categories and recurring-inflow rules define what can be stated; Academy content must not invent customer-specific amounts or classify a single credit as salary. See [Transaction Analysis](TRANSACTION-ANALYSIS-CAPABILITY.md).
