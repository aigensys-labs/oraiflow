from shared.contact_resolution import (
    extract_contact_transfer_request,
    format_contact_choices,
    is_contact_choice_reply,
    is_contact_resolution_cancel,
    resolve_contact_choice,
)

ACCOUNTS = [
    {"account_id": "acc-001", "account_name": "Everyday account", "currency": "EUR"},
    {"account_id": "acc-002", "account_name": "Savings account", "currency": "EUR"},
    {"account_id": "acc-003", "account_name": "Travel wallet", "currency": "EUR"},
]

CONTACTS = [
    {
        "contact_id": "ct-john-carter",
        "full_name": "John Carter",
        "bank_name": "Northstar Bank",
        "account_reference_masked": "DEMO ···· 1042",
        "currency": "EUR",
        "status": "ACTIVE",
    },
    {
        "contact_id": "ct-john-mitchell",
        "full_name": "John Mitchell",
        "bank_name": "Union Financial",
        "account_reference_masked": "DEMO ···· 2781",
        "currency": "EUR",
        "status": "ACTIVE",
    },
]


def test_explicit_send_amount_to_name_is_parsed_as_untrusted_draft():
    result = extract_contact_transfer_request(
        "Send 50 EUR to John",
        {"accounts": ACCOUNTS, "selected_account_id": "acc-002"},
    )
    assert result["beneficiary_query"] == "John"
    assert result["amount"] == "50"
    assert result["currency"] == "EUR"
    assert result["from_account_id"] == "acc-002"
    assert len(result["source_account_candidates"]) == 3
    assert "beneficiary_contact_id" not in result


def test_transfer_parser_supports_currency_symbols_words_and_recipient_before_amount():
    context = {"accounts": ACCOUNTS, "selected_account_id": "acc-001"}
    assert extract_contact_transfer_request("Transfer €50 to Michael", context)["currency"] == "EUR"
    assert extract_contact_transfer_request("Send 50 euros to Michael", context)["amount"] == "50"
    assert (
        extract_contact_transfer_request("Pay Michael 50 EUR", context)["beneficiary_query"]
        == "Michael"
    )
    assert (
        extract_contact_transfer_request("Send John Carter 50 EUR", context)["beneficiary_query"]
        == "John Carter"
    )


def test_transfer_parser_fails_closed_when_currency_or_source_account_is_missing():
    assert extract_contact_transfer_request("Send 50 to John", {"accounts": ACCOUNTS}) is None
    no_accounts = extract_contact_transfer_request("Send 50 EUR to John", {"accounts": []})
    assert no_accounts is not None and no_accounts["from_account_id"] == ""
    assert no_accounts["source_account_candidates"] == []
    assert (
        extract_contact_transfer_request(
            "Send 50 EUR to my savings account", {"accounts": ACCOUNTS}
        )
        is None
    )


def test_duplicate_first_name_is_never_resolved_by_arbitrary_order():
    assert resolve_contact_choice(CONTACTS, "John") is None
    assert is_contact_choice_reply(CONTACTS, "John") is True


def test_unique_contact_can_be_resolved_by_full_name_or_bank_reference():
    assert resolve_contact_choice(CONTACTS, "John Carter") == CONTACTS[0]
    assert resolve_contact_choice(CONTACTS, "Carter") == CONTACTS[0]
    assert resolve_contact_choice(CONTACTS, "Northstar Bank") == CONTACTS[0]
    assert resolve_contact_choice(CONTACTS, "DEMO ···· 2781") == CONTACTS[1]


def test_contact_choice_supports_numeric_and_natural_ordinal_replies():
    assert resolve_contact_choice(CONTACTS, "1") == CONTACTS[0]
    assert resolve_contact_choice(CONTACTS, "option 2") == CONTACTS[1]
    assert resolve_contact_choice(CONTACTS, "the first one") == CONTACTS[0]
    assert resolve_contact_choice(CONTACTS, "the second one") == CONTACTS[1]
    assert resolve_contact_choice(CONTACTS, "the last one") == CONTACTS[1]


def test_out_of_range_selection_stays_unresolved_and_is_treated_as_choice():
    assert resolve_contact_choice(CONTACTS, "option 4") is None
    assert is_contact_choice_reply(CONTACTS, "option 4") is True


def test_unrelated_question_does_not_get_misread_as_contact_selection():
    assert is_contact_choice_reply(CONTACTS, "What is my balance?") is False


def test_cancel_and_contact_options_are_customer_safe():
    assert is_contact_resolution_cancel("Cancel") is True
    assert is_contact_resolution_cancel("Never mind") is True
    choices = format_contact_choices(CONTACTS)
    assert "John Carter" in choices and "Northstar Bank" in choices and "1042" in choices
    assert "John Mitchell" in choices and "Union Financial" in choices and "2781" in choices


def test_parser_never_defaults_to_first_of_multiple_source_accounts():
    result = extract_contact_transfer_request(
        "Send 50 EUR to John",
        {"accounts": ACCOUNTS, "selected_account_id": None},
    )
    assert result is not None
    assert result["from_account_id"] == ""
    assert len(result["source_account_candidates"]) == 3


def test_parser_uses_only_an_explicit_or_unique_currency_compatible_source_account():
    one_account = {"accounts": [ACCOUNTS[1]], "selected_account_id": None}
    unique = extract_contact_transfer_request("Send 50 EUR to John", one_account)
    assert unique["from_account_id"] == "acc-002"

    explicit = {"accounts": ACCOUNTS, "selected_account_id": None}
    parsed = extract_contact_transfer_request("Send 50 EUR from Savings account to John", explicit)
    assert parsed["from_account_id"] == "acc-002"
    assert parsed["source_account_candidates"] == [
        {
            "account_id": account["account_id"],
            "account_name": account["account_name"],
            "currency": "EUR",
        }
        for account in ACCOUNTS
    ]


def test_parser_does_not_fallback_to_another_account_when_selected_currency_differs():
    accounts = [
        {"account_id": "acc-usd", "account_name": "Dollar account", "currency": "USD"},
        {"account_id": "acc-eur", "account_name": "Euro account", "currency": "EUR"},
    ]
    parsed = extract_contact_transfer_request(
        "Send 50 EUR to John",
        {"accounts": accounts, "selected_account_id": "acc-usd"},
    )
    assert parsed is not None and parsed["from_account_id"] == ""
