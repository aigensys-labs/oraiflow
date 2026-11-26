from pydantic import ValidationError
import pytest

from services.api.main import TransferConfirmRequest


def test_confirmation_requires_displayed_preview_digest():
    common = {
        "preview_id": "c675e75d-45a8-4abc-a42a-7bc00b2eea11",
        "idempotency_key": "test-idempotency-key-001",
        "customer_confirmed": True,
        "sca_code": "123456",
    }
    with pytest.raises(ValidationError):
        TransferConfirmRequest(**common)

    request = TransferConfirmRequest(**{**common, "payload_digest": "a" * 64})
    assert request.payload_digest == "a" * 64


def test_profile_update_cannot_override_server_identity_claims():
    from services.api.main import ProfileUpdateRequest

    with pytest.raises(ValidationError):
        ProfileUpdateRequest(
            display_name="Alex Morgan", subject="attacker", tenant_id="other-tenant"
        )
