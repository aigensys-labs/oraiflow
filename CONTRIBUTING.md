# Contributing to ORAIflow Core

Thank you for helping improve ORAIflow Core. 

Contributions should preserve its central authority boundary: the model interprets customer intent, OCCF declares permitted operations, MCP exposes authenticated capabilities, the Banking Kernel authorizes consequential commands, Temporal orchestrates durable work, and the ledger remains the financial source of truth.

## Reporting a problem or proposing a change

Use the [GitHub Issue Tracker](https://github.com/aigensys-labs/oraiflow/issues) chooser and select the form that best matches the report:

- 🐛 Bug report for a reproducible application or service defect.
- 🧭 Agent behavior / conversation regression for interpretation, tool-selection, workflow or response-grounding problems.
- ✨ Feature request for a proposed capability or improvement.
- 📚 Documentation improvement for unclear or missing guidance.
- 🐳 Demo setup / Docker help for local installation and startup problems.

**Include**: Python version, ORAIflow version, minimal reproducible example

When opening a pull request, use the repository's pull request template. Security vulnerabilities must not be reported in public issues; follow [SECURITY.md](SECURITY.md) and use the repository's private vulnerability-reporting channel when enabled. Never include real financial records, secrets or unredacted logs in an issue or pull request.

## Before proposing a change

1. Read the [architecture](docs/ARCHITECTURE.md), [capability contracts](docs/CAPABILITY-CONTRACTS.md) and [security boundaries](docs/SECURITY-BOUNDARIES.md).
2. Check the [test guide](tests/README.md) and relevant architecture/capability documentation before changing an existing owner or contract.
3. Prefer extending an existing contract and owner over adding a parallel router, schema, data provider, response path or phrase-specific branch.
4. Keep test data synthetic. Never include secrets, real customer records or unredacted logs.

## Validation

Run the relevant checks from the repository root:

```bash
uv lock
uv sync --locked --group dev
uv run --locked python scripts/generate_contracts.py --check
uv run --locked pytest -q
(cd apps/web && npm install --no-audit --no-fund && npm test && npm run build)
(cd services/copilot_runtime && npm install --no-audit --no-fund && node --check server.js)
```

For changes that touch Compose, run `docker compose config -q` after creating a local `.env` from `.env.example`. For changes to a browser journey, include a real browser/AG-UI acceptance result; unit tests alone do not establish end-to-end behavior. The transfer smoke test writes a synthetic EUR 1.00 debit and must only be run against disposable demo data.

## Pull request expectations

- Explain the customer-visible behavior and the source-of-truth owner changed.
- Add or update tests for success, empty, partial and failure outcomes as applicable.
- Include the relevant provider/browser/MCP/database/workflow integration evidence, or state clearly which evidence is unavailable.
- Update the canonical registry and regenerate derived contracts when a capability contract changes.
- Update stable product, operator or contributor documentation when behavior or configuration changes; remove obsolete guidance rather than creating duplicate runbooks.
- Describe migration, rollback and data impact. Do not claim production readiness based on source-level tests.

> [!IMPORTANT]
> **Scope:** PRs should contain only changes related to the stated issue or feature.
> PRs mixing unrelated changes may be closed without review.

## Code Style

- **Formatter**: [Black](https://black.readthedocs.io/)
- **Docstrings**: Google/Numpy style
- **Type hints**: Required on all public methods

## Licensing

"AIGENSYS" means **AIGENSYS s.r.o.**, a limited liability company
organized and existing under the laws of the Czech Republic, 
with its registered office at Bilkova 855/19, Prague 1, Czech Republic,
Company VAT ID Number: **CZ23450924**.

By submitting a contribution to this project, you agree that:

1. Your contribution is licensed under the project's
   [FSL-1.1-Apache-2.0](LICENSE) terms and becomes part of the Software.

2. You grant AIGENSYS a perpetual, irrevocable, worldwide,
   royalty-free license to use, modify, reproduce, and distribute
   your contribution in the Software or any other product or service
   that AIGENSYS offers.

3. To the extent your contribution would necessarily infringe any
   patents you own or control, you grant AIGENSYS a perpetual,
   irrevocable, worldwide, royalty-free license under those patents
   for the same purposes.

4. You represent and warrant that you have the right to grant the
   licenses above and that your contribution does not infringe any
   third-party intellectual property rights.

## Public documentation policy

The public repository contains stable product, architecture, configuration, security, operations, testing and contributor documentation. Maintainer-only roadmaps, chronological development changelogs, release-validation evidence, internal acceptance matrices, trace-specific debug runbooks, product-positioning briefs and demo storyboards are distributed separately from the source repository. Do not add links to those private/maintainer materials from public documentation. For a release, keep the public source archive and companion internal-document archive at the same version.

## Python style and documentation

`pyproject.toml` is the authoritative location for Python project metadata, runtime/development dependencies, pytest settings, Black formatting and Ruff lint configuration. Black is the only Python formatter; do not combine it with competing auto-formatters. Ruff provides correctness-focused linting. Public production modules, classes and functions must have concise Google-convention docstrings that explain intent and important authority or side-effect boundaries. Add `Args`, `Returns` and `Raises` sections where they materially clarify a non-trivial interface. Do not write docstrings that merely repeat a function name.

Run `make format` before submitting Python changes. Run `make quality` to check Black formatting across the complete Python tree, Ruff rules and production docstring coverage. CI checks Black formatting for Python files changed by the push or pull request; Ruff, Python syntax compilation, documentation coverage and tests run across the repository. The existing v0.7.x tree has not yet had a verified full Black normalization pass, so do not claim the entire legacy tree is Black-clean until `make format` and `make quality` both pass. ORAIflow is a Docker-first demo application: Python/uv are developer tools, not prerequisites for running the public demo. Do not add a separate dependency list to a `requirements.txt` file.


## Other Ways to Contribute

- Fix or improve documentation
- Share feedback on issues or PRs
- Write a blog post or tutorial using ORAIflow and let us know

## Questions?

Open a [Discussion](https://github.com/aigensys-labs/oraiflow/discussions)
or reach out via the issue tracker.