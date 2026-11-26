import pytest

from shared.identity import IdentityContext, require_capability


def test_capability_grant_is_explicit():
    identity = IdentityContext("user-001", "tenant-demo", "local-demo-config", frozenset({"accounts:read"}))
    require_capability(identity, "accounts:read")
    with pytest.raises(PermissionError):
        require_capability(identity, "payments:confirm")


def test_demo_profile_edit_capability_is_explicit_and_separate_from_identity(monkeypatch):
    from shared.identity import current_demo_identity

    monkeypatch.delenv("DEMO_USER_ID", raising=False)
    monkeypatch.delenv("DEMO_TENANT_ID", raising=False)
    identity = current_demo_identity()
    require_capability(identity, "profile:update")
    assert identity.subject == "user-001"
    assert identity.tenant_id == "tenant-demo"
