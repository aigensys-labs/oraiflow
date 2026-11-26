# ORAIflow documentation

**Source package:** `v0.7.8`  
**OCCF registry:** `1.5.17`

This index is the entry point for the stable documentation shipped with the source repository. The root [README](../README.md) introduces the project and local startup.

## Product, architecture and contracts

- [Architecture](ARCHITECTURE.md) — service topology, responsibility boundaries and data flow.
- [Interpretation and Contract Flow](INTERPRETATION-AND-CONTRACT-FLOW.md) — customer message through validation, planning, execution and final response.
- [Interpretation Assessment](INTERPRETATION-ASSESSMENT.md) — model-facing schema, evidence, confidence and semantic limitations.
- [Decision Routing Policy](DECISION-ROUTING-POLICY.md) — bounded pre-assessment routes and their owners.
- [Capability Contracts](CAPABILITY-CONTRACTS.md) — canonical OCCF registry, schemas and capability lifecycle.

## Customer and domain capabilities

- [Transaction Analysis](TRANSACTION-ANALYSIS-CAPABILITY.md) — direction, period, merchant/category/account filters, aggregation and screening limitations.
- [Transfer Workflow](TRANSFER-WORKFLOW.md) — contact and source-account resolution, editable draft and approval boundary.
- [Loan Account](LOAN-ACCOUNT.md) — illustrative liability data and repayment education limits.
- [Financial Health Methodology](FINANCIAL-HEALTH-METHODOLOGY.md) — recorded-position calculations and label limitations.
- [Academy](ACADEMY.md) — learning topics and personal-data boundaries.
- [Workspace Knowledge Catalogue](WORKSPACE-KNOWLEDGE-CATALOGUE.md) — workspace purpose, navigation and application context.
- [Synthetic Demo Data Assumptions](DEMO-DATA-ASSUMPTIONS.md) — sample data coverage and what it does not prove.

## Operations, security and development

- [Zero-to-hero local guide](ZERO-TO-HERO.md) — setup, provider connection, smoke tests and troubleshooting.
- [Configuration](CONFIGURATION.md) — environment variables, service settings and persistence.
- [Security Boundaries](SECURITY-BOUNDARIES.md) — simulated controls, authority boundaries and prerequisites for real integration.
- [Security Policy](../SECURITY.md) — private vulnerability reporting and sensitive configuration rules.
- [Contributing](../CONTRIBUTING.md) — change ownership, validation and pull request expectations.
- [Developer Tools](DEVELOPER-TOOLS.md) — logs, Inspector, timing and diagnostic routes.
- [Test Guide](../tests/README.md) — test commands, database setup and smoke-test data impact.

## Important product boundary

ORAIflow Core v0.7.8 is a self-hosted reference implementation built around synthetic demo identity, records, credentials, SCA and a local ledger. It is not a bank, a regulated payment product or a production-ready financial service. Model interpretation, financial-data authority and consequential command authorization remain separate responsibilities; a component being present in source or a unit test passing does not establish live institutional readiness.
