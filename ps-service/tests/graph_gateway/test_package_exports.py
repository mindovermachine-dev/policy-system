"""The package front door exposes the gateway's public surface (issue #206, S14)."""

from __future__ import annotations

from ps_service import graph_gateway

_GATEWAY_SURFACE = (
    "GraphWriteGateway",
    "GatewaySettings",
    "build_default_graph_write_gateway",
    "StagedSubmission",
    "MutationGroup",
    "GroupOutcome",
    "CatchUpResult",
    "RecoveryResult",
    "UpsertNode",
    "UpsertEdge",
    "MergeProperty",
    "RemoveProperty",
    "DeleteNode",
    "DeleteEdge",
    "NodeRef",
    "ExpectedPosition",
    "GraphWriteRejectedError",
    "UnlistedNameError",
    "MissingTargetError",
    "StaleGraphStateError",
    "GraphUnavailableError",
    "GraphApplyError",
    "GraphApplyBlockedError",
)


def test_front_door_exports_the_gateway_surface() -> None:
    for name in _GATEWAY_SURFACE:
        assert name in graph_gateway.__all__, name
        assert getattr(graph_gateway, name) is not None, name


def test_every_exported_name_resolves() -> None:
    for name in graph_gateway.__all__:
        assert hasattr(graph_gateway, name), name


def test_front_door_docstring_describes_the_gateway_not_only_the_store() -> None:
    doc = graph_gateway.__doc__ or ""

    assert "separate sub-issue" not in doc
    assert "currently holds the store half" not in doc
    for needle in ("#206", "log-first", "no writer", "provision"):
        assert needle in doc, needle
