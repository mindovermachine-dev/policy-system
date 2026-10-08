"""Shared audit-actor resolution (issue #195, AC-BI-001 helper half)."""

from __future__ import annotations

import pytest

from ps_service.audit.actor import LOCAL_TEST_BYPASS_AUDIT_ACTOR, resolve_audit_actor
from ps_service.audit.errors import AuditActorUnresolvedError

_VERIFIED = ("user-sub", "https://idp.example.com")


def test_resolve_audit_actor_returns_the_verified_actor_unchanged() -> None:
    assert resolve_audit_actor(_VERIFIED, is_local_test_bypass_active=False) == _VERIFIED
    assert resolve_audit_actor(_VERIFIED, is_local_test_bypass_active=True) == _VERIFIED


def test_resolve_audit_actor_returns_sentinel_pair_when_actor_missing_and_bypass_active() -> None:
    assert resolve_audit_actor(None, is_local_test_bypass_active=True) == (
        LOCAL_TEST_BYPASS_AUDIT_ACTOR,
        LOCAL_TEST_BYPASS_AUDIT_ACTOR,
    )


def test_resolve_audit_actor_raises_when_actor_missing_and_bypass_inactive() -> None:
    with pytest.raises(AuditActorUnresolvedError):
        resolve_audit_actor(None, is_local_test_bypass_active=False)


def test_sentinel_value_is_system_local_test_bypass() -> None:
    assert LOCAL_TEST_BYPASS_AUDIT_ACTOR == "system:local-test-bypass"
