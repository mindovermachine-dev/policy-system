"""Tests for ps_test_plugin.server: hello_world()."""

from __future__ import annotations

from ps_test_plugin.server import (
    _CITIES,  # pyright: ignore[reportPrivateUsage] -- test pins the exact known-city set
    hello_world,
)


def test_hello_world_greets_from_one_of_the_known_cities() -> None:
    """The greeting always has the fixed shape and names a city from the known set."""
    greeting = hello_world()

    assert greeting.startswith("Hello from ")
    assert greeting.endswith("!")
    assert greeting.removeprefix("Hello from ").removesuffix("!") in _CITIES


def test_hello_world_varies_across_calls() -> None:
    """Repeated calls are not pinned to a single city (flaky only if `_CITIES` shrinks to 1)."""
    greetings = {hello_world() for _ in range(50)}

    assert len(greetings) > 1
