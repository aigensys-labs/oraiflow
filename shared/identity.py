"""Server-derived identity context for the local demo.

This is deliberately NOT an OIDC/JWT implementation. The demo customer and
 tenant are resolved from server configuration, never from model arguments or
untrusted request headers. Production deployments must replace this resolver
with a verified OIDC resource-server implementation.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class IdentityContext:
    """Identity and authorization attributes for the single synthetic demo user."""

    subject: str
    tenant_id: str
    authentication_method: str
    capabilities: frozenset[str]


def current_demo_identity() -> IdentityContext:
    """Resolve the configured synthetic demo identity."""
    return IdentityContext(
        subject=os.getenv("DEMO_USER_ID", "user-001"),
        tenant_id=os.getenv("DEMO_TENANT_ID", "tenant-demo"),
        authentication_method="local-demo-config",
        capabilities=frozenset({
            "accounts:read",
            "profile:update",
            "transactions:read",
            "contacts:read",
            "loans:read",
            "financial_health:read",
            "payments:preview",
            "payments:confirm",
            "payments:read",
            "audit:read",
        }),
    )


def require_capability(identity: IdentityContext, capability: str) -> None:
    """Enforce that the demo identity is allowed to invoke the requested capability."""
    if capability not in identity.capabilities:
        raise PermissionError(f"Missing required capability: {capability}")
