# Loan account and repayment education

## Product boundary

Loans is a dedicated workspace, separate from deposit Accounts. Illustrative demo-loan data is **disabled by default**. When explicitly enabled with `DEMO_LOAN_ENABLED=true`, the demo record is seeded once per server-derived customer/tenant identity and is a synthetic illustrative liability, not a deposit account, credit offer, approved loan, or evidence of an actual disbursement. It is stored in `demo_loans` and `demo_loan_installments`, never in the `accounts` table. It is therefore excluded from available-balance totals and cannot be selected as a transfer source. Existing illustrative rows are preserved when the feature is disabled, but are hidden from customer Loans API/MCP reads and excluded from Financial Health calculations.

## Default illustrative terms

| Term | Default |
|---|---:|
| Principal | EUR 5,000.00 |
| Illustrative APR assumption | 6.90% |
| Term | 24 months |
| Equal monthly instalment | EUR 223.64 (last payment adjusted for rounding) |
| Total illustrative interest | EUR 367.27 |
| Total illustrative repayment | EUR 5,367.27 |

The schedule is generated with decimal arithmetic and rounded to euro minor units. The final principal component is adjusted so the amortization schedule sums exactly to the original principal. Enable the illustrative data first with `DEMO_LOAN_ENABLED=true`. Configure its terms using `DEMO_LOAN_PRINCIPAL_MINOR`, `DEMO_LOAN_APR_BPS` and `DEMO_LOAN_TERM_MONTHS`. A zero APR is supported. The record is seeded once: changing these variables after `loan-demo-001` already exists does not silently overwrite the stored schedule; use an explicit controlled demo-data reset/update if you need a different scenario. The default is not a recommendation or a market quote.

## Read paths

- `GET /api/loans` returns loan records scoped to the authenticated identity and protected by `loans:read` for the dedicated Loans workspace. With the illustrative feature disabled, it returns an empty loan collection unless a supported non-demo loan source is added.
- MCP capability `banking.loans.overview` / `get_customer_loan_overview` exposes authenticated loan terms and instalments for agent explanations. It returns a valid empty result when no loan record is available, is read-only and has no customer-provided identity input.
- Both paths distinguish principal, interest, instalment, dates, `status=ILLUSTRATIVE`, `demo_only=true` and `payment_tracking=NOT_TRACKED`.

## What the agent can do

The agent may explain principal versus interest, amortization, instalments, total illustrative repayment, and why actual terms depend on the customer's agreement. It can compare the schedule values transparently. It must explain that dates are illustrative and cannot assert that an instalment is due, paid, late or overdue. General financial-literacy help may discuss keeping enough liquid money for essentials, the effect of fees and the need to read the loan agreement.

## What the agent cannot do

There is no loan origination, credit approval, disbursement, term modification, repayment initiation, live payment-status tracking, or reminder service. A calendar date passing does not change the installment status. The loan principal is not cash available to spend. Do not use a loan balance as evidence of deposit funds or transfer eligibility.

## Extending to a real loan product

A real institution integration must replace the synthetic tables with an authenticated loan-information adapter and verified statements/schedules. Payment-status events, reminders, early-settlement quotes, fee rules, and repayment initiation require distinct contracts, provenance/freshness, and institution/Kernel authorization. Do not silently re-label the demo schedule as a live product.
