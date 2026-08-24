"""Tests for ps_service.domain_mapper's package front door (__init__.py)."""

from __future__ import annotations


def test_public_actions_importable_from_package_root() -> None:
    """PLAN_REVIEWED.md §1: __init__.py re-exports both public actions, so
    callers can import from the package root, not just the submodules."""
    from ps_service.domain_mapper import (
        derive_obligations_and_capabilities,
        extract_roles_and_requirements,
    )
    from ps_service.domain_mapper.derivation import (
        derive_obligations_and_capabilities as derive_direct,
    )
    from ps_service.domain_mapper.extraction import (
        extract_roles_and_requirements as extract_direct,
    )

    assert extract_roles_and_requirements is extract_direct
    assert derive_obligations_and_capabilities is derive_direct
