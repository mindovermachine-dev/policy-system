"""The Graph Write Gateway rig, seen from Restore.

Restore hands the gateway ONE multi-graph group and only READS `policy_system`. `RestoreRig` is
the gateway rig (in-memory log and FalkorDB fakes, the true boundaries) plus the pieces
`restore_instrument` is called with: a `db` whose `select_graph("policy_system")` is the rig's
read handle, an `open_gateway` that returns the rig's gateway and the opening audit row's event id.

`restore(artifact, ...)` is the real `restore_instrument` driven through that rig, so a test
asserts on what the gateway logged and on the graphs it applied, never on Cypher text.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

from company_merge._gateway_fakes import POLICY_SYSTEM, MergeRig, RigReadHandle

from ps_service.restore.restore_instrument import restore_instrument

if TYPE_CHECKING:
    from collections.abc import Callable

    from falkordb import FalkorDB
    from graph_gateway._fakes import InMemoryGraph

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.logging.emitter import LogEmitter
    from ps_service.restore.models import RestoreArtifact, RestoreOutcome

THRESHOLD = 0.85
OWNER = ("sub-owner-1", "https://idp.example")
ACTOR = "test-actor"


class RigDb:
    """A `FalkorDB`-shaped read opener: `policy_system` is the rig's read handle.

    The handle is made on every selection from whatever graph object the rig holds under that
    name now, as `select_graph` does on a real connection: a swap replaces the live graph.
    """

    def __init__(self, handle: Callable[[], GraphHandle]) -> None:
        self._handle = handle
        self.selected: list[str] = []

    def select_graph(self, name: str) -> GraphHandle:
        """Return the rig's `policy_system` read handle; any other graph is a test bug."""
        self.selected.append(name)
        if name != POLICY_SYSTEM:
            message = f"restore opened {name!r}, but only policy_system is ever read"
            raise AssertionError(message)
        return self._handle()


class RestoreRig(MergeRig):
    """A `MergeRig` that also runs the real `restore_instrument` against itself."""

    def __init__(self) -> None:
        super().__init__(swap=True)
        self.db = RigDb(lambda: RigReadHandle(self.graphs.open(POLICY_SYSTEM)))
        self.open_gateway_calls = 0

    def open_gateway(self) -> object:
        """What the orchestration hands the restore: the rig's gateway (counted)."""
        self.open_gateway_calls += 1
        return self.gateway

    def graph(self, name: str) -> InMemoryGraph:
        """The rig's graph `name` (created empty when it does not exist yet)."""
        return self.graphs.open(name)

    def restore(
        self,
        artifact: RestoreArtifact,
        *,
        emitter: LogEmitter | None = None,
        owner: tuple[str, str] | None = None,
        source: str | None = None,
        threshold: float = THRESHOLD,
    ) -> RestoreOutcome:
        """Run the real `restore_instrument` for `artifact` through this rig."""
        return restore_instrument(
            artifact,
            db=cast("FalkorDB", self.db),
            single_tenant_graph_name=POLICY_SYSTEM,
            similarity_threshold=threshold,
            actor=ACTOR,
            open_gateway=self.open_gateway,
            audit_event_id=self.audit_event_id,
            emitter=emitter,
            source=source,
            owner=owner,
        )


_IDS_READ = re.compile(r"^MATCH \(n:(?P<label>\w+)\) WHERE n\.id IN \$ids RETURN n\.id$")
_ANY_LABEL_READ = "UNWIND $ids AS id MATCH (n {id: id}) RETURN id"


class _Rows:
    def __init__(self, result_set: list[object]) -> None:
        self.result_set = result_set


class ScriptedLive:
    """A read-only `policy_system` handle scripted per test (a `GraphHandle`).

    Answers the batched existence reads from `existing` (label -> ids) and everything else from
    `answer(query, params)` (return `None` to fall through to an empty result). It refuses every
    write statement: Restore reads this graph and submits primitives, it never writes through it.
    """

    def __init__(
        self,
        existing: dict[str, set[str]] | None = None,
        answer: Callable[[str, dict[str, object] | None], list[object] | None] | None = None,
    ) -> None:
        self._existing = existing or {}
        self._answer = answer
        self.queries: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _Rows:
        """Answer one read; a write statement is a test failure."""
        self.queries.append(q)
        if re.match(r"^\s*(MERGE|CREATE|SET|DELETE|UNWIND \$\w+ AS \w+ (MERGE|CREATE))", q):
            message = f"restore wrote through its read handle: {q}"
            raise AssertionError(message)
        if self._answer is not None:
            scripted = self._answer(q, params)
            if scripted is not None:
                return _Rows(scripted)
        ids = cast("list[str]", (params or {}).get("ids", []))
        matched = _IDS_READ.match(q)
        if matched is not None:
            held = self._existing.get(matched.group("label"), set())
            return _Rows([[i] for i in ids if i in held])
        if q == _ANY_LABEL_READ:
            everything: set[str] = set()
            for held_ids in self._existing.values():
                everything |= held_ids
            return _Rows([[i] for i in ids if i in everything])
        return _Rows([])


UNUSED_EVENT_ID = "00000000-0000-4000-8000-000000000214"
"""An audit event id for a call that must not reach the gateway (never logged under)."""


def never_open_gateway() -> object:
    """An `open_gateway` for a call that must fail before the gateway is opened."""
    message = "the gateway was opened where the restore had to fail before touching any graph"
    raise AssertionError(message)
