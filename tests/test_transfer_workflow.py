from __future__ import annotations

from shared.contact_resolution import resolve_contact_choice
from shared.transfer_workflow import (
    apply_frontend_dismissal,
    apply_frontend_draft_result,
    apply_verified_account_ranking,
    apply_verified_source_account_selection,
    account_choice_prompt,
    build_transfer_workflow,
    build_explicit_transfer_workflow,
    latest_pending_workflow,
    with_workflow,
    currency_choice_prompt,
    resolve_contact_reply,
    resolve_currency_reply,
    resolve_search_result,
    resolve_source_account_reply,
)


ACCOUNTS = [
    {"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR"},
    {"account_id": "acc-002", "account_name": "Savings account", "currency": "EUR"},
    {"account_id": "acc-003", "account_name": "Travel wallet", "currency": "EUR"},
]
CONTACTS = [
    {"contact_id": "ct-john-carter", "full_name": "John Carter", "bank_name": "Northstar Bank", "account_reference_masked": "DEMO ···· 1042", "currency": "EUR", "status": "ACTIVE"},
    {"contact_id": "ct-john-mitchell", "full_name": "John Mitchell", "bank_name": "Union Financial", "account_reference_masked": "DEMO ···· 2781", "currency": "EUR", "status": "ACTIVE"},
]
CONTEXT = {"accounts": ACCOUNTS, "contacts": CONTACTS, "selected_account_id": None}


def make_workflow(**slots):
    assessment = {
        "intent_id": "TRANSFER_PREPARATION",
        "slots": {"amount": "50", "currency": "EUR", **slots},
        "entities": {"beneficiary": "John"},
    }
    return build_transfer_workflow(assessment, CONTEXT)


def test_duplicate_john_requires_choice_then_unique_surname_resolves_candidate():
    workflow = make_workflow()
    status, workflow = resolve_search_result(workflow, CONTACTS, CONTEXT)
    assert status == "AWAITING_CONTACT"
    status, workflow = resolve_contact_reply(workflow, "Carter", CONTEXT)
    assert status == "AWAITING_SOURCE_ACCOUNT"
    assert workflow["contact"]["contact_id"] == "ct-john-carter"


def test_latter_one_resolves_only_within_returned_contact_candidates():
    assert resolve_contact_choice(CONTACTS, "the latter one") == CONTACTS[1]


def test_combined_contact_and_short_account_answer_resolves_both():
    workflow = make_workflow()
    _, workflow = resolve_search_result(workflow, CONTACTS, CONTEXT)
    status, workflow = resolve_contact_reply(workflow, "Carter from Everyday account", CONTEXT)
    assert status == "VERIFY_SOURCE_ACCOUNT"
    assert workflow["stage"] == "VERIFYING_SOURCE_ACCOUNT"
    assert workflow["contact"]["contact_id"] == "ct-john-carter"
    assert workflow["source_account_id"] == "acc-001"


def test_everyday_and_everyday_account_both_resolve_the_same_trusted_account():
    workflow = make_workflow()
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)
    for answer in ("Everyday", "Everyday account"):
        status, updated = resolve_source_account_reply(workflow, answer, CONTEXT)
        assert status == "VERIFY_SOURCE_ACCOUNT"
        assert updated["stage"] == "VERIFYING_SOURCE_ACCOUNT"
        assert updated["source_account_id"] == "acc-001"


def test_unrelated_acknowledgement_is_not_misread_as_account_selection():
    workflow = make_workflow()
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)
    status, updated = resolve_source_account_reply(workflow, "ok", CONTEXT)
    assert status == "UNRELATED"
    assert updated.get("source_account_id", "") == ""


def test_acknowledgement_does_not_become_account_choice_from_ui_selection():
    workflow = make_workflow()
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)
    context_with_selection = {**CONTEXT, "selected_account_id": "acc-001"}
    status, updated = resolve_source_account_reply(workflow, "ok", context_with_selection)
    assert status == "UNRELATED"
    assert updated.get("source_account_id", "") == ""


def test_comparative_preference_is_not_overridden_by_existing_ui_selection():
    workflow = make_workflow()
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)
    context_with_selection = {**CONTEXT, "selected_account_id": "acc-001"}
    status, updated = resolve_source_account_reply(workflow, "with the highest amount", context_with_selection)
    assert status == "NEEDS_VERIFIED_BALANCE"
    assert updated["stage"] == "RESOLVING_SOURCE_ACCOUNT"
    assert updated["account_resolution_reason"] == "NEEDS_VERIFIED_BALANCE_MAX"
    assert updated.get("source_account_id", "") == ""


def test_highest_amount_one_resolves_pending_transfer_through_verified_balance_ranking():
    workflow = make_workflow()
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)

    status, resolving = resolve_source_account_reply(workflow, "the highest amount one", CONTEXT)
    assert status == "NEEDS_VERIFIED_BALANCE"
    assert resolving["stage"] == "RESOLVING_SOURCE_ACCOUNT"
    assert resolving["source_account_preference"] == "highest_balance"

    verified_accounts = [
        {"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR", "status": "ACTIVE", "balance_minor": 2_013_080},
        {"account_id": "acc-002", "account_name": "Savings account", "currency": "EUR", "status": "ACTIVE", "balance_minor": 860_000},
        {"account_id": "acc-003", "account_name": "Travel wallet", "currency": "EUR", "status": "ACTIVE", "balance_minor": 248_750},
    ]
    status, ready = apply_verified_account_ranking(
        resolving, CONTEXT, verified_accounts,
    )
    assert status == "READY_FOR_DRAFT"
    assert ready["source_account_id"] == "acc-001"
    assert ready["account_resolution_reason"] == "VERIFIED_HIGHEST_BALANCE"


def test_highest_balance_preference_is_resolved_only_from_verified_mcp_accounts():
    workflow = make_workflow(source_account_preference="highest_balance")
    status, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)
    assert status == "NEEDS_VERIFIED_BALANCE"
    verified = [
        {"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR", "balance_minor": 1242980},
        {"account_id": "acc-002", "account_name": "Savings account", "currency": "EUR", "balance_minor": 865000},
        {"account_id": "acc-003", "account_name": "Travel wallet", "currency": "EUR", "balance_minor": 248750},
    ]
    status, workflow = apply_verified_account_ranking(workflow, CONTEXT, verified)
    assert status == "READY_FOR_DRAFT"
    assert workflow["source_account_id"] == "acc-001"


def test_highest_balance_tie_requires_customer_choice():
    workflow = make_workflow(source_account_preference="highest_balance")
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)
    verified = [
        {"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR", "balance_minor": 1000},
        {"account_id": "acc-002", "account_name": "Savings account", "currency": "EUR", "balance_minor": 1000},
        {"account_id": "acc-003", "account_name": "Travel wallet", "currency": "EUR", "balance_minor": 500},
    ]
    status, workflow = apply_verified_account_ranking(workflow, CONTEXT, verified)
    assert status == "AWAITING_SOURCE_ACCOUNT"
    assert workflow["account_resolution_reason"] == "BALANCE_RANKING_TIED"
    assert not workflow.get("source_account_id")


def test_model_semantic_slot_can_drive_highest_amount_ranking_without_static_parser():
    workflow = make_workflow(source_account_preference="highest_balance")
    assert workflow["source_account_preference"] == "highest_balance"
    assert workflow["stage"] == "CONTACT_SEARCH_PENDING"
    assert workflow["account_resolution_reason"] == "NEEDS_VERIFIED_BALANCE_MAX"


def test_frontend_draft_result_is_terminal_and_dismissal_is_recorded():
    workflow = make_workflow()
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)
    _, workflow = resolve_source_account_reply(workflow, "Everyday", CONTEXT)
    workflow["action_id"] = "act-test-12345678"
    workflow["workflow_id"] = "trf-test-12345678"
    opened = apply_frontend_draft_result(workflow, {"status": "draft_opened", "workflow_id": "trf-test-12345678", "action_id": "act-test-12345678"})
    assert opened["stage"] == "DRAFT_OPENED"
    dismissed = apply_frontend_dismissal(opened, {
        "workflow_id": "trf-test-12345678",
        "action_id": "act-test-12345678",
        "status": "dismissed",
    })
    assert dismissed["stage"] == "DISMISSED"


def test_frontend_dismissal_cannot_apply_to_a_different_action():
    workflow = make_workflow()
    workflow.update({"workflow_id": "trf-a-12345678", "action_id": "act-a-12345678", "stage": "DRAFT_OPENED"})
    unchanged = apply_frontend_dismissal(workflow, {
        "workflow_id": "trf-a-12345678", "action_id": "act-b-12345678", "status": "dismissed",
    })
    assert unchanged["stage"] == "DRAFT_OPENED"


def test_frontend_draft_receipt_must_match_both_workflow_and_action_ids():
    workflow = make_workflow()
    workflow.update({"workflow_id": "trf-expected-123", "action_id": "act-expected-123", "stage": "DRAFT_REQUESTED"})
    mismatched = apply_frontend_draft_result(workflow, {
        "status": "draft_opened",
        "workflow_id": "trf-other-123",
        "action_id": "act-expected-123",
    })
    assert mismatched["stage"] == "DRAFT_REJECTED"
    assert mismatched["last_action_status"] == "rejected"


def test_frontend_draft_receipt_without_ids_cannot_open_workflow():
    workflow = make_workflow()
    workflow.update({"workflow_id": "trf-expected-456", "action_id": "act-expected-456", "stage": "DRAFT_REQUESTED"})
    missing_ids = apply_frontend_draft_result(workflow, {"status": "draft_opened"})
    assert missing_ids["stage"] == "DRAFT_REJECTED"


def test_missing_currency_preserves_slots_and_asks_currency_and_source_together():
    assessment = {
        "intent_id": "TRANSFER_PREPARATION",
        "slots": {"amount": 50},
        "entities": {"beneficiary": "Lucas"},
    }
    context_with_selected_eur_account = {**CONTEXT, "selected_account_id": "acc-001"}
    workflow = build_transfer_workflow(assessment, context_with_selected_eur_account)
    assert workflow["amount"] == "50"
    assert workflow["currency"] == ""
    assert workflow["beneficiary_query"] == "Lucas"

    lucas = {
        "contact_id": "ct-lucas-meyer", "full_name": "Lucas Meyer",
        "bank_name": "Meridian Bank", "currency": "EUR", "status": "ACTIVE",
    }
    status, workflow = resolve_search_result(workflow, [lucas], context_with_selected_eur_account)
    assert status == "AWAITING_CURRENCY"
    assert workflow["amount"] == "50"
    assert workflow["contact"]["contact_id"] == "ct-lucas-meyer"
    assert workflow["proposed_currency"] == "EUR"
    prompt = currency_choice_prompt(workflow)
    assert "I can prepare **50 EUR**." in prompt
    assert "Choose the account that should fund it" in prompt
    assert "Everyday account" in prompt
    assert "Savings account" in prompt
    assert "Travel wallet" in prompt
    assert "Your account choice confirms EUR" in prompt
    assert "Should I prepare" not in prompt


def test_confirming_recipient_currency_advances_to_source_account_without_losing_slots():
    assessment = {
        "intent_id": "TRANSFER_PREPARATION",
        "slots": {"amount": "50"},
        "entities": {"beneficiary": "Lucas"},
    }
    lucas = {
        "contact_id": "ct-lucas-meyer", "full_name": "Lucas Meyer",
        "bank_name": "Meridian Bank", "currency": "EUR", "status": "ACTIVE",
    }
    workflow = build_transfer_workflow(assessment, CONTEXT)
    _, workflow = resolve_search_result(workflow, [lucas], CONTEXT)
    status, workflow = resolve_currency_reply(workflow, "yes", CONTEXT)
    assert status == "AWAITING_SOURCE_ACCOUNT"
    assert workflow["amount"] == "50"
    assert workflow["currency"] == "EUR"
    assert workflow["contact"]["contact_id"] == "ct-lucas-meyer"


def test_explicit_currency_must_match_saved_recipient_currency():
    assessment = {
        "intent_id": "TRANSFER_PREPARATION",
        "slots": {"amount": "50"},
        "entities": {"beneficiary": "Lucas"},
    }
    lucas = {"contact_id": "ct-lucas-meyer", "full_name": "Lucas Meyer", "currency": "EUR", "status": "ACTIVE"}
    workflow = build_transfer_workflow(assessment, CONTEXT)
    _, workflow = resolve_search_result(workflow, [lucas], CONTEXT)
    status, workflow = resolve_currency_reply(workflow, "USD", CONTEXT)
    assert status == "CURRENCY_MISMATCH"
    assert workflow["requested_currency"] == "USD"
    assert workflow["proposed_currency"] == "EUR"


def test_named_source_account_alone_confirms_the_proposed_currency_and_selects_source():
    workflow = make_workflow(currency="")
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)

    status, updated = resolve_currency_reply(workflow, "Everyday", CONTEXT)

    assert status == "VERIFY_SOURCE_ACCOUNT"
    assert updated["currency"] == "EUR"
    assert updated["source_account_id"] == "acc-001"
    assert updated["stage"] == "VERIFYING_SOURCE_ACCOUNT"


def test_highest_balance_alone_confirms_the_proposed_currency_and_schedules_verified_read():
    workflow = make_workflow(currency="")
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)

    status, updated = resolve_currency_reply(workflow, "the one with the highest amount", CONTEXT)

    assert status == "NEEDS_VERIFIED_BALANCE"
    assert updated["currency"] == "EUR"
    assert updated["source_account_preference"] == "highest_balance"
    assert updated["stage"] == "RESOLVING_SOURCE_ACCOUNT"


def test_comparative_account_preference_waits_for_contact_and_currency_first():
    assessment = {
        "intent_id": "TRANSFER_PREPARATION",
        "slots": {"amount": "70", "source_account_preference": "highest_balance"},
        "entities": {"beneficiary": "Lucas"},
    }
    lucas = {"contact_id": "ct-lucas", "full_name": "Lucas Meyer", "currency": "EUR", "status": "ACTIVE"}
    workflow = build_transfer_workflow(assessment, CONTEXT)
    assert workflow["stage"] == "CONTACT_SEARCH_PENDING"

    status, workflow = resolve_search_result(workflow, [lucas], CONTEXT)
    assert status == "AWAITING_CURRENCY"
    assert workflow["stage"] == "AWAITING_CURRENCY"
    assert workflow["source_account_preference"] == "highest_balance"

    status, workflow = resolve_currency_reply(workflow, "yes", CONTEXT)
    assert status == "NEEDS_VERIFIED_BALANCE"
    assert workflow["stage"] == "RESOLVING_SOURCE_ACCOUNT"
    assert workflow["amount"] == "70"
    assert workflow["currency"] == "EUR"


def test_ok_confirms_only_the_pending_currency_choice_and_advances_to_source_account():
    workflow = make_workflow(currency="")
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)
    assert workflow["stage"] == "AWAITING_CURRENCY"
    status, updated = resolve_currency_reply(workflow, "ok", CONTEXT)
    assert status == "AWAITING_SOURCE_ACCOUNT"
    assert updated["currency"] == "EUR"
    assert updated["amount"] == "50"
    assert updated["contact"]["contact_id"] == "ct-john-carter"
    assert updated.get("source_account_id", "") == ""


def test_currency_confirmation_variants_are_consumed_only_in_currency_stage():
    for answer in ("ok", "okay", "yes", "sure", "sounds good", "go ahead", "please do"):
        workflow = make_workflow(currency="")
        _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)
        status, updated = resolve_currency_reply(workflow, answer, CONTEXT)
        assert status == "AWAITING_SOURCE_ACCOUNT", answer
        assert updated["currency"] == "EUR", answer
        assert updated.get("source_account_id", "") == "", answer


def test_combined_currency_confirmation_and_highest_balance_choice_resolves_in_one_reply():
    workflow = make_workflow(currency="")
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)

    status, resolved = resolve_currency_reply(workflow, "yes, highest balance", CONTEXT)

    assert status == "NEEDS_VERIFIED_BALANCE"
    assert resolved["currency"] == "EUR"
    assert resolved["source_account_preference"] == "highest_balance"
    assert resolved["stage"] == "RESOLVING_SOURCE_ACCOUNT"


def test_combined_currency_confirmation_and_named_source_resolves_in_one_reply():
    workflow = make_workflow(currency="")
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)

    status, resolved = resolve_currency_reply(workflow, "yes, Everyday", CONTEXT)

    assert status == "VERIFY_SOURCE_ACCOUNT"
    assert resolved["currency"] == "EUR"
    assert resolved["source_account_id"] == "acc-001"
    assert resolved["stage"] == "VERIFYING_SOURCE_ACCOUNT"


def test_explicit_currency_and_source_preference_can_be_supplied_together():
    workflow = make_workflow(currency="")
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)

    status, resolved = resolve_currency_reply(workflow, "EUR and the highest balance", CONTEXT)

    assert status == "NEEDS_VERIFIED_BALANCE"
    assert resolved["currency"] == "EUR"
    assert resolved["source_account_preference"] == "highest_balance"


def test_former_and_latter_resolve_only_against_the_pending_contact_candidates():
    former_workflow = make_workflow()
    _, former_workflow = resolve_search_result(former_workflow, CONTACTS, CONTEXT)
    status, former = resolve_contact_reply(former_workflow, "former", CONTEXT)
    assert status == "AWAITING_SOURCE_ACCOUNT"
    assert former["contact"]["contact_id"] == "ct-john-carter"

    latter_workflow = make_workflow()
    _, latter_workflow = resolve_search_result(latter_workflow, CONTACTS, CONTEXT)
    status, latter = resolve_contact_reply(latter_workflow, "the latter", CONTEXT)
    assert status == "AWAITING_SOURCE_ACCOUNT"
    assert latter["contact"]["contact_id"] == "ct-john-mitchell"


def test_former_contact_choice_can_include_source_preference_in_same_reply():
    workflow = make_workflow()
    _, workflow = resolve_search_result(workflow, CONTACTS, CONTEXT)

    status, resolved = resolve_contact_reply(workflow, "the former, highest balance", CONTEXT)

    assert status == "NEEDS_VERIFIED_BALANCE"
    assert resolved["contact"]["contact_id"] == "ct-john-carter"
    assert resolved["source_account_preference"] == "highest_balance"


def test_draft_opened_is_a_completed_pending_task_not_a_live_clarification():
    from types import SimpleNamespace
    from shared.pending_task import pending_task_from_message

    workflow = make_workflow()
    workflow.update({
        "stage": "DRAFT_OPENED",
        "workflow_id": "trf-opened-test",
        "action_id": "act-opened-test",
    })
    message = SimpleNamespace(type="ai", content="Your draft is open.", additional_kwargs={})
    with_workflow(message, workflow)

    task = pending_task_from_message(message)
    assert task is not None
    assert task.status == "COMPLETED"
    assert task.stage == "DRAFT_OPENED"
    assert latest_pending_workflow([message]) is None


def test_short_source_account_alias_travel_survives_missing_ui_context_and_requires_mcp_verification():
    workflow = make_workflow()
    _, workflow = resolve_search_result(workflow, [CONTACTS[1]], CONTEXT)
    assert workflow["stage"] == "AWAITING_SOURCE_ACCOUNT"
    status, selected = resolve_source_account_reply(workflow, "travel", {})
    assert status == "VERIFY_SOURCE_ACCOUNT"
    assert selected["source_account_id"] == "acc-003"
    assert selected["stage"] == "VERIFYING_SOURCE_ACCOUNT"

    verified = [
        {"account_id": "acc-001", "currency": "EUR", "status": "ACTIVE", "balance_minor": 1000},
        {"account_id": "acc-002", "currency": "EUR", "status": "ACTIVE", "balance_minor": 500},
        {"account_id": "acc-003", "currency": "EUR", "status": "ACTIVE", "balance_minor": 250},
    ]
    final_status, ready = apply_verified_source_account_selection(selected, verified)
    assert final_status == "READY_FOR_DRAFT"
    assert ready["source_account_id"] == "acc-003"


def test_named_source_account_is_rejected_if_mcp_verification_finds_currency_or_status_mismatch():
    workflow = make_workflow()
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)
    _, selected = resolve_source_account_reply(workflow, "Everyday", CONTEXT)
    status, rejected = apply_verified_source_account_selection(selected, [
        {"account_id": "acc-001", "currency": "USD", "status": "ACTIVE", "balance_minor": 1000},
    ])
    assert status == "AWAITING_SOURCE_ACCOUNT"
    assert rejected.get("source_account_id") == ""


def test_highest_balance_ranking_uses_authenticated_mcp_rows_even_without_browser_account_context():
    workflow = make_workflow(source_account_preference="highest_balance")
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)
    verified = [
        {"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR", "status": "ACTIVE", "balance_minor": 12500},
        {"account_id": "acc-002", "account_name": "Savings account", "currency": "EUR", "status": "ACTIVE", "balance_minor": 865000},
        {"account_id": "acc-003", "account_name": "Travel wallet", "currency": "EUR", "status": "ACTIVE", "balance_minor": 2500},
    ]
    status, ranked = apply_verified_account_ranking(workflow, {}, verified)
    assert status == "READY_FOR_DRAFT"
    assert ranked["source_account_id"] == "acc-002"


def test_currency_confirmation_prompt_mentions_previously_named_source_account():
    workflow = {
        "amount": "100",
        "proposed_currency": "EUR",
        "contact": {"full_name": "Lucas Meyer", "currency": "EUR"},
        "source_account_query": "Everyday account",
    }
    prompt = currency_choice_prompt(workflow)
    assert "your requested source is **everyday account**" in prompt.casefold()
    assert "I will verify that exact account" in prompt
    assert "I can prepare **100 EUR**." in prompt
    assert "No transfer draft has been opened" in prompt


def test_explicit_but_unavailable_source_is_not_replaced_by_selected_or_only_account():
    assessment = {
        "intent_id": "TRANSFER_PREPARATION",
        "slots": {"amount": "100", "source_account": "Everyday account"},
        "entities": {"beneficiary": "Lucas"},
    }
    context = {
        "accounts": [
            {"account_id": "acc-002", "account_name": "Savings account", "currency": "EUR"},
        ],
        "selected_account_id": "acc-002",
    }
    lucas = {
        "contact_id": "ct-lucas-meyer", "full_name": "Lucas Meyer",
        "bank_name": "Meridian Bank", "currency": "EUR", "status": "ACTIVE",
    }
    workflow = build_transfer_workflow(assessment, context)
    status, workflow = resolve_search_result(workflow, [lucas], context)
    assert status == "AWAITING_CURRENCY"

    status, workflow = resolve_currency_reply(workflow, "ok", context)
    assert status == "AWAITING_SOURCE_ACCOUNT"
    assert workflow.get("source_account_id") in (None, "")
    assert workflow["source_account_query"] == "Everyday account"
    assert "couldn't match your requested source **Everyday account**" in account_choice_prompt(workflow)


def test_explicit_source_account_survives_currency_confirmation_without_reasking():
    assessment = {
        "intent_id": "TRANSFER_PREPARATION",
        "slots": {"amount": "100", "source_account": "Everyday account"},
        "entities": {"beneficiary": "Lucas"},
    }
    lucas = {
        "contact_id": "ct-lucas-meyer", "full_name": "Lucas Meyer",
        "bank_name": "Meridian Bank", "currency": "EUR", "status": "ACTIVE",
    }
    workflow = build_transfer_workflow(assessment, CONTEXT)
    assert workflow["source_account_query"] == "Everyday account"

    status, workflow = resolve_search_result(workflow, [lucas], CONTEXT)
    assert status == "AWAITING_CURRENCY"
    assert workflow["source_account_query"] == "Everyday account"

    status, workflow = resolve_currency_reply(workflow, "ok", CONTEXT)
    assert status == "VERIFY_SOURCE_ACCOUNT"
    assert workflow["stage"] == "VERIFYING_SOURCE_ACCOUNT"
    assert workflow["source_account_id"] == "acc-001"
    assert workflow["currency"] == "EUR"

    # The ID is still only a selection candidate until a fresh authenticated
    # MCP account list verifies currency, active status and account identity.
    status, verified = apply_verified_source_account_selection(workflow, [
        {"account_id": "acc-001", "currency": "EUR", "status": "ACTIVE", "balance_minor": 2_013_080},
    ])
    assert status == "READY_FOR_DRAFT"
    assert verified["stage"] == "READY_FOR_DRAFT"


def test_source_account_slot_must_be_anchored_to_customer_text_and_catalogue():
    import json
    from services.agent_runtime.interpretation import validate_interpretation_assessment

    payload = {
        "intent_id": "TRANSFER_PREPARATION",
        "intent_confidence": 0.94,
        "slot_confidence": 0.96,
        "entity_confidence": 0.92,
        "capability_id": "banking.contacts.search",
        "capability_selection_confidence": 0.94,
        "resolution": "READY",
        "slots": {"amount": "100", "source_account": "Everyday account"},
        "entities": {"beneficiary": "Lucas"},
        "evidence": [
            {"field": "intent", "source": "current_turn", "quote": "Send 100 to Lucas from everyday"},
            {"field": "amount", "source": "current_turn", "quote": "100"},
            {"field": "beneficiary", "source": "current_turn", "quote": "Lucas"},
        ],
    }
    sources = {
        "current_turn": "Send 100 to Lucas from everyday",
        "conversation": "Send 100 to Lucas from everyday",
        "application_context": json.dumps({"accounts": ACCOUNTS}),
    }
    validated, decision = validate_interpretation_assessment(
        payload, expected_capability_id="banking.contacts.search", sources=sources,
    )
    assert validated is not None
    assert decision["decision"] == "PASS"
    assert any(item.field == "source_account" and item.source == "current_turn" for item in validated.evidence)


def test_explicit_transfer_request_enters_the_single_typed_workflow():
    workflow = build_explicit_transfer_workflow("send 50 EUR to Lucas", CONTEXT)
    assert workflow is not None
    assert workflow["workflow_type"] == "TRANSFER_PREPARATION"
    assert workflow["stage"] == "CONTACT_SEARCH_PENDING"
    assert workflow["amount"] == "50"
    assert workflow["currency"] == "EUR"
    assert workflow["beneficiary_query"] == "Lucas"


def test_explicit_transfer_without_currency_preserves_beneficiary_and_amount():
    workflow = build_explicit_transfer_workflow("send 50 to Lucas", CONTEXT)
    assert workflow is not None
    assert workflow["amount"] == "50"
    assert workflow["currency"] == ""
    assert workflow["beneficiary_query"] == "Lucas"
    assert workflow["stage"] == "CONTACT_SEARCH_PENDING"


def test_explicit_highest_balance_preference_is_normalized_and_not_selected_from_ui_context():
    workflow = build_explicit_transfer_workflow(
        "send 50 EUR to John from account with the highest amount", CONTEXT,
    )
    assert workflow is not None
    assert workflow["amount"] == "50"
    assert workflow["currency"] == "EUR"
    assert workflow["beneficiary_query"] == "John"
    assert workflow["source_account_preference"] == "highest_balance"
    assert workflow["source_account_id"] == ""


def test_explicit_highest_balance_preference_survives_ambiguous_john_and_former_reply():
    workflow = build_explicit_transfer_workflow(
        "send 50 EUR to John from the account with the highest amount", CONTEXT,
    )
    assert workflow is not None

    status, awaiting_contact = resolve_search_result(workflow, CONTACTS, CONTEXT)
    assert status == "AWAITING_CONTACT"
    assert awaiting_contact["source_account_preference"] == "highest_balance"

    status, selected = resolve_contact_reply(awaiting_contact, "former", CONTEXT)
    assert status == "NEEDS_VERIFIED_BALANCE"
    assert selected["contact"]["contact_id"] == "ct-john-carter"
    assert selected["source_account_preference"] == "highest_balance"
    assert selected["stage"] == "RESOLVING_SOURCE_ACCOUNT"
    assert selected["source_account_id"] == ""


def test_explicit_account_alias_resolves_against_account_catalogue_only():
    workflow = build_explicit_transfer_workflow("send 50 EUR to John from Everyday account", CONTEXT)
    assert workflow is not None
    assert workflow["source_account_id"] == "acc-001"
    assert workflow["source_account_preference"] == ""


def test_latest_pending_transfer_does_not_fall_through_typed_navigation_to_legacy_transfer_metadata():
    from types import SimpleNamespace
    from shared.pending_task import PendingTaskContext, attach_pending_task

    stale_workflow = {"workflow_type": "TRANSFER_PREPARATION", "stage": "AWAITING_CONTACT", "workflow_id": "trf-old"}
    assistant = SimpleNamespace(type="ai", content="Which workspace should I open?", additional_kwargs={})
    assistant.additional_kwargs["oraiflow_transfer_workflow"] = stale_workflow
    attach_pending_task(assistant, PendingTaskContext(
        task_id="nav-new", domain="navigation", intent_id="WORKSPACE_NAVIGATION",
        stage="AWAITING_DESTINATION", state={"destination_query": "take me there"},
    ))
    assert latest_pending_workflow([assistant]) is None


def test_new_explicit_transfer_replaces_an_older_pending_transfer_task():
    from types import SimpleNamespace

    old = build_transfer_workflow(
        {"slots": {"amount": "25", "currency": "EUR"}, "entities": {"beneficiary": "Anna"}},
        CONTEXT,
    )
    old_message = SimpleNamespace(type="ai", content="Which Anna?", additional_kwargs={})
    with_workflow(old_message, old)
    new = build_explicit_transfer_workflow("send 50 EUR to Lucas", CONTEXT)
    assert new is not None
    new_message = SimpleNamespace(type="ai", content="", additional_kwargs={})
    with_workflow(new_message, new)
    current = [old_message, SimpleNamespace(type="human", content="send 50 EUR to Lucas"), new_message]
    active = latest_pending_workflow(current)
    assert active is not None
    assert active["workflow_id"] == new["workflow_id"]
    assert active["beneficiary_query"] == "Lucas"


def test_unknown_explicit_source_account_is_preserved_and_not_replaced_by_selected_account():
    context = {**CONTEXT, "selected_account_id": "acc-001"}
    workflow = build_explicit_transfer_workflow(
        "send 50 EUR to John from my Holiday account", context,
    )
    assert workflow is not None
    assert workflow["source_account_query"] == "my Holiday account"
    assert workflow["source_account_id"] == ""
    status, resolved = resolve_search_result(workflow, [CONTACTS[0]], context)
    assert status == "AWAITING_SOURCE_ACCOUNT"
    assert resolved["source_account_query"] == "my Holiday account"
    assert resolved["source_account_id"] == ""


def test_one_source_choice_reply_can_confirm_currency_without_separate_yes():
    workflow = make_workflow(currency="")
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)

    prompt = currency_choice_prompt(workflow)
    assert "Your account choice confirms EUR" in prompt
    assert "No transfer draft has been opened" in prompt

    status, resolved = resolve_currency_reply(workflow, "the one with the highest amount", CONTEXT)

    assert status == "NEEDS_VERIFIED_BALANCE"
    assert resolved["currency"] == "EUR"
    assert resolved["source_account_preference"] == "highest_balance"
    assert resolved["stage"] == "RESOLVING_SOURCE_ACCOUNT"


def test_previously_supplied_source_preference_is_not_reasked_in_currency_prompt():
    workflow = make_workflow(currency="", source_account_preference="highest_balance")
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)

    prompt = currency_choice_prompt(workflow)

    assert "highest-balance preference" in prompt
    assert "Reply **yes** to confirm EUR" in prompt
    assert "Your account choice confirms EUR" not in prompt


def test_negated_source_preference_does_not_select_highest_balance():
    workflow = make_workflow(currency="")
    _, workflow = resolve_search_result(workflow, [CONTACTS[0]], CONTEXT)

    status, updated = resolve_currency_reply(workflow, "not highest balance", CONTEXT)

    assert status == "UNRELATED"
    assert updated.get("source_account_preference") in (None, "")
    assert updated["stage"] == "AWAITING_CURRENCY"
