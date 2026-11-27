import json
import re
from pathlib import Path

from shared.version import __version__

ROOT = Path(__file__).resolve().parents[1]


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_python_and_javascript_packages_share_the_release_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    assert _json("apps/web/package.json")["version"] == __version__
    assert _json("services/copilot_runtime/package.json")["version"] == __version__
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "ORAIflow — local/self-hosted synthetic demo configuration" in env_example


def test_python_services_use_the_canonical_version_constant():
    api = (ROOT / "services/api/main.py").read_text(encoding="utf-8")
    agent = (ROOT / "services/agent_runtime/main.py").read_text(encoding="utf-8")
    mcp = (ROOT / "services/mcp/main.py").read_text(encoding="utf-8")

    assert "from shared.version import __version__" in api
    assert "from shared.version import __version__" in agent
    assert "from shared.version import __version__" in mcp
    assert "version=__version__" in api
    assert '"version": __version__' in api
    assert "version=__version__" in agent
    assert '"version": __version__' in agent
    assert "version=__version__" in mcp


def test_runtime_and_ui_read_version_from_package_metadata_and_share_the_footer():
    runtime = (ROOT / "services/copilot_runtime/server.js").read_text(encoding="utf-8")
    ui = (ROOT / "apps/web/src/main.jsx").read_text(encoding="utf-8")

    assert "createRequire(import.meta.url)" in runtime
    assert "require('./package.json')" in runtime
    assert "v${version}" in runtime
    assert "import packageMetadata from '../package.json'" in ui
    assert "Build {packageMetadata.version}" in ui
    assert ui.count("function WorkspaceFooter(") == 1
    assert ui.count("<WorkspaceFooter") == 2


def test_local_temporal_dev_image_is_not_a_mutable_latest_tag():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "image: temporalio/temporal:1.10.1" in compose
    assert "image: temporalio/temporal:latest" not in compose


def test_repository_and_compose_project_use_the_oraiflow_identity():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert compose.startswith("name: oraiflow\n")
    # Keep the pre-existing Docker volume identity so upgrading does not orphan demo data.
    assert "name: oraiflow-core_postgres-data" in compose
    assert "git clone <published-repository-url> oraiflow" in readme


def test_pyproject_is_the_authoritative_python_application_manifest():
    import tomllib

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["name"] == "oraiflow"
    assert metadata["project"]["version"] == __version__
    assert metadata["project"]["requires-python"] == ">=3.12"
    assert metadata["tool"]["uv"]["package"] is False
    assert "langchain-core>=0.3,<2" in metadata["project"]["dependencies"]
    assert "pytest>=8,<9" in metadata["dependency-groups"]["dev"]
    assert not (ROOT / "requirements.txt").exists()
    assert not (ROOT / "dev-requirements.txt").exists()
    assert not (ROOT / "pytest.ini").exists()


def test_github_ci_enforces_python_syntax_lint_and_black_for_changed_files():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "scripts/check_changed_black.py" in workflow
    assert "ruff check" in workflow
    assert "compileall" in workflow
    assert "pytest -q" in workflow


def test_github_issue_and_pull_request_templates_are_present():
    issue_templates = ROOT / ".github/ISSUE_TEMPLATE"
    expected = {
        "01-bug-report.yml",
        "02-agent-behavior.yml",
        "03-feature-request.yml",
        "04-documentation.yml",
        "05-demo-setup.yml",
        "config.yml",
    }
    assert expected.issubset({path.name for path in issue_templates.iterdir()})
    assert (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").is_file()

    bug_form = (issue_templates / "01-bug-report.yml").read_text(encoding="utf-8")
    agent_form = (issue_templates / "02-agent-behavior.yml").read_text(encoding="utf-8")
    assert "🐛" in bug_form
    assert "🧭" in agent_form
    assert "synthetic demo data" in bug_form.casefold()
    assert "synthetic demo data" in agent_form.casefold()


def test_stable_public_docs_do_not_pin_the_application_release_version():
    version_pattern = re.compile(rf"(?<![0-9.])v?{re.escape(__version__)}(?![0-9.])")
    candidates = [ROOT / "README.md", ROOT / ".env.example", ROOT / "CONTRIBUTING.md"]
    candidates.extend((ROOT / "docs").rglob("*.md"))
    candidates.extend((ROOT / ".github").rglob("*.yml"))
    candidates.extend((ROOT / ".github").rglob("*.yaml"))

    offenders = [
        str(path.relative_to(ROOT))
        for path in candidates
        if path.is_file() and version_pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"Stable public docs must not pin the release version: {offenders}"


def test_agui_adapter_imports_snapshot_serialization_dependencies():
    import ast

    source = (ROOT / "services/agent_runtime/agui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names = {
        alias.asname or alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_names.update(
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "typing"
        for alias in node.names
    )

    assert {"json", "Any"}.issubset(imported_names)
