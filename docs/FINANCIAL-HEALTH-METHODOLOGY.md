# ORAIflow Financial Health indicator — methodology 1.1.0

## Purpose and limitations

The Overview banner shows an educational indicator calculated from data recorded in the authenticated ORAIflow environment. It is not a credit score, creditworthiness decision, regulated financial-health assessment, or complete household balance sheet. It must never be represented as one.

The indicator includes only:

- Active deposit-account balances recorded in ORAIflow.
- Outstanding principal from registered ORAIflow loan records included in the snapshot.
- Negative active deposit balances, treated as overdraft liabilities.

It excludes property, external bank accounts, investments, pensions, unrecorded debts, income and monthly debt-service obligations. Currencies are calculated independently; ORAIflow does not net EUR against USD or other currencies.

## Recorded measures

For each currency:

- **Recorded deposit assets** = sum of non-negative balances in active ORAIflow deposit accounts.
- **Loan liabilities** = sum of outstanding principal in included loan records.
- **Overdraft liabilities** = absolute value of negative active deposit balances.
- **Total recorded liabilities** = loan liabilities + overdraft liabilities.
- **Recorded equity** = recorded deposit assets − total recorded liabilities.
- **Loans / recorded equity** = loan liabilities ÷ recorded equity. It is unavailable where recorded equity is zero or negative.
- **Total liabilities / recorded deposit assets** = total recorded liabilities ÷ recorded deposit assets. It is unavailable when recorded deposit assets are zero.

The banner shows Loans / recorded equity as the primary ratio and Total liabilities / recorded deposits as a secondary measure. The human-readable status label is based on total liabilities / recorded deposit assets, not on the loan-to-equity ratio. This distinction is displayed in the UI so the badge is not misleading.

## Status bands and customer-facing labels

The underlying ratio calculations and thresholds are unchanged. The current interface and Academy explanation map internal statuses to concise customer-facing labels:

| Total recorded liabilities / recorded deposit assets | Internal status | Current label |
|---:|---|---|
| Below 25% | `LOW_LEVERAGE` | Excellent |
| 25% to below 50% | `MODERATE_LEVERAGE` | Good |
| 50% to below 75% | `ELEVATED_LEVERAGE` | Needs attention |
| 75% to below 100% | `HIGH_LEVERAGE` | Warning |
| Exactly 100% | `LIABILITIES_MATCH_ASSETS` | Warning — liabilities match recorded assets |
| Above 100% | `LIABILITIES_EXCEED_ASSETS` | Warning — liabilities exceed recorded assets |
| Assets unavailable or zero | `INSUFFICIENT_DATA` | Not enough recorded data |

These are product-interface labels attached to a narrow recorded-leverage heuristic, not a bureau score, lender rating, validated prediction or comprehensive judgement of a person's financial health. The lower bands remain ORAIflow heuristics; the words “Excellent” and “Good” are presentation language, not statistically calibrated customer grades. The 100% relationship has a direct arithmetic interpretation only within the included recorded dataset.

A single snapshot cannot establish that a customer's position is “improving” or “worsening”. Trend language requires comparable historical snapshots and a separately defined trend method. Do not infer a trend from the current status alone.

## How the agent should explain changes

The agent should explain the direction of the arithmetic without judgement or guarantees:

- Paying down debt from genuine surplus reduces a liability, but also reduces the deposit asset used to make the repayment. The result must be recalculated; the badge is not guaranteed to improve by the amount paid.
- Building savings over time may increase recorded assets, provided the funds remain in accounts included in the snapshot.
- Moving money between a customer's own recorded deposit accounts does not change consolidated assets or leverage.
- Taking on another loan increases recorded liabilities and may increase leverage; the purpose, affordability and repayment terms matter as well as the ratio.
- Drawing down recorded savings can worsen the recorded ratio if liabilities remain unchanged.
- Do not recommend draining emergency reserves, borrowing to manipulate the badge, or taking an action solely to reach a lower displayed percentage.

For the customer's own current ratio, the agent must retrieve the authenticated `banking.financial_health.overview` capability. It may not infer the ratio from a remembered balance, a prompt, an example, or a frontend display value.

## Why debt-service-to-income is not shown yet

Debt-service-to-income (DTI) is a different measure: monthly debt payments divided by gross monthly income. The CFPB explains the formula and notes that lender limits vary by product and institution. ORAIflow does not currently have a complete, verified view of gross income and all monthly debt obligations, so it must not calculate or imply a complete DTI figure.

## References

- European Central Bank, *The Household Finance and Consumption Survey: results from the 2021 wave*, Financial Stability / household debt indicators: https://www.ecb.europa.eu/pub/pdf/scpsps/ecb.sps46~3563bc9f03.en.pdf
- Consumer Financial Protection Bureau, *What is a debt-to-income ratio?*: https://www.consumerfinance.gov/ask-cfpb/what-is-a-debt-to-income-ratio-en-1791/
- Statistics Canada, *Household debt-to-net-worth ratio*: https://www150.statcan.gc.ca/n1/pub/13-606-g/2016001/article/14621-eng.htm

The sources above provide definitions and population-level context. They do not endorse ORAIflow's lower heuristic bands or constitute personalized advice.
