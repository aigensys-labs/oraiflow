import json
from pathlib import Path

from shared.version import __version__

ROOT = Path(__file__).resolve().parents[1]


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_python_and_javascript_packages_share_the_release_version():
    assert __version__ == "0.7.7"
    assert _json("apps/web/package.json")["version"] == __version__
    assert _json("services/copilot_runtime/package.json")["version"] == __version__
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert f"ORAIflow Core v{__version__}" in env_example


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
