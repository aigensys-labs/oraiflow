import json

from shared.data_policy import redact_for_model, sanitize_mcp_tool_output


def test_redacts_iban_card_number_and_credential_assignments():
    source = "IBAN DE89370400440532013000 card 4111 1111 1111 1111 api_key=sk-example123"
    result = redact_for_model(source)
    assert "DE89370400440532013000" not in result
    assert "4111 1111 1111 1111" not in result
    assert "sk-example123" not in result
    assert "[REDACTED_IBAN]" in result
    assert "[REDACTED_PAYMENT_CARD_NUMBER]" in result


def test_preserves_non_sensitive_banking_text():
    source = "Transfer to John Carter for EUR 25.00"
    assert redact_for_model(source) == source


def test_mcp_tool_output_is_labeled_untrusted_and_redacted():
    raw = json.dumps({
        "description": "Payment to CZ6508000000192000145399",
        "amount_minor": -2500,
        "currency": "EUR",
    })
    safe = sanitize_mcp_tool_output(raw)
    envelope = json.loads(safe)
    assert envelope["trust_class"] == "untrusted_banking_data"
    assert "CZ6508000000192000145399" not in safe
    assert "[REDACTED_IBAN]" in safe
    assert envelope["data"]["amount_minor"] == -2500
