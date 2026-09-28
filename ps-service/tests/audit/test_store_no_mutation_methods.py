"""Structural proof that `AuditStore` exposes no mutation/deletion method (issue #147, AC-BI-013).

Fast, hermetic -- no Postgres, no instantiation. AC-BI-013 requires "the
codebase is inspected" to show no `UPDATE`/`DELETE` capability exists on
`AuditStore` at all -- this test checks that structurally (the `Protocol`'s
and the real implementation's own public method names), independent of
whatever behavioral tests also happen to run against a real database.

Slice 3 added `record_standalone` (still an insert-only write, just one that
opens its own connection instead of reusing the caller's cursor). Slice 4
adds `query` -- a pure read, not a mutation -- so it belongs in the expected
set too, still with no `update`/`delete`/`mutate` method anywhere; re-verify
again at the end of every later slice that no new method ever contains one
of those substrings.
"""

from __future__ import annotations

from ps_service.audit.store import AuditStore, PsycopgAuditStore

_FORBIDDEN_METHOD_NAME_SUBSTRINGS = ("update", "delete", "mutate")
_EXPECTED_METHOD_NAMES = {"record", "record_standalone", "query"}


def _public_method_names(target: type[object]) -> set[str]:
    return {name for name in vars(target) if not name.startswith("_")}


def test_audit_store_protocol_exposes_no_update_or_delete_method() -> None:
    """`AuditStore`'s `Protocol` defines only insert-only writes -- no update/delete/mutate."""
    method_names = _public_method_names(AuditStore)

    assert method_names == _EXPECTED_METHOD_NAMES
    for method_name in method_names:
        assert not any(
            forbidden in method_name.lower() for forbidden in _FORBIDDEN_METHOD_NAME_SUBSTRINGS
        )


def test_psycopg_audit_store_exposes_no_update_or_delete_method() -> None:
    """`PsycopgAuditStore`, the real implementation, matches the `Protocol` exactly."""
    method_names = _public_method_names(PsycopgAuditStore)

    assert method_names == _EXPECTED_METHOD_NAMES
    for method_name in method_names:
        assert not any(
            forbidden in method_name.lower() for forbidden in _FORBIDDEN_METHOD_NAME_SUBSTRINGS
        )
