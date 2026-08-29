"""Tests for `ps_service.ingestion.pipeline` (PLAN_REVIEWED.md §7 Increment 11).

Two tests, per the plan:

(a) AC-005 proof — two separate `ingest_regulatory_instrument()` calls, each inside its
own `bind_run_context()` scope (via `pipeline.py`'s own `with` block), emit
log entries carrying two distinct `run_id`s. Mirrors
`tests/llm_interface/test_route_completion_logs_run_id.py`'s
bind -> call -> read-back-from-emitted-JSON mechanism, using the same
`make_emitter`/`read_lines` fixtures (now shared at `tests/conftest.py` —
see that file's docstring for why this is the DRY-extraction point).

(b) AC-006 proof — THE B2/B3 FIX. An AST-based scan (not a substring grep)
of every `.py` file actually delivered under `ps_service/ingestion/`
(`pathlib.Path(...).rglob("*.py")`, not a hardcoded file list), asserting no
string literal naming a regulation ("CRA"/"GDPR"/"NIS2") appears inside a
conditional/branching construct's decision anywhere in that package.
Docstring prose (this file's own module docstring included, and
`pipeline.py`'s own docstring, which names all three as examples) never
trips this check — not because of a special-cased exclusion list, but
because the scan only ever walks the *decision* subtree of a conditional
construct (an `if`/`elif` test, a ternary's test, a `while`/`assert`
condition, a bare `Compare` node, a `match` statement's case patterns) —
and a docstring statement can never structurally appear inside one of those.
A `_docstring_constant_ids` exclusion is layered on top anyway, explicitly,
per PLAN_REVIEWED.md's requirement — defensive, not load-bearing given the
above, but making the "docstrings never trip this check" guarantee explicit
rather than implicit.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import ps_service.ingestion as ingestion_package
from ps_service.ingestion.models import (
    FetchedRegulatoryInstrumentStructure,
    RegulatoryInstrumentMetadata,
    StructuralEdge,
    StructuralNode,
)
from ps_service.ingestion.pipeline import ingest_regulatory_instrument
from ps_service.logging import bind_run_context

if TYPE_CHECKING:
    from ps_service.logging import LogEmitter
    from ps_service.logging.emitter import TextSink


class _MakeEmitter(Protocol):
    """Call shape of the shared `make_emitter` fixture (`tests/conftest.py`)."""

    def __call__(
        self, *, filename: str = ..., fallback: TextSink | None = ...
    ) -> tuple[LogEmitter, Path]: ...


class _ReadLines(Protocol):
    """Call shape of the shared `read_lines` fixture (`tests/conftest.py`)."""

    def __call__(self, log_path: Path) -> list[dict[str, object]]: ...


# --- shared fakes (structurally satisfy IngestionAdapter / GraphHandle) ---


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally; every scripted response is
    `[[0]]`, so `verify_structural_graph_reachable`'s total/reachable counts
    are always equal (0 == 0) for every label — no gap, no raise. Cheap and
    sufficient: this test suite's job is to prove `run_id` correlation and
    regulation-independence, not to re-prove `graph_writer`'s own Cypher
    shape (already covered by `test_graph_writer.py`).
    """

    def __init__(self) -> None:
        self.result_set: list[object] = [[0]]


class _FakeGraph:
    """Satisfies `GraphHandle` structurally, recording every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append((q, params))
        return _FakeQueryResult()


class _FakeAdapter:
    """Satisfies `IngestionAdapter` structurally: one canned
    `FetchedRegulatoryInstrumentStructure` per identifier, looked up from a dict built
    by the test — the dispatch-by-identifier lives here, in test fakery,
    never inside `pipeline.py`.
    """

    def __init__(
        self, structures_by_identifier: dict[str, FetchedRegulatoryInstrumentStructure]
    ) -> None:
        self._structures_by_identifier = structures_by_identifier

    def fetch_regulatory_instrument_structure(
        self, identifier: str
    ) -> FetchedRegulatoryInstrumentStructure:
        return self._structures_by_identifier[identifier]


def _structure(title: str) -> FetchedRegulatoryInstrumentStructure:
    metadata = RegulatoryInstrumentMetadata(
        title=title,
        jurisdiction="EU",
        effective_date=date(2027, 12, 11),
        version="1.0",
        status="active",
        source_type="external",
        instrument_type="regulation",
    )
    nodes = (
        StructuralNode(
            "ARTICLE", f"{title}#art_1", {"text": "t", "citation_ref": "Art. 1", "order": 1}
        ),
    )
    edges = (StructuralEdge("RegulatoryInstrument", f"{title}-id", "ARTICLE", f"{title}#art_1"),)
    return FetchedRegulatoryInstrumentStructure(metadata=metadata, nodes=nodes, edges=edges)


# --- (a) AC-005: two calls, two distinct run_ids ---------------------------


def test_ingest_regulatory_instrument_two_calls_emit_two_distinct_run_ids(
    make_emitter: _MakeEmitter, read_lines: _ReadLines
) -> None:
    emitter, log_path = make_emitter()
    adapter = _FakeAdapter(
        {
            "IDENTIFIER_ONE": _structure("Fixture One"),
            "IDENTIFIER_TWO": _structure("Fixture Two"),
        }
    )

    result_one = ingest_regulatory_instrument(
        "IDENTIFIER_ONE",
        "ONE",
        version="1.0",
        adapter=adapter,
        graph=_FakeGraph(),
        emitter=emitter,
    )
    result_two = ingest_regulatory_instrument(
        "IDENTIFIER_TWO",
        "TWO",
        version="1.0",
        adapter=adapter,
        graph=_FakeGraph(),
        emitter=emitter,
    )
    emitter.flush()

    assert result_one.run_id != result_two.run_id

    lines = read_lines(log_path)
    run_ids_for_one = {line["run_id"] for line in lines if line.get("entity_id") == "ONE-1.0"}
    run_ids_for_two = {line["run_id"] for line in lines if line.get("entity_id") == "TWO-1.0"}
    assert run_ids_for_one == {result_one.run_id}
    assert run_ids_for_two == {result_two.run_id}
    assert run_ids_for_one != run_ids_for_two


def test_ingest_regulatory_instrument_emits_one_log_entry_per_stage(
    make_emitter: _MakeEmitter, read_lines: _ReadLines
) -> None:
    emitter, log_path = make_emitter()
    adapter = _FakeAdapter({"IDENTIFIER": _structure("Fixture")})

    ingest_regulatory_instrument(
        "IDENTIFIER", "SHORT", version="1.0", adapter=adapter, graph=_FakeGraph(), emitter=emitter
    )
    emitter.flush()

    actions = [
        line["action"] for line in read_lines(log_path) if line.get("entity_id") == "SHORT-1.0"
    ]
    assert actions == [
        "fetch_regulatory_instrument_structure",
        "register_regulatory_instrument_version",
        "persist_native_structural_graph",
        "verify_structural_graph_reachable",
    ]


def test_ingest_computes_regulatory_instrument_id_from_short_name_and_version(
    make_emitter: _MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    adapter = _FakeAdapter({"ANY_IDENTIFIER": _structure("Fixture")})

    result = ingest_regulatory_instrument(
        "ANY_IDENTIFIER", "XYZ", version="2.3", adapter=adapter, graph=_FakeGraph(), emitter=emitter
    )

    assert result.regulatory_instrument_id == "XYZ-2.3"


def test_ingest_regulatory_instrument_uses_currently_bound_run_id_when_nested_in_an_outer_context(
    make_emitter: _MakeEmitter, read_lines: _ReadLines
) -> None:
    """`bind_run_context()`'s own nested-scope restore semantics (run_context.py)
    mean `ingest_regulatory_instrument()`'s inner `run_id` is still what gets baked into
    its own log entries, even when called from within an already-bound outer
    scope — proving the pipeline always uses its own freshly bound id, not
    whatever happened to be active on entry.
    """
    emitter, log_path = make_emitter()
    adapter = _FakeAdapter({"IDENTIFIER": _structure("Fixture")})

    with bind_run_context("outer-run"):
        result = ingest_regulatory_instrument(
            "IDENTIFIER",
            "SHORT",
            version="1.0",
            adapter=adapter,
            graph=_FakeGraph(),
            emitter=emitter,
        )
    emitter.flush()

    assert result.run_id != "outer-run"
    lines = [line for line in read_lines(log_path) if line.get("entity_id") == "SHORT-1.0"]
    assert lines, "no entries were written — wiring bug"
    assert all(line["run_id"] == result.run_id for line in lines)


# --- (b) AC-006: AST-based regulation-name-conditional scan (B2/B3 fix) ---

_FORBIDDEN_REGULATORY_INSTRUMENT_NAMES = frozenset({"CRA", "GDPR", "NIS2"})
_FORBIDDEN_CELEX_IDENTIFIERS = frozenset({"32024R2847", "32016R0679", "32022L2555"})
_FORBIDDEN_LITERALS = _FORBIDDEN_REGULATORY_INSTRUMENT_NAMES | _FORBIDDEN_CELEX_IDENTIFIERS

_DOCSTRING_HOST_TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _files_to_scan() -> list[Path]:
    """B2 fix: every `.py` file actually delivered under
    `ps_service/ingestion/`, found by walking the real filesystem
    (`rglob("*.py")`) rather than a hardcoded list — this must catch a
    violation in `models.py`/`errors.py`/`falkordb_client.py`/
    `graph_writer.py`/any `adapters/**` module, not just `pipeline.py`.
    """
    ingestion_root = Path(ingestion_package.__file__).parent
    return sorted(ingestion_root.rglob("*.py"))


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """`id()`s of every module/class/function docstring's `ast.Constant`
    node — the first statement of a `Module`/`ClassDef`/`FunctionDef`/
    `AsyncFunctionDef` body, when it is a bare string-literal expression.

    Kept as an explicit, separate exclusion (per PLAN_REVIEWED.md's B3 fix
    wording) even though `_find_forbidden_literals` below only ever
    walks a conditional construct's *decision* subtree — a docstring
    statement can never structurally be part of one, so this exclusion is
    defense-in-depth, not load-bearing; see this module's own docstring.
    """
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_HOST_TYPES):
            first_statement = node.body[0] if node.body else None
            if (
                isinstance(first_statement, ast.Expr)
                and isinstance(first_statement.value, ast.Constant)
                and isinstance(first_statement.value.value, str)
            ):
                docstring_ids.add(id(first_statement.value))
    return docstring_ids


def _conditional_decision_subtrees(tree: ast.AST) -> list[ast.AST]:
    """Every expression subtree that is a conditional/branching construct's
    *decision* — never its body/orelse — per PLAN_REVIEWED.md's "ast.If /
    ast.Compare / any other executable context" wording: an `if`/`elif`
    test, a ternary's test, a `while`/`assert` condition, a bare `Compare`
    node (covers `==`/`!=`/`in`/`not in`/`is`/... wherever it occurs, even
    outside an explicit `if`), and a `match` statement's case patterns.
    """
    subtrees: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp, ast.While, ast.Assert)):
            subtrees.append(node.test)
        elif isinstance(node, ast.Compare):
            subtrees.append(node)
        elif isinstance(node, ast.Match):
            subtrees.extend(case.pattern for case in node.cases)
    return subtrees


def _find_forbidden_literals(tree: ast.AST) -> list[ast.Constant]:
    """B3 fix: AST-based walk, not a substring `"CRA" not in source` check.

    Flags a string-literal `Constant` node whose value is a forbidden
    literal — a regulation name (`CRA`/`GDPR`/`NIS2`) or one of the real
    CELEX identifiers of the instruments ingested today
    (`32024R2847`/`32016R0679`/`32022L2555`) — when it appears inside a
    conditional/branching construct's decision
    (`_conditional_decision_subtrees`) — i.e. a genuine
    `short_name == "CRA"`-shaped regulation-name conditional. Comments are
    already invisible to `ast` (discarded at tokenization); docstring `Expr`
    nodes are additionally, explicitly excluded (`_docstring_constant_ids`).
    """
    docstring_ids = _docstring_constant_ids(tree)
    seen: set[int] = set()
    violations: list[ast.Constant] = []
    for subtree in _conditional_decision_subtrees(tree):
        for node in ast.walk(subtree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in _FORBIDDEN_LITERALS
                and id(node) not in docstring_ids
                and id(node) not in seen
            ):
                seen.add(id(node))
                violations.append(node)
    return violations


def test_no_forbidden_literal_conditionals_in_ingestion_package() -> None:
    for path in _files_to_scan():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations = _find_forbidden_literals(tree)
        assert not violations, (
            f"{path}: forbidden-literal conditional found at "
            f"{[(v.value, v.lineno) for v in violations]}"
        )


def test_files_to_scan_covers_every_known_ingestion_module() -> None:
    """Guards against `_files_to_scan` silently narrowing back down to a
    hardcoded-looking subset (the exact defect B2 fixed) — asserts the
    rglob walk actually finds every module Increments 1-11 delivered.
    """
    scanned_relative_paths = {
        str(path.relative_to(Path(ingestion_package.__file__).parent)) for path in _files_to_scan()
    }
    expected = {
        "__init__.py",
        "errors.py",
        "models.py",
        "pipeline.py",
        "falkordb_client.py",
        "graph_writer.py",
        "adapters/__init__.py",
        "adapters/base.py",
        "adapters/errors.py",
        "adapters/cellar_eli/__init__.py",
        "adapters/cellar_eli/fetch.py",
        "adapters/cellar_eli/metadata.py",
        "adapters/cellar_eli/structure.py",
        "adapters/cellar_eli/adapter.py",
    }
    assert expected <= scanned_relative_paths


def test_find_forbidden_literals_flags_a_hypothetical_conditional() -> None:
    """Proves the scan's positive case: a `short_name == "CRA"` comparison
    inside an `if` test is flagged, wherever it occurs — not just against
    this package's real files (which contain none).
    """
    tree = ast.parse(
        "def f(short_name):\n    if short_name == 'CRA':\n        return True\n    return False\n"
    )

    violations = _find_forbidden_literals(tree)

    assert [v.value for v in violations] == ["CRA"]


def test_find_forbidden_literals_flags_a_hypothetical_celex_conditional() -> None:
    """Proves the scan now also flags a branch on a specific CELEX
    identifier — the same per-instrument conditional AC-BI-013 forbids,
    just keyed on the source identifier rather than the short name.
    """
    tree = ast.parse(
        "def f(identifier):\n"
        "    if identifier == '32016R0679':\n"
        "        return True\n"
        "    return False\n"
    )

    assert [v.value for v in _find_forbidden_literals(tree)] == ["32016R0679"]


def test_find_forbidden_literals_ignores_docstring_examples() -> None:
    """Proves the scan's negative case: a docstring naming all three
    regulations (and their CELEX identifiers) as illustrative examples
    (exactly `pipeline.py`'s / `metadata.py`'s own docstring shape) is not
    flagged.
    """
    tree = ast.parse(
        '"""Ingests a regulation, e.g. CRA, GDPR, or NIS2, from its source."""\n'
        "def f(short_name: str) -> None:\n"
        "    pass\n"
    )

    violations = _find_forbidden_literals(tree)

    assert violations == []

    celex_tree = ast.parse(
        '"""Ingests CRA (32024R2847) / NIS2 (32022L2555)."""\n'
        "def g(identifier: str) -> None:\n"
        "    pass\n"
    )

    assert _find_forbidden_literals(celex_tree) == []
