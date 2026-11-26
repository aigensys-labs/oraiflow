# Security Policy

## Scope and support

ORAIflow Core is a synthetic-data reference implementation, not a production banking service. Security fixes for the public source are handled on a best-effort basis. No release is certified for use with real customer data, real funds or production credentials.

## Reporting a vulnerability

Please do not publish exploitable details, credentials, customer information or private certificates in a public issue. Use the repository's private vulnerability-reporting channel. **Before making the repository public, the maintainer must enable and test private vulnerability reporting (or publish an alternative private security contact).** Do not publish this repository until a working private reporting route is available.

When reporting, include the affected version or commit, impact, prerequisites and a minimal reproduction. Redact tokens, personal data and institution-specific details. Allow the maintainer reasonable time to investigate and coordinate a fix before public disclosure.

## Sensitive configuration

Never commit `.env`, API tokens, passwords, private keys, client certificates containing private key material, production data or unredacted logs. Only public CA certificates may be placed in `certs/` for local trust configuration; that directory is ignored by Git. Rotate any secret that may have been committed or disclosed; removing it from the latest commit is not sufficient.
