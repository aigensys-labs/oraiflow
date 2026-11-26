from shared.financial_health import calculate_recorded_financial_health


def test_financial_health_calculates_loan_to_equity_and_debt_to_assets_per_currency():
    result = calculate_recorded_financial_health(
        [
            {"currency": "EUR", "balance_minor": 2_000_000, "status": "ACTIVE"},
            {"currency": "EUR", "balance_minor": 500_000, "status": "ACTIVE"},
            {"currency": "EUR", "balance_minor": 100_000, "status": "CLOSED"},
        ],
        [{"currency": "EUR", "outstanding_principal_minor": 500_000, "demo_only": True}],
        include_demo_loans=True,
    )
    eur = result["positions"][0]
    assert eur["recorded_deposit_assets_minor"] == 2_500_000
    assert eur["loan_liabilities_minor"] == 500_000
    assert eur["recorded_equity_minor"] == 2_000_000
    assert eur["loan_to_equity_bps"] == 2500
    assert eur["debt_to_assets_bps"] == 2000
    assert eur["status"] == "LOW_LEVERAGE"
    assert result["coverage"]["is_credit_score"] is False


def test_financial_health_keeps_currencies_separate():
    result = calculate_recorded_financial_health(
        [
            {"currency": "EUR", "balance_minor": 1_000_000, "status": "ACTIVE"},
            {"currency": "USD", "balance_minor": 500_000, "status": "ACTIVE"},
        ],
        [
            {"currency": "EUR", "outstanding_principal_minor": 100_000, "demo_only": True},
            {"currency": "USD", "outstanding_principal_minor": 250_000, "demo_only": True},
        ],
        include_demo_loans=True,
    )
    assert [item["currency"] for item in result["positions"]] == ["EUR", "USD"]
    assert result["positions"][0]["recorded_deposit_assets_minor"] == 1_000_000
    assert result["positions"][1]["recorded_deposit_assets_minor"] == 500_000


def test_negative_deposit_balance_is_recorded_as_overdraft_liability():
    result = calculate_recorded_financial_health(
        [
            {"currency": "EUR", "balance_minor": 1_000_000, "status": "ACTIVE"},
            {"currency": "EUR", "balance_minor": -100_000, "status": "ACTIVE"},
        ],
        [{"currency": "EUR", "outstanding_principal_minor": 200_000, "demo_only": True}],
        include_demo_loans=True,
    )
    eur = result["positions"][0]
    assert eur["recorded_deposit_assets_minor"] == 1_000_000
    assert eur["overdraft_liabilities_minor"] == 100_000
    assert eur["total_recorded_liabilities_minor"] == 300_000


def test_zero_assets_is_insufficient_data_not_zero_risk():
    result = calculate_recorded_financial_health(
        [{"currency": "EUR", "balance_minor": 0, "status": "ACTIVE"}],
        [{"currency": "EUR", "outstanding_principal_minor": 100_000, "demo_only": True}],
        include_demo_loans=True,
    )
    eur = result["positions"][0]
    assert eur["status"] == "INSUFFICIENT_DATA"
    assert eur["loan_to_equity_bps"] is None


def test_liabilities_equal_to_assets_are_distinguished_from_exceeding_assets():
    result = calculate_recorded_financial_health(
        [{"currency": "EUR", "balance_minor": 500_000, "status": "ACTIVE"}],
        [{"currency": "EUR", "outstanding_principal_minor": 500_000, "demo_only": True}],
        include_demo_loans=True,
    )
    eur = result["positions"][0]
    assert eur["recorded_equity_minor"] == 0
    assert eur["loan_to_equity_bps"] is None
    assert eur["status"] == "LIABILITIES_MATCH_ASSETS"


def test_liabilities_above_assets_are_flagged_without_false_equity_ratio():
    result = calculate_recorded_financial_health(
        [{"currency": "EUR", "balance_minor": 500_000, "status": "ACTIVE"}],
        [{"currency": "EUR", "outstanding_principal_minor": 600_000, "demo_only": True}],
        include_demo_loans=True,
    )
    eur = result["positions"][0]
    assert eur["recorded_equity_minor"] == -100_000
    assert eur["loan_to_equity_bps"] is None
    assert eur["status"] == "LIABILITIES_EXCEED_ASSETS"


def test_includes_recorded_non_demo_loan_rows():
    result = calculate_recorded_financial_health(
        [{"currency": "EUR", "balance_minor": 800_000, "status": "ACTIVE"}],
        [{"currency": "EUR", "outstanding_principal_minor": 500_000, "demo_only": False}],
    )
    assert result["positions"][0]["loan_liabilities_minor"] == 500_000
    assert result["positions"][0]["status"] == "ELEVATED_LEVERAGE"


def test_demo_loan_is_excluded_by_default_from_financial_health():
    result = calculate_recorded_financial_health(
        [{"currency": "EUR", "balance_minor": 1_000_000, "status": "ACTIVE"}],
        [{"currency": "EUR", "outstanding_principal_minor": 500_000, "demo_only": True}],
    )
    eur = result["positions"][0]
    assert eur["loan_liabilities_minor"] == 0
    assert eur["recorded_equity_minor"] == 1_000_000
    assert (
        "opt-in illustrative demo loan outstanding principal" not in result["coverage"]["included"]
    )


def test_demo_loan_can_be_included_only_with_explicit_opt_in():
    result = calculate_recorded_financial_health(
        [{"currency": "EUR", "balance_minor": 1_000_000, "status": "ACTIVE"}],
        [{"currency": "EUR", "outstanding_principal_minor": 500_000, "demo_only": True}],
        include_demo_loans=True,
    )
    assert result["positions"][0]["loan_liabilities_minor"] == 500_000
    assert "opt-in illustrative demo loan outstanding principal" in result["coverage"]["included"]


def test_demo_loan_feature_is_opt_in_by_default(monkeypatch):
    from shared.demo_configuration import demo_loan_enabled

    monkeypatch.delenv("DEMO_LOAN_ENABLED", raising=False)
    assert demo_loan_enabled() is False
    for value in ("", "false", "0", "off", "no"):
        monkeypatch.setenv("DEMO_LOAN_ENABLED", value)
        assert demo_loan_enabled() is False
    for value in ("true", "1", "yes", "on", "TRUE"):
        monkeypatch.setenv("DEMO_LOAN_ENABLED", value)
        assert demo_loan_enabled() is True
