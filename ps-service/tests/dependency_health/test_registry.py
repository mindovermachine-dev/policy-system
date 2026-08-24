"""Unit tests for `ps_service.dependency_health.registry`."""

from __future__ import annotations

from ps_service.dependency_health import (
    all_healthy,
    is_healthy,
    mark_healthy,
    mark_unhealthy,
    reset_for_tests,
)


def test_dependency_with_no_recorded_call_is_healthy() -> None:
    assert is_healthy("falkordb") is True


def test_mark_unhealthy_makes_is_healthy_false() -> None:
    mark_unhealthy("falkordb", error=ConnectionError("boom"))

    assert is_healthy("falkordb") is False


def test_mark_healthy_after_unhealthy_self_heals() -> None:
    mark_unhealthy("falkordb", error=ConnectionError("boom"))
    mark_healthy("falkordb")

    assert is_healthy("falkordb") is True


def test_all_healthy_is_false_if_any_named_dependency_is_unhealthy() -> None:
    mark_healthy("falkordb")
    mark_unhealthy("llm_interface", error=RuntimeError("provider down"))

    assert all_healthy(["falkordb", "llm_interface"]) is False


def test_all_healthy_is_true_when_every_named_dependency_is_healthy() -> None:
    mark_healthy("falkordb")
    mark_healthy("llm_interface")

    assert all_healthy(["falkordb", "llm_interface"]) is True


def test_marking_one_dependency_unhealthy_does_not_affect_another() -> None:
    mark_unhealthy("falkordb", error=ConnectionError("boom"))

    assert is_healthy("llm_interface") is True


def test_reset_for_tests_clears_all_recorded_state() -> None:
    mark_unhealthy("falkordb", error=ConnectionError("boom"))
    mark_unhealthy("llm_interface", error=RuntimeError("provider down"))

    reset_for_tests()

    assert is_healthy("falkordb") is True
    assert is_healthy("llm_interface") is True
