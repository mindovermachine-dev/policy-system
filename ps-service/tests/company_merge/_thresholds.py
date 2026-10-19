"""Shared Capability review-floor value for tests that are not about the floor.

`REVIEW_THRESHOLD = 0.0` is legacy-equivalent: the floor is configured and active, but every
scored best neighbour clears it, so a test written before the floor existed keeps its behaviour.
A test that exercises the floor states its own explicit value instead of using this constant.
"""

from __future__ import annotations

REVIEW_THRESHOLD = 0.0

# A floor of exactly zero: the pair is recorded even at a similarity of 0.0. Tests that assert a
# near miss at a zero or arbitrarily low score, or compare against the frozen no-floor reference
# writer, state it by name so they do not depend on the shared REVIEW_THRESHOLD value.
NO_FLOOR_REVIEW_THRESHOLD = 0.0
