import json
from types import SimpleNamespace

from services.agent_runtime.capability_results import (
    capability_result_diagnostic,
    verified_list_result_diagnostic,
)
from shared.contracts.registry import create_result_envelope


def tool_message(name: str, envelope: dict):
    return SimpleNamespace(type="tool", name=name, content=json.dumps(envelope))


def human_message(text="send 50 EUR to John"):
    return SimpleNamespace(type="human", content=text)


def test_valid_contact_search_result_is_preserved_as_untrusted_data():
    contact = {
        "contact_id": "contact-001",
        "full_name": "John Carter",
        "bank_name": "Northstar Bank",
        "account_reference_masked": "DEMO ···· 1042",
        "currency": "EUR",
        "status": "ACTIVE",
    }
    envelope = create_result_envelope(
        "banking.contacts.search",
        "SUCCESS",
        data=[contact],
        trace_id="test-trace",
    )
    messages = [human_message(), tool_message("search_contacts", envelope)]

    seen, rows, reason, outcome, error_code = verified_list_result_diagnostic(
        messages,
        "banking.contacts.search",
        trace_id="test-trace",
    )
    assert seen is True
    assert rows == [contact]
    assert reason is None
    assert outcome == "SUCCESS"
    assert error_code is None


def test_empty_contact_search_is_a_valid_empty_result_not_a_connector_failure():
    envelope = create_result_envelope(
        "banking.contacts.search",
        "EMPTY",
        data=[],
        trace_id="test-trace",
    )
    messages = [human_message(), tool_message("search_contacts", envelope)]

    seen, rows, reason, outcome, error_code = verified_list_result_diagnostic(
        messages,
        "banking.contacts.search",
        trace_id="test-trace",
    )
    assert seen is True
    assert rows == []
    assert reason is None
    assert outcome == "EMPTY"
    assert error_code is None


def test_contact_permission_rejection_keeps_the_actual_error_code():
    envelope = create_result_envelope(
        "banking.contacts.search",
        "REJECTED",
        error={"code": "CAPABILITY_NOT_PERMITTED"},
        trace_id="test-trace",
    )
    messages = [human_message(), tool_message("search_contacts", envelope)]

    seen, rows, reason, outcome, error_code = verified_list_result_diagnostic(
        messages,
        "banking.contacts.search",
        trace_id="test-trace",
    )
    assert seen is True
    assert rows is None
    assert reason == "OUTCOME_REJECTED"
    assert outcome == "REJECTED"
    assert error_code == "CAPABILITY_NOT_PERMITTED"


def test_contact_connector_unavailable_is_not_misreported_as_no_contacts():
    envelope = create_result_envelope(
        "banking.contacts.search",
        "UNAVAILABLE",
        error={"code": "TRANSPORT_UNAVAILABLE", "retryable": True},
        trace_id="test-trace",
    )
    messages = [human_message(), tool_message("search_contacts", envelope)]

    seen, envelope_result, reason, outcome, error_code = capability_result_diagnostic(
        messages,
        "banking.contacts.search",
        trace_id="test-trace",
    )
    assert seen is True
    assert envelope_result is None
    assert reason == "OUTCOME_UNAVAILABLE"
    assert outcome == "UNAVAILABLE"
    assert error_code == "TRANSPORT_UNAVAILABLE"


def test_invalid_contact_envelope_is_classified_separately():
    messages = [
        human_message(),
        SimpleNamespace(type="tool", name="search_contacts", content="not-json"),
    ]
    seen, rows, reason, outcome, error_code = verified_list_result_diagnostic(
        messages,
        "banking.contacts.search",
        trace_id="test-trace",
    )
    assert seen is True
    assert rows is None
    assert reason == "INVALID_RESULT_ENVELOPE"
    assert outcome is None
    assert error_code is None


def test_missing_contact_result_is_distinct_from_empty_search():
    seen, rows, reason, outcome, error_code = verified_list_result_diagnostic(
        [human_message()],
        "banking.contacts.search",
        trace_id="test-trace",
    )
    assert seen is False
    assert rows is None
    assert reason == "RESULT_NOT_FOUND"
    assert outcome is None
    assert error_code is None


def test_contact_result_trust_class_mismatch_is_not_reported_as_generic_envelope_error():
    contact = {
        "contact_id": "contact-001",
        "full_name": "John Carter",
        "bank_name": "Northstar Bank",
        "account_reference_masked": "DEMO ···· 1042",
        "currency": "EUR",
        "status": "ACTIVE",
    }
    envelope = create_result_envelope(
        "banking.contacts.search",
        "SUCCESS",
        data=[contact],
        trace_id="test-trace",
    )
    envelope["trust_class"] = "connector_error"
    seen, rows, reason, outcome, error_code = verified_list_result_diagnostic(
        [human_message(), tool_message("search_contacts", envelope)],
        "banking.contacts.search",
        trace_id="test-trace",
    )
    assert seen is True
    assert rows is None
    assert reason == "TRUST_CLASS_MISMATCH"
    assert outcome == "SUCCESS"
    assert error_code is None


def test_contact_result_wrong_top_level_data_type_has_specific_diagnostic():
    contact = {
        "contact_id": "contact-001",
        "full_name": "John Carter",
        "bank_name": "Northstar Bank",
        "account_reference_masked": "DEMO ···· 1042",
        "currency": "EUR",
        "status": "ACTIVE",
    }
    envelope = create_result_envelope(
        "banking.contacts.search",
        "SUCCESS",
        data=[contact],
        trace_id="test-trace",
    )
    envelope["data"] = {"contact": contact}
    seen, rows, reason, outcome, error_code = verified_list_result_diagnostic(
        [human_message(), tool_message("search_contacts", envelope)],
        "banking.contacts.search",
        trace_id="test-trace",
    )
    assert seen is True
    assert rows is None
    assert reason == "INVALID_DATA_SHAPE"
    assert outcome == "SUCCESS"
    assert error_code is None


def test_contact_result_non_object_record_has_specific_diagnostic():
    contact = {
        "contact_id": "contact-001",
        "full_name": "John Carter",
        "bank_name": "Northstar Bank",
        "account_reference_masked": "DEMO ···· 1042",
        "currency": "EUR",
        "status": "ACTIVE",
    }
    envelope = create_result_envelope(
        "banking.contacts.search",
        "SUCCESS",
        data=[contact],
        trace_id="test-trace",
    )
    envelope["data"] = ["not-a-contact-record"]
    seen, rows, reason, outcome, error_code = verified_list_result_diagnostic(
        [human_message(), tool_message("search_contacts", envelope)],
        "banking.contacts.search",
        trace_id="test-trace",
    )
    assert seen is True
    assert rows is None
    assert reason == "INVALID_DATA_RECORD"
    assert outcome == "SUCCESS"
    assert error_code is None


def test_contact_result_invalid_record_fields_are_rejected_by_occf_schema():
    contact = {
        "contact_id": "contact-001",
        "full_name": "John Carter",
        "bank_name": "Northstar Bank",
        "account_reference_masked": "DEMO ···· 1042",
        "currency": "EUR",
        "status": "ACTIVE",
    }
    envelope = create_result_envelope(
        "banking.contacts.search",
        "SUCCESS",
        data=[contact],
        trace_id="test-trace",
    )
    envelope["data"] = [{**contact, "unexpected": "value"}]
    seen, rows, reason, outcome, error_code = verified_list_result_diagnostic(
        [human_message(), tool_message("search_contacts", envelope)],
        "banking.contacts.search",
        trace_id="test-trace",
    )
    assert seen is True
    assert rows is None
    assert reason == "INVALID_DATA_SCHEMA"
    assert outcome == "SUCCESS"
    assert error_code is None
