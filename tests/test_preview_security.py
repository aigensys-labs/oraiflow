from datetime import datetime, timezone

from shared.previews import preview_digest, verify_preview_digest


def sample_preview():
    return {
        "preview_id": "c675e75d-45a8-4abc-a42a-7bc00b2eea11",
        "user_id": "user-001",
        "tenant_id": "tenant-demo",
        "from_account_id": "acc-001",
        "beneficiary": "John Carter",
        "amount_minor": 2500,
        "currency": "EUR",
        "fee_minor": 0,
        "total_minor": 2500,
        "policy_version": "demo-transfer-policy-v1",
        "policy_decision": "APPROVED",
        "policy_details": {
            "max_transfer_minor": 100000,
            "available_balance_minor": 1243080,
            "fee_minor": 0,
            "total_minor": 2500,
        },
        "expires_at": datetime(2026, 11, 19, 13, 0, tzinfo=timezone.utc),
    }


def test_preview_digest_is_stable_and_verifiable():
    preview = sample_preview()
    digest = preview_digest(preview, "test-signing-secret")
    assert digest == preview_digest(preview, "test-signing-secret")
    assert verify_preview_digest(preview, digest, "test-signing-secret")


def test_preview_digest_binds_beneficiary_amount_and_tenant():
    preview = sample_preview()
    digest = preview_digest(preview, "test-signing-secret")
    for field, changed in (("beneficiary", "Jordan Li"), ("beneficiary_contact_id", "ct-contact-123456"), ("amount_minor", 2501), ("tenant_id", "tenant-other")):
        tampered = {**preview, field: changed}
        assert not verify_preview_digest(tampered, digest, "test-signing-secret")


def test_preview_digest_binds_persisted_policy_details():
    preview = sample_preview()
    digest = preview_digest(preview, "test-signing-secret")
    tampered = {**preview, "policy_details": {**preview["policy_details"], "max_transfer_minor": 90000}}
    assert not verify_preview_digest(tampered, digest, "test-signing-secret")


def test_preview_digest_binds_expiration_and_policy_version():
    preview = sample_preview()
    digest = preview_digest(preview, "test-signing-secret")
    tampered = {**preview, "policy_version": "demo-transfer-policy-v2"}
    assert not verify_preview_digest(tampered, digest, "test-signing-secret")
    tampered = {**preview, "expires_at": datetime(2026, 11, 19, 13, 1, tzinfo=timezone.utc)}
    assert not verify_preview_digest(tampered, digest, "test-signing-secret")
