from services.agent_runtime.intent_contracts import classify_canonical_read_intent
from services.agent_runtime.scope_policy import (
    SAVINGS_EDUCATION_FALLBACK,
    finalize_response_envelope,
    is_savings_education_request,
)


def test_do_i_have_some_savings_resolves_to_account_overview():
    assert classify_canonical_read_intent("do I have some savings?") == "ACCOUNT_OVERVIEW"
    assert classify_canonical_read_intent("do I have any savings") == "ACCOUNT_OVERVIEW"
    assert classify_canonical_read_intent("show me my savings") == "ACCOUNT_OVERVIEW"


def test_savings_account_good_for_is_in_scope_financial_literacy():
    question = "what is my saving account good for?"
    assert is_savings_education_request(question)
    classification, answer = finalize_response_envelope(
        {"classification": "OUT_OF_SCOPE", "message": "I cannot help."},
        current_user_text=question,
        registered_intents={"ACCOUNT_OVERVIEW"},
    )
    assert classification == "EDUCATIONAL_GUIDANCE"
    assert answer == SAVINGS_EDUCATION_FALLBACK


def test_accounts_page_contains_only_deposit_accounts_and_loans_have_dedicated_workspace():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "apps/web/src/main.jsx").read_text()
    loan_page = source[
        source.index("function LoanDetailsPage") : source.index("function AcademyPage")
    ]
    accounts_page = source[
        source.index("function AccountsPage") : source.index("function ContactsPage")
    ]
    assert "Loans &amp; credit" in loan_page
    assert "not a real credit agreement" in loan_page
    assert (
        "payment_tracking"
        in (Path(__file__).resolve().parents[1] / "shared/loan_demo.py").read_text()
        or "NOT_TRACKED" in loan_page
    )
    assert "never count as available money or transfer sources" in loan_page
    assert "loan-overview-card" not in accounts_page
    assert "All accounts" in accounts_page


def test_compose_passes_loan_demo_configuration_to_migration_environment():
    from pathlib import Path
    import yaml

    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    migration_environment = compose["services"]["migrate"]["environment"]
    for name in (
        "DEMO_LOAN_ENABLED",
        "DEMO_LOAN_PRINCIPAL_MINOR",
        "DEMO_LOAN_APR_BPS",
        "DEMO_LOAN_TERM_MONTHS",
    ):
        assert name in migration_environment


def test_overview_replaces_account_health_with_loan_highlight_and_passes_loan_data():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "apps/web/src/main.jsx").read_text()
    assert 'label="Loans"' in source
    assert 'label="Account health"' not in source
    assert "loans={loans}" in source
    assert "Outstanding · next instalment" in source
    assert "fmtDateOnly(nextLoanInstallment.due_date)" in source
    assert "Illustrative demo liability · not a real credit agreement" in source


def test_overview_accounts_panel_keeps_only_three_deposit_accounts():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "apps/web/src/main.jsx").read_text()
    overview = source[
        source.index("function OverviewPage") : source.index("function LoanDetailsPage")
    ]
    assert "accounts.slice(0, 3)" in overview
    assert "loan-account-divider" not in overview
    assert "loan-account-row" not in overview
    assert "const primaryLoan" in overview
    assert 'label="Loans"' in overview


def test_deposit_total_is_not_modified_to_include_loan_liability():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "apps/web/src/main.jsx").read_text()
    assert "const total = overview?.total_balance?.amount_minor ?? 0" in source
    # Loan amount is rendered in its own summary and is not added to `total`.
    assert (
        "fmtMoney(primaryLoan.outstanding_principal_minor ?? primaryLoan.principal_minor" in source
    )


def test_overview_banner_contains_recorded_financial_health_and_is_after_welcome():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "apps/web/src/main.jsx").read_text()
    overview = source[
        source.index("function OverviewPage") : source.index("function LoanDetailsPage")
    ]
    assert "hero-health-panel" in overview
    assert "Loans / recorded equity" in overview
    assert "All liabilities / recorded deposits" in overview
    assert (
        overview.index("welcome-row") < overview.index("hero-banner") < overview.index("stats-grid")
    )
    app = source[source.index("function App()") :]
    assert "window.localStorage.getItem('oraiflow.overviewHeroVisible') === 'true'" in app
    assert "catch { return false }" in app
    assert "Hidden is the product default" in app and "always wins" in app


def test_overview_and_workspace_navigation_expose_loan_and_academy():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "apps/web/src/main.jsx").read_text()
    assert "activeNav === 'Loans'" in source
    assert "activeNav === 'Academy'" in source
    assert "ACADEMY_TOPICS.map" in source
    assert "Understand your badge" in source


def test_successful_action_ack_uses_standard_message_identity_and_final_replay_guard():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "services/agent_runtime/agent.py").read_text()
    assert '"name": ACTION_ACK_NAME' in source
    assert '"id": f"{ACTION_ACK_ID_PREFIX}{stable_action_id}"' in source
    assert "action_ack=True" in source
    assert "has_successful_frontend_ack_after_latest_user" in source
    assert "return {}" in source


def test_workspace_and_academy_ui_consume_the_versioned_registry_catalogue():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    ui = (root / "apps/web/src/main.jsx").read_text()
    contracts = (root / "apps/web/src/contracts.js").read_text()
    generated = (root / "apps/web/src/generated/contracts.js").read_text()
    registry = __import__("json").loads((root / "shared/contracts/registry.json").read_text())
    workspaces = registry["application_policy"]["workspaces"]
    topics = registry["application_policy"]["academy_topics"]

    # Page introductions read the same generated catalogue consumed by the UI,
    # rather than maintaining page-purpose copy in another prompt/component list.
    assert "const workspace = workspaceId ? WORKSPACE_CATALOGUE?.[workspaceId] : null" in ui
    assert "`${workspace.purpose} ${workspace.value}`" in ui
    assert "data-workspace-catalogue-version={WORKSPACE_CATALOGUE_VERSION}" in ui
    assert "ACADEMY_TOPICS.map" in ui
    assert (
        "export const WORKSPACE_CATALOGUE = Object.freeze(CONTRACT_REGISTRY.application_policy?.workspaces || {})"
        in contracts
    )
    assert (
        "export const ACADEMY_TOPICS = Object.freeze(CONTRACT_REGISTRY.application_policy?.academy_topics || [])"
        in contracts
    )
    assert "export const CONTRACT_REGISTRY" in generated

    # Every registered workspace represented in the UI uses a stable workspaceId.
    for workspace_id in workspaces:
        if workspace_id == "Overview":  # Overview has its own composed hero/header, not PageIntro.
            continue
        assert f'workspaceId="{workspace_id}"' in ui, workspace_id
    assert len(topics) >= 5
    assert {topic["topic_id"] for topic in topics} >= {
        "FINANCIAL_HEALTH_BADGE",
        "BORROWING_AND_LEVERAGE",
        "CREDIT_SCORE_BASICS",
        "SAVINGS_WITH_PURPOSE",
        "LOAN_REPAYMENTS_AND_APR",
    }


def test_academy_learning_paths_share_panel_height_and_orai_scrollbar_treatment():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "apps/web/src/main.jsx").read_text()
    styles = (root / "apps/web/src/styles.css").read_text()
    academy_start = source.index("function AcademyPage")
    academy_end = source.index("function AccountsPage", academy_start)
    academy = source[academy_start:academy_end]
    assert "Learn with ORAI" in academy
    assert "Explore learning paths" in academy
    assert "academy-topic-list" in academy
    assert ".academy-chat-section > .copilot-page-card," in styles
    assert ".academy-topics-section > .academy-topic-list" in styles
    assert ".academy-topic-list::-webkit-scrollbar,.chat-messages::-webkit-scrollbar" in styles
    assert (
        ".academy-topic-list::-webkit-scrollbar-thumb,.chat-messages::-webkit-scrollbar-thumb"
        in styles
    )
    assert (
        ".academy-topic-list {" in styles
        and "scrollbar-width:thin" in styles
        and "scrollbar-color:#29435a transparent" in styles
    )


def test_academy_and_cards_disclosure_banners_have_consistent_top_spacing():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "apps/web/src/main.jsx").read_text()
    styles = (root / "apps/web/src/styles.css").read_text()
    assert source.count('className="info-strip workspace-disclosure"') >= 2
    assert ".info-strip.workspace-disclosure{margin-top:18px}" in styles


def test_customer_response_interceptor_preserves_streamed_message_identity():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "services/agent_runtime/agent.py").read_text()
    protocol = source[
        source.index("def _customer_response_from_protocol") : source.index(
            "def _pending_navigation_message"
        )
    ]
    assert "replace_assistant_message_content(" in protocol
    assert 'streamed_content = getattr(response, "content", "")' in protocol
    assert 'streamed_customer_text=str(streamed_content or "")' in protocol
    assert "AIMessage(\n            content=customer_text" not in protocol


def test_demo_agui_provider_uses_same_validated_turn_contract_and_catalogue_routes():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "services/agent_runtime/agui.py").read_text()
    assert "return _selected_account_context(state)" in source
    assert "_build_agent_turn_context(" in source
    assert "_attach_agent_turn_contract(message, turn_context" in source
    assert "workspace_help_for_request(latest_text)" in source
    assert "academy_direct_guidance_for_request(latest_text)" in source
    assert "assistant_capability_help_for_request(latest_text)" in source
    assert "agent_turn_contract_prepared" in source and "provider=demo" in source
