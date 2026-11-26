from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from shared.contracts.registry import (
    ContractViolation,
    capability_by_name,
    create_result_envelope,
    get_registry,
    validate_application_context,
    validate_capability_input,
    validate_capability_output,
    validate_kernel_input,
    validate_presentation_props,
    validate_result_envelope,
    validate_registry_consistency,
    validate_schema,
    pydantic_model_for_schema,
    get_capability,
)

ROOT = Path(__file__).resolve().parents[1]


def test_registry_is_loadable_and_every_declared_schema_is_valid():
    registry = get_registry()
    assert registry["registry_version"] == "1.5.17"
    assert len(registry["capabilities"]) == 11
    assert len(registry["intents"]) == 17
    assert len(registry["presentations"]) == 3
    assert len(registry["kernel_operations"]) == 3
    validate_registry_consistency(registry)
    transaction = registry["intents"]["TRANSACTION_EXPLANATION"]["execution_plan"]
    selector = transaction["semantic_inputs"]["transaction_selector"]
    assert selector["allowed_values"] == ["latest"]
    assert selector["default"] == "latest"
    assert selector["argument_mappings"]["latest"] == {"limit": 1}
    account_total = registry["intents"]["ACCOUNT_TOTAL_BALANCE"]["execution_plan"]
    scope = account_total["semantic_inputs"]["account_scope"]
    assert scope["default"] == "all_accounts"
    assert account_total["ignore_selected_account"] is True


def test_transaction_selector_has_one_canonical_semantic_definition():
    plan = get_registry()["intents"]["TRANSACTION_EXPLANATION"]["execution_plan"]
    selector = plan["semantic_inputs"]["transaction_selector"]
    assert selector["allowed_values"] == ["latest"]
    assert selector["default"] == "latest"
    assert selector["argument_mappings"]["latest"] == {"limit": 1}
    # Do not maintain a second copy of this field in legacy slot metadata or
    # static arguments; planner compilation must use semantic_inputs only.
    assert "transaction_selector" not in plan.get("optional_slots", [])
    assert "transaction_selector" not in plan.get("slot_descriptions", {})
    assert "transaction_selector" not in plan.get("slot_values", {})
    assert "limit" not in plan.get("arguments", {})


def test_rich_semantic_inputs_are_not_duplicated_in_legacy_slot_metadata():
    for intent_id, intent in get_registry()["intents"].items():
        plan = intent.get("execution_plan", {})
        semantic_inputs = plan.get("semantic_inputs", {})
        if not semantic_inputs:
            continue
        semantic_slot_names = {
            field for field, spec in semantic_inputs.items()
            if spec.get("location", "slots") == "slots"
        }
        assert not semantic_slot_names.intersection(plan.get("optional_slots", [])), intent_id
        assert not semantic_slot_names.intersection(plan.get("required_slots", [])), intent_id
        assert not semantic_slot_names.intersection(plan.get("slot_descriptions", {})), intent_id
        assert not semantic_slot_names.intersection(plan.get("slot_values", {})), intent_id



def test_registry_rejects_duplicate_legacy_declarations_of_semantic_inputs():
    import copy

    registry = copy.deepcopy(get_registry())
    plan = registry["intents"]["TRANSACTION_ANALYSIS"]["execution_plan"]
    plan.setdefault("optional_slots", []).append("period")
    with pytest.raises(RuntimeError, match="declared in both semantic_inputs and optional_slots"):
        validate_registry_consistency(registry)

    # Requiredness belongs to semantic_inputs too; it does not need a second
    # declaration in required_slots to become enforceable by the planner.
    registry = copy.deepcopy(get_registry())
    plan = registry["intents"]["TRANSACTION_ANALYSIS"]["execution_plan"]
    period = plan["semantic_inputs"]["period"]
    period["required"] = True
    period.pop("default", None)
    # A registered conditional default is still permitted, but the semantic
    # field itself remains the sole owner of requiredness.
    period["default_policy"] = "when_unspecified"
    validate_registry_consistency(registry)

def test_every_agent_capability_has_input_output_execution_and_authorization_contracts():
    for contract_id, contract in get_registry()["capabilities"].items():
        assert contract["input_schema"]
        assert contract["output_schema"]
        assert contract["execution"]["effect"]
        assert contract["execution"]["retry"]
        assert contract["authorization"]["principal"]
        assert isinstance(contract["authorization"]["agent_invocable"], bool)
        assert capability_by_name(contract["name"])[0] == contract_id



def test_loan_overview_can_return_a_valid_empty_result_when_no_recorded_loan_exists():
    assert validate_capability_output("banking.loans.overview", {}) == {}

def test_application_context_uses_registered_schema_and_is_read_only():
    from shared.contracts.registry import get_registry

    context = {
        "agent_turn_contract_version": "1.1.0",
        "workspace": "Activity",
        "selected_account_id": "acc-001",
        "selected_account_name": "Everyday account",
        "accounts": [
            {"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR"},
            {"account_id": "acc-002", "account_name": "Savings", "currency": "EUR"},
        ],
        "contacts": [],
    }
    assert validate_application_context(context) == context
    contract = get_registry()["application_context"]
    assert contract["authority"] == "frontend_read_only"
    assert contract["may_authorize_banking_operations"] is False


def test_application_context_rejects_unknown_or_inconsistent_selection():
    context = {
        "agent_turn_contract_version": "1.1.0",
        "workspace": "Activity",
        "selected_account_id": "acc-999",
        "selected_account_name": "Everyday account",
        "accounts": [
            {"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR"},
        ],
        "contacts": [],
    }
    with pytest.raises(ContractViolation, match="selected_account_matches_catalogue"):
        validate_application_context(context)

    context["selected_account_id"] = "acc-001"
    context["selected_account_name"] = "Forged label"
    with pytest.raises(ContractViolation, match="selected_account_matches_catalogue"):
        validate_application_context(context)


def test_application_context_rejects_duplicate_accounts_and_identity_fields():
    account = {"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR"}
    context = {
        "agent_turn_contract_version": "1.1.0",
        "workspace": "Overview",
        "selected_account_id": None,
        "selected_account_name": "All accounts",
        "accounts": [account, account],
        "contacts": [],
    }
    with pytest.raises(ContractViolation, match="unique_account_identifiers"):
        validate_application_context(context)
    context["accounts"] = [account]
    context["customer_id"] = "customer-from-model"
    with pytest.raises(ContractViolation):
        validate_application_context(context)


def test_agent_assisted_intents_map_to_registered_capabilities_or_catalogue_resources():
    registry = get_registry()
    for intent_id, intent in registry["intents"].items():
        if intent["recognition"]["mode"] != "agent_assisted":
            continue
        plan = intent["execution_plan"]
        if plan.get("capability"):
            capability = registry["capabilities"][plan["capability"]]
            assert capability["authorization"]["agent_invocable"] is True
            assert plan["response_mode"]
        else:
            assert plan["response_mode"] in {"registered_catalogue", "registered_resource"}
            assert plan["resource_id"] in {"assistant_capabilities", "academy_topics", "workspace_catalogue"}


def test_loan_overview_is_read_only_and_separate_from_deposit_accounts():
    contract = get_capability("banking.loans.overview")
    assert contract["kind"] == "mcp"
    assert contract["authorization"]["required_capability"] == "loans:read"
    assert contract["authorization"]["agent_invocable"] is True
    assert contract["execution"]["effect"] == "read_only"
    assert contract["input_schema"]["properties"] == {}
    assert "banking.loans.overview" not in get_registry()["presentations"]["render_account_summary"]["binding"]["source_capabilities"]
    assert "banking.loans.overview" not in get_registry()["capabilities"]["banking.accounts.list"]["output_schema"].get("properties", {})


def test_frontend_preconditions_use_only_declared_generic_evaluators():
    registry = get_registry()
    expected = {
        "workspace_allowlist",
        "context_field_present",
        "context_collection_contains_argument",
    }
    for capability_id, contract in registry["capabilities"].items():
        if contract["kind"] != "frontend":
            continue
        assert contract["preconditions"]
        for precondition in contract["preconditions"]:
            assert precondition["enforced_by"] == "frontend"
            assert precondition["evaluator"] in expected

    navigation = registry["application_policy"]["navigation"]
    assert "My profile" not in navigation["pages"]
    assert navigation["aliases"]["audit"] == "Controls & audit"


def test_account_read_rejects_unexpected_identity_fields():
    with pytest.raises(ContractViolation):
        validate_capability_input("banking.accounts.list", {"customer_id": "attacker"})


def test_account_balance_requires_account_id():
    with pytest.raises(ContractViolation):
        validate_capability_input("banking.accounts.balance", {})


def test_recent_transactions_limit_is_bounded_and_strict():
    assert validate_capability_input("banking.transactions.recent", {"limit": 6})["limit"] == 6
    with pytest.raises(ContractViolation):
        validate_capability_input("banking.transactions.recent", {"limit": 100})
    with pytest.raises(ContractViolation):
        validate_capability_input("banking.transactions.recent", {"limit": True})



def test_contacts_search_contract_is_read_only_and_returns_only_resolution_fields():
    contract = get_capability("banking.contacts.search")
    assert contract["kind"] == "mcp"
    assert contract["execution"]["effect"] == "read_only"
    assert contract["authorization"]["agent_invocable"] is True
    assert contract["authorization"]["required_capability"] == "contacts:read"
    assert validate_capability_input("banking.contacts.search", {"name_query": "John"})["name_query"] == "John"
    with pytest.raises(ContractViolation):
        validate_capability_input("banking.contacts.search", {"name_query": "John", "customer_id": "forged"})
    contact = {
        "contact_id": "ct-12345678", "full_name": "John Carter", "bank_name": "Northstar Bank",
        "account_reference_masked": "DEMO ···· 1042", "currency": "EUR", "status": "ACTIVE",
    }
    assert validate_capability_output("banking.contacts.search", [contact]) == [contact]
    with pytest.raises(ContractViolation):
        validate_capability_output("banking.contacts.search", [{**contact, "email": "john@example.test"}])


def test_transfer_draft_requires_a_resolved_contact_identifier():
    valid = {
        "beneficiary": "John Carter", "amount": "50.00", "currency": "EUR",
        "from_account_id": "acc-001", "beneficiary_contact_id": "ct-12345678",
        "workflow_id": "trf-test-12345678", "action_id": "act-test-12345678",
    }
    assert validate_capability_input("frontend.transfer.prepare_draft", valid) == valid
    with pytest.raises(ContractViolation):
        validate_capability_input("frontend.transfer.prepare_draft", {key: value for key, value in valid.items() if key != "beneficiary_contact_id"})


def test_application_context_rejects_duplicate_contact_ids():
    contact = {"contact_id": "ct-12345678", "full_name": "John Carter", "status": "ACTIVE"}
    context = {
        "agent_turn_contract_version": "1.1.0",
        "workspace": "Contacts", "selected_account_id": None, "selected_account_name": "All accounts",
        "accounts": [], "contacts": [contact, contact],
    }
    with pytest.raises(ContractViolation, match="unique_contact_identifiers"):
        validate_application_context(context)

def test_recent_transaction_output_requires_verified_typed_records():
    valid = [{
        "transaction_id": "txn-1", "account_name": "Everyday account",
        "description": "Coffee", "category": "Dining", "amount_minor": -485,
        "currency": "EUR", "status": "BOOKED", "occurred_at": None,
    }]
    assert validate_capability_output("banking.transactions.recent", valid) == valid
    with pytest.raises(ContractViolation):
        validate_capability_output("banking.transactions.recent", [{**valid[0], "amount_minor": "-485"}])


def test_success_envelope_carries_provenance_and_validated_data():
    payload = [{
        "transaction_id": "txn-1", "account_name": "Everyday account",
        "description": "Coffee", "category": None, "amount_minor": -485,
        "currency": "EUR", "status": "BOOKED", "occurred_at": None,
    }]
    envelope = create_result_envelope(
        "banking.transactions.recent", "SUCCESS", data=payload, trace_id="trace-123"
    )
    assert envelope["evidence"]["source"] == "mcp"
    assert envelope["evidence"]["scope"] == "authenticated_customer_transactions"
    assert envelope["trace_id"] == "trace-123"
    assert validate_result_envelope("banking.transactions.recent", envelope) == envelope


def test_empty_success_is_distinct_from_failure():
    empty = create_result_envelope("banking.transactions.recent", "EMPTY", data=[], trace_id="trace-empty")
    assert empty["outcome"] == "EMPTY"
    assert empty["data"] == []
    assert empty["evidence"]["completeness"] == "EMPTY"
    assert validate_result_envelope("banking.transactions.recent", empty) == empty


def test_failure_envelope_has_no_data_or_evidence():
    failure = create_result_envelope(
        "banking.transactions.recent", "UNAVAILABLE",
        error={"code": "BANKING_DATA_UNAVAILABLE", "retryable": True},
        trace_id="trace-fail",
    )
    assert failure["data"] is None and failure["evidence"] is None
    assert failure["trust_class"] == "connector_error"
    assert validate_result_envelope("banking.transactions.recent", failure) == failure


def test_result_envelope_rejects_capability_substitution():
    envelope = create_result_envelope("banking.transactions.recent", "EMPTY", data=[])
    with pytest.raises(ContractViolation):
        validate_result_envelope("banking.accounts.list", envelope)


def test_presentation_contract_rejects_model_invented_rows_and_titles():
    evidence = {
        "source": "mcp", "scope": "authenticated_customer_transactions",
        "retrieved_at": "2026-11-21T00:00:00+00:00", "completeness": "COMPLETE", "trace_id": "t1",
    }
    props = {"title": "Recent activity", "transactions": [], "evidence": evidence}
    assert validate_presentation_props("render_transaction_list", props) == props
    with pytest.raises(ContractViolation):
        validate_presentation_props("render_transaction_list", {**props, "transactions": [{"fake": 1}]})


def test_kernel_operations_are_explicitly_not_agent_callable():
    for operation in get_registry()["kernel_operations"].values():
        assert operation["agent_callable"] is False
        assert operation["authority"] == "banking_kernel"
    with pytest.raises(ContractViolation):
        validate_kernel_input("kernel.transfer.confirm", {"preview_id": "p"})


def test_generated_frontend_contracts_match_canonical_registry():
    result = subprocess.run(
        [sys.executable, "scripts/generate_contracts.py", "--check"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_result_envelope_rejects_outcome_data_and_evidence_mismatches():
    transactions = [{
        "transaction_id": "txn-1", "account_name": "Everyday account", "description": "Coffee",
        "category": None, "amount_minor": -485, "currency": "EUR", "status": "BOOKED", "occurred_at": None,
    }]
    with pytest.raises(ContractViolation):
        create_result_envelope("banking.transactions.recent", "EMPTY", data=transactions)
    with pytest.raises(ContractViolation):
        create_result_envelope("banking.transactions.recent", "SUCCESS", data=[])


def test_result_envelope_rejects_forged_provenance_and_trace_mismatch():
    envelope = create_result_envelope(
        "banking.transactions.recent", "EMPTY", data=[], trace_id="trace-original"
    )
    forged_source = {**envelope, "evidence": {**envelope["evidence"], "source": "model"}}
    forged_scope = {**envelope, "evidence": {**envelope["evidence"], "scope": "other_customer"}}
    forged_trace = {**envelope, "evidence": {**envelope["evidence"], "trace_id": "another-trace"}}
    for forged in (forged_source, forged_scope, forged_trace):
        with pytest.raises(ContractViolation):
            validate_result_envelope("banking.transactions.recent", forged)


def test_registry_rejects_unknown_context_semantic_rules_and_frontend_precondition_fields():
    import copy

    registry = copy.deepcopy(get_registry())
    registry["application_context"]["semantic_rules"][0]["evaluator"] = "guess_account"
    with pytest.raises(RuntimeError, match="unsupported application context semantic evaluator"):
        validate_registry_consistency(registry)

    registry = copy.deepcopy(get_registry())
    registry["capabilities"]["frontend.account.select"]["preconditions"][0]["argument"] = "unknown_field"
    with pytest.raises(RuntimeError, match="undeclared input argument"):
        validate_registry_consistency(registry)


def test_registry_rejects_unknown_references_and_agent_callable_kernel_operations():
    import copy

    registry = copy.deepcopy(get_registry())
    registry["presentations"]["render_transaction_list"]["binding"]["source_capabilities"] = ["banking.transactions.missing"]
    with pytest.raises(RuntimeError, match="unknown source capability"):
        validate_registry_consistency(registry)

    registry = copy.deepcopy(get_registry())
    registry["kernel_operations"]["kernel.transfer.confirm"]["agent_callable"] = True
    with pytest.raises(RuntimeError, match="must remain unavailable to the agent"):
        validate_registry_consistency(registry)


def test_model_facing_tool_schemas_are_generated_from_registry_inputs():
    account_list_model = pydantic_model_for_schema(
        get_capability("banking.accounts.list")["input_schema"], "ListCustomerAccountsInput"
    )
    balance_model = pydantic_model_for_schema(
        get_capability("banking.accounts.balance")["input_schema"], "GetAccountBalanceInput"
    )
    transactions_model = pydantic_model_for_schema(
        get_capability("banking.transactions.recent")["input_schema"], "GetRecentTransactionsInput"
    )

    assert account_list_model.model_validate({}).model_dump() == {}
    assert balance_model.model_validate({"account_id": "acc-001"}).account_id == "acc-001"
    with pytest.raises(Exception):
        balance_model.model_validate({"account_id": None})
    assert transactions_model.model_validate({}).limit == 5
    assert transactions_model.model_validate({"limit": 20}).limit == 20
    with pytest.raises(Exception):
        transactions_model.model_validate({"limit": 21})
    with pytest.raises(Exception):
        transactions_model.model_validate({"limit": True})


def test_workspace_navigation_contract_accepts_optional_runtime_action_id():
    from shared.contracts.registry import ContractViolation, validate_capability_input

    validate_capability_input("frontend.workspace.navigate", {"workspace": "Contacts"})
    validate_capability_input(
        "frontend.workspace.navigate",
        {"workspace": "Contacts", "action_id": "nav-1234567890abcdef1234567890abcdef"},
    )
    with pytest.raises(ContractViolation):
        validate_capability_input(
            "frontend.workspace.navigate",
            {"workspace": "Contacts", "action_id": "invalid-id"},
        )


def test_0_6_4_registry_has_versioned_workspace_academy_and_turn_outcome_contracts():
    registry = get_registry()
    policy = registry["application_policy"]
    assert policy["workspace_catalogue_version"] == "1.0.0"
    assert policy["academy_catalogue_version"] == "1.0.0"
    assert policy["turn_outcome_contract_version"] == "1.2.0"
    assert set(policy["workspaces"]) == set(policy["navigation"]["pages"])
    assert {"Loans", "Academy", "Payments", "Controls & audit"} <= set(policy["workspaces"])
    assert len({topic["topic_id"] for topic in policy["academy_topics"]}) == len(policy["academy_topics"])
    assert all(topic.get("keywords") for topic in policy["academy_topics"])
    validate_schema("turn_outcome", "schema", registry["turn_outcome_schema"], {
        "schema_version": "1.2.0",
        "turn_id": "turn-1",
        "trace_ids": [],
        "outcome": "RESPONSE_ONLY",
        "successful_capabilities": [],
        "partial_or_empty_capabilities": [],
        "failed_capabilities": [],
        "successful_frontend_actions": [],
        "failed_frontend_actions": [],
        "task_outcomes": [],
    })


def test_application_context_rejects_mismatched_agent_turn_contract_version():
    context = {
        "agent_turn_contract_version": "1.0.0",
        "workspace": "Overview",
        "selected_account_id": None,
        "selected_account_name": "All accounts",
        "accounts": [],
        "contacts": [],
    }
    with pytest.raises(ContractViolation):
        validate_application_context(context)



def test_transaction_analysis_capability_is_read_only_scoped_and_strict():
    capability = get_capability("banking.transactions.analyze")
    assert capability["name"] == "analyze_customer_transactions"
    assert capability["authorization"]["required_capability"] == "transactions:read"
    assert capability["authorization"]["agent_invocable"] is True
    assert capability["execution"]["effect"] == "read_only"
    assert capability["evidence"]["scope"] == "authenticated_customer_transactions"
    request = {
        "period": "this_month", "flow": "outflows", "merchant_query": "coffee",
        "measure": "sum", "group_by": "none", "analysis_mode": "activity",
        "include_transfers": False, "limit": 50,
    }
    assert validate_capability_input("banking.transactions.analyze", request) == request
    with pytest.raises(ContractViolation):
        validate_capability_input("banking.transactions.analyze", {**request, "owner_id": "forged"})
    with pytest.raises(ContractViolation):
        validate_capability_input("banking.transactions.analyze", {**request, "group_by": "arbitrary_sql"})
    with pytest.raises(ContractViolation):
        validate_capability_input("banking.transactions.analyze", {**request, "period": "2026-11"})


def test_transaction_analysis_result_preserves_currency_and_aggregation_evidence():
    from shared.contracts.registry import validate_capability_output

    payload = {
        "period": "this_month", "period_start": "2026-11-01", "period_end": "2026-11-24",
        "business_timezone": "Europe/Prague", "flow": "outflows", "analysis_mode": "activity",
        "measure": "sum", "group_by": "merchant", "include_transfers": False,
        "transaction_count": 2, "truncated": False,
        "summary_by_currency": [{"currency": "EUR", "transaction_count": 2, "sum_minor": 970, "average_minor": 485}],
        "groups": [{"group_value": "Blue Bottle Coffee", "currency": "EUR", "transaction_count": 2,
                    "sum_minor": 970, "average_minor": 485, "active_months": 1, "potentially_recurring": False}],
        "transactions": [],
    }
    assert validate_capability_output("banking.transactions.analyze", payload) == payload
    with pytest.raises(ContractViolation):
        validate_capability_output("banking.transactions.analyze", {**payload, "summary_by_currency": [{**payload["summary_by_currency"][0], "currency": "EUR+USD"}]})
