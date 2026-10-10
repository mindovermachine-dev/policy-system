"""Structural proof that the graph log store exposes no rewrite method (issue #205, AC-BI-005).

Fast, hermetic: checks the public method names of the `GraphLogStore` Protocol and of the real
implementation. The database refuses the rewrite regardless (see `test_provision_live.py`); this
test proves the store never offers one. Same idiom as the audit store's equivalent test.
"""

from __future__ import annotations

from ps_service.graph_gateway.store import GraphLogStore, PsycopgGraphLogStore

_FORBIDDEN_NAME_PARTS = ("update", "delete", "remove", "truncate", "drop", "overwrite", "replace")
_ALLOWED_MUTATIONS = {
    "append_group",
    "append_group_standalone",
    "advance_applied_position",
    "record_digest_checkpoint",
}


def _public_method_names(target: type[object]) -> set[str]:
    return {name for name in vars(target) if not name.startswith("_")}


def test_store_public_surface_has_no_update_delete_or_truncate_method() -> None:
    for target in (GraphLogStore, PsycopgGraphLogStore):
        for name in _public_method_names(target):
            assert not any(part in name.lower() for part in _FORBIDDEN_NAME_PARTS), name


def test_store_mutation_methods_are_only_the_append_style_ones() -> None:
    for target in (GraphLogStore, PsycopgGraphLogStore):
        names = _public_method_names(target)
        mutating = {name for name in names if name.startswith(("append", "advance", "record"))}
        assert mutating <= _ALLOWED_MUTATIONS


def test_protocol_and_implementation_expose_the_same_public_methods() -> None:
    assert _public_method_names(GraphLogStore) == _public_method_names(PsycopgGraphLogStore)


_EXPECTED_PUBLIC_SURFACE = {
    "append_group",
    "append_group_standalone",
    "read_entries",
    "last_position",
    "read_groups_by_audit_event",
    "read_applied_position",
    "advance_applied_position",
    "read_digest_checkpoint",
    "read_highest_checkpoint_at_or_below",
    "record_digest_checkpoint",
    "graphs_with_pending_entries",
    "logged_graphs",
}


def test_store_public_surface_is_exactly_the_documented_operations() -> None:
    # Any new public method must be added here on purpose: this is the guard that the store
    # never grows an update/delete path for the log, payload or checkpoint tables.
    for target in (GraphLogStore, PsycopgGraphLogStore):
        assert _public_method_names(target) == _EXPECTED_PUBLIC_SURFACE
