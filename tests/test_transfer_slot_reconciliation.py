from shared.transfer_slot_reconciliation import reconcile_explicit_transfer_slots


def _assessment(**slot_values):
    return {
        "intent_id": "TRANSFER_PREPARATION",
        "slots": dict(slot_values),
        "entities": {"beneficiary": "John"},
    }


def test_reconciles_omitted_amount_and_currency_after_model_selected_matching_contact_search():
    assessment = _assessment()
    recovered = reconcile_explicit_transfer_slots(
        assessment,
        current_customer_text="Send 1 EUR to John",
        proposed_recipient_query="John",
    )
    assert recovered == ["amount", "currency"]
    assert assessment["slots"] == {"amount": "1", "currency": "EUR"}


def test_reconciles_only_missing_values_and_never_overwrites_model_slots():
    assessment = _assessment(amount="2", currency="USD")
    recovered = reconcile_explicit_transfer_slots(
        assessment,
        current_customer_text="Send 1 EUR to John",
        proposed_recipient_query="John",
    )
    assert recovered == []
    assert assessment["slots"] == {"amount": "2", "currency": "USD"}


def test_does_not_infer_missing_currency_from_account_or_recipient_context():
    assessment = _assessment(amount=50)
    recovered = reconcile_explicit_transfer_slots(
        assessment,
        current_customer_text="Send 50 to Lucas",
        proposed_recipient_query="Lucas",
        context={"accounts": [{"account_id": "acc-001", "currency": "EUR"}], "selected_account_id": "acc-001"},
    )
    assert recovered == []
    assert assessment["slots"] == {"amount": 50}


def test_does_not_reconcile_when_model_proposes_a_different_recipient_query():
    assessment = _assessment()
    recovered = reconcile_explicit_transfer_slots(
        assessment,
        current_customer_text="Send 1 EUR to John",
        proposed_recipient_query="Jonathan",
    )
    assert recovered == []
    assert assessment["slots"] == {}


def test_does_not_reconcile_for_non_transfer_intent():
    assessment = {"intent_id": "RECENT_TRANSACTIONS", "slots": {}}
    recovered = reconcile_explicit_transfer_slots(
        assessment,
        current_customer_text="Send 1 EUR to John",
        proposed_recipient_query="John",
    )
    assert recovered == []
    assert assessment["slots"] == {}


def test_reconciles_explicit_source_account_when_recipient_currency_is_missing():
    assessment = _assessment(amount="100")
    context = {
        "accounts": [
            {"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR"},
            {"account_id": "acc-002", "account_name": "Savings account", "currency": "EUR"},
            {"account_id": "acc-003", "account_name": "Travel wallet", "currency": "EUR"},
        ]
    }
    recovered = reconcile_explicit_transfer_slots(
        assessment,
        current_customer_text="Send 100 to Lucas from everyday",
        proposed_recipient_query="Lucas",
        context=context,
    )
    assert recovered == ["source_account"]
    assert assessment["slots"]["source_account"] == "Everyday account"


def test_reconciles_source_account_from_canonical_alias_with_my_or_out_of_prefix():
    context = {"accounts": [
        {"account_id": "acc-003", "account_name": "Travel wallet", "currency": "EUR"},
    ]}
    for text in (
        "Send 100 to Lucas from my travel",
        "Send 100 to Lucas out of the Travel wallet",
        "Send 100 to Lucas using Travel wallet",
    ):
        assessment = _assessment(amount="100")
        recovered = reconcile_explicit_transfer_slots(
            assessment, current_customer_text=text,
            proposed_recipient_query="Lucas", context=context,
        )
        assert recovered == ["source_account"], text
        assert assessment["slots"]["source_account"] == "Travel wallet"


def test_does_not_recover_ambiguous_or_unmentioned_source_account():
    ambiguous_context = {"accounts": [
        {"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR"},
        {"account_id": "acc-004", "account_name": "Everyday wallet", "currency": "EUR"},
    ]}
    assessment = _assessment(amount="100")
    recovered = reconcile_explicit_transfer_slots(
        assessment, current_customer_text="Send 100 to Lucas from everyday",
        proposed_recipient_query="Lucas", context=ambiguous_context,
    )
    assert recovered == []
    assert "source_account" not in assessment["slots"]

    assessment = _assessment(amount="100")
    recovered = reconcile_explicit_transfer_slots(
        assessment, current_customer_text="Send 100 to Lucas",
        proposed_recipient_query="Lucas", context=ambiguous_context,
    )
    assert recovered == []
    assert "source_account" not in assessment["slots"]


def test_send_100_to_lucas_from_everyday_needs_only_recipient_currency_confirmation():
    import json

    from services.agent_runtime.interpretation import validate_interpretation_assessment
    from shared.transfer_workflow import (
        build_transfer_workflow,
        resolve_currency_reply,
        resolve_search_result,
        apply_verified_source_account_selection,
    )

    accounts = [
        {"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR"},
        {"account_id": "acc-002", "account_name": "Savings account", "currency": "EUR"},
        {"account_id": "acc-003", "account_name": "Travel wallet", "currency": "EUR"},
    ]
    context = {"accounts": accounts, "selected_account_id": None}
    customer_text = "Send 100 to Lucas from everyday"
    assessment = {
        "intent_id": "TRANSFER_PREPARATION",
        "intent_confidence": 0.95,
        "slot_confidence": 0.95,
        "entity_confidence": 0.95,
        "capability_id": "banking.contacts.search",
        "capability_selection_confidence": 0.95,
        "resolution": "READY",
        "slots": {"amount": "100"},
        "entities": {"beneficiary": "Lucas"},
        "evidence": [
            {"field": "intent", "source": "current_turn", "quote": customer_text},
            {"field": "amount", "source": "current_turn", "quote": "100"},
            {"field": "beneficiary", "source": "current_turn", "quote": "Lucas"},
        ],
    }
    recovered = reconcile_explicit_transfer_slots(
        assessment, current_customer_text=customer_text,
        proposed_recipient_query="Lucas", context=context,
    )
    assert recovered == ["source_account"]
    validated, decision = validate_interpretation_assessment(
        assessment,
        expected_capability_id="banking.contacts.search",
        sources={
            "current_turn": customer_text,
            "conversation": customer_text,
            "application_context": json.dumps(context),
        },
    )
    assert validated is not None and decision["decision"] == "PASS"

    workflow = build_transfer_workflow(validated.model_dump(), context)
    lucas = {
        "contact_id": "ct-lucas-meyer", "full_name": "Lucas Meyer",
        "currency": "EUR", "status": "ACTIVE",
    }
    status, workflow = resolve_search_result(workflow, [lucas], context)
    assert status == "AWAITING_CURRENCY"
    assert workflow["source_account_query"] == "Everyday account"

    status, workflow = resolve_currency_reply(workflow, "ok", context)
    assert status == "VERIFY_SOURCE_ACCOUNT"
    assert workflow["source_account_id"] == "acc-001"

    status, workflow = apply_verified_source_account_selection(workflow, [
        {"account_id": "acc-001", "currency": "EUR", "status": "ACTIVE", "balance_minor": 2_013_080},
    ])
    assert status == "READY_FOR_DRAFT"
    assert workflow["stage"] == "READY_FOR_DRAFT"
