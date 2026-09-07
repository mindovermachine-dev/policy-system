"""AC-BI-009 proof: the EXTERNAL extraction stages (`extraction.py`,
`derivation.py`, `adapters/cellar_eli.py`) never name a Policy/Standard/
Control governance artifact -- narrower than this file's original
pre-issue-#54 scope, per CHANGES.md F2/F5's fold of S5 into S3.

**History (why this file changed shape in issue #54's S3):** before issue
#54, `DeriveGovernanceArtifacts` did not exist anywhere in
`ps_service.domain_mapper`, so a whole-package literal ban
(`test_no_governance_artifact_literals_in_domain_mapper_package`) was a
correct proxy for "governance derivation is out of scope." Issue #54 adds
`governance.py`, which legitimately mints `Policy`/`Standard`/`Control`
nodes and writes `GOVERNED_BY`/`SUPPORTED_BY`/`IMPLEMENTED_BY` edges
(D1/AC-BI-005/006) -- a whole-package ban would now be a false positive on
the very module this issue ships. AC-BI-009's own text narrows the concern
to what actually matters: the EXTERNAL pipeline's stages
(`extraction.py`/`derivation.py`/`adapters/cellar_eli.py`) must never name a
governance artifact, regardless of what `governance.py` (an internal-source-
only action) does. `test_files_to_scan_covers_every_known_domain_mapper_module`
still walks the WHOLE package (unchanged mechanism) to guard against
`_files_to_scan`/`_external_files_to_scan` silently narrowing further than
intended -- its own `expected` set below gains `governance.py`, proving the
whole-package walk still finds it (even though the ban itself no longer
scans it).

An AST-based scan (not a substring grep) of exactly the three external-
extraction-stage files (`_external_files_to_scan`, a fixed list -- not a
`pathlib.Path(...).rglob("*.py")` walk, since the whole point is to scan
LESS than the whole package now), asserting no string-literal `Constant`
node whose value is one of `"Policy"`/`"Standard"`/`"Control"`/
`"GOVERNED_BY"`/`"SUPPORTED_BY"`/`"IMPLEMENTED_BY"` appears ANYWHERE in
those three files (module/class/function/docstrings excluded).

**Deliberate divergence from #14's own scan mechanism, and why:** #14's
`test_no_regulation_name_conditionals_in_ingestion_package` only ever walks
a conditional/branching construct's *decision* subtree (`ast.If`/`IfExp`/
`While`/`Assert`/`Compare`/`Match`) -- appropriate for THAT check, whose
concern was regulation-name-conditional branching logic specifically. This
file's concern (AC-BI-009) is broader within its own narrowed file set: no
`Policy`/`Standard`/`Control`/`GOVERNED_BY`/`SUPPORTED_BY`/`IMPLEMENTED_BY`
string literal ANYWHERE in those three files, not only inside a
conditional's decision -- so this scan walks the ENTIRE module tree for a
matching `ast.Constant`, not just conditional subtrees. The docstring-
exclusion mechanism itself (`_docstring_constant_ids`) is copied verbatim
from #14's precedent, per this batch's explicit instruction to reuse the
same AST-based exclusion mechanism, not invent a different one. Comments
are already invisible to `ast` (discarded at tokenization) -- no separate
mechanism is needed for those, exactly as in #14's own scan.
"""

from __future__ import annotations

import ast
from pathlib import Path

import ps_service.domain_mapper as domain_mapper_package
from ps_service.domain_mapper.governance import derive_governance_artifacts

_FORBIDDEN_LITERALS = frozenset(
    {"Policy", "Standard", "Control", "GOVERNED_BY", "SUPPORTED_BY", "IMPLEMENTED_BY"}
)

_EXTERNAL_STAGE_RELATIVE_PATHS = ("extraction.py", "derivation.py", "adapters/cellar_eli.py")
"""The external (catalog) pipeline's own stage files -- AC-BI-009's exact
list. `governance.py` (internal-source only) is deliberately NOT in this
list -- it is meant to name these literals."""

_DOCSTRING_HOST_TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _files_to_scan() -> list[Path]:
    """Every `.py` file actually delivered under `ps_service/domain_mapper/`
    (source only -- `ps-service/src/ps_service/domain_mapper/`, never
    `tests/`), found by walking the real filesystem (`rglob("*.py")`)
    rather than a hardcoded list -- mirrors #14's `_files_to_scan`
    precedent exactly. Used only by
    `test_files_to_scan_covers_every_known_domain_mapper_module` now (issue
    #54 S3) -- the literal-ban test itself scans the narrower
    `_external_files_to_scan()` list instead; see this module's own
    docstring for why.
    """
    domain_mapper_root = Path(domain_mapper_package.__file__).parent
    return sorted(domain_mapper_root.rglob("*.py"))


def _external_files_to_scan() -> list[Path]:
    """AC-BI-009's exact three external-pipeline-stage files, as real paths.

    A fixed list (`_EXTERNAL_STAGE_RELATIVE_PATHS`), not a `rglob` walk --
    the whole point of issue #54 S3's rework is to scan LESS than the whole
    package, since `governance.py` (internal-source only) legitimately names
    every `_FORBIDDEN_LITERALS` term.
    """
    domain_mapper_root = Path(domain_mapper_package.__file__).parent
    return [domain_mapper_root / relative_path for relative_path in _EXTERNAL_STAGE_RELATIVE_PATHS]


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """`id()`s of every module/class/function docstring's `ast.Constant`
    node -- the first statement of a `Module`/`ClassDef`/`FunctionDef`/
    `AsyncFunctionDef` body, when it is a bare string-literal expression.

    Copied verbatim (same mechanism, same exclusion rule) from #14's own
    `ps-service/tests/ingestion/test_pipeline.py::_docstring_constant_ids`
    -- this batch's instructions require staying consistent with that
    precedent rather than inventing a different exclusion rule.
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


def _find_forbidden_literals(tree: ast.AST) -> list[ast.Constant]:
    """AST-based whole-tree walk, not a substring `"Policy" not in source`
    check. Flags every string-literal `Constant` node whose value is one of
    `_FORBIDDEN_LITERALS`, anywhere in the module -- not restricted to a
    conditional construct's decision subtree (see this module's own
    docstring for why that restriction, present in #14's own scan, does not
    apply to this broader AC-008 check). Docstring `Expr` nodes are
    excluded via `_docstring_constant_ids`, same mechanism as #14's scan.
    """
    docstring_ids = _docstring_constant_ids(tree)
    violations: list[ast.Constant] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _FORBIDDEN_LITERALS
            and id(node) not in docstring_ids
        ):
            violations.append(node)
    return violations


def test_external_extraction_stages_never_name_governance_artifacts() -> None:
    """AC-BI-009: `extraction.py`/`derivation.py`/`adapters/cellar_eli.py` --
    and ONLY those three files -- never name a Policy/Standard/Control
    governance artifact. Replaces the pre-#54
    `test_no_governance_artifact_literals_in_domain_mapper_package` (a
    whole-package ban that would now false-positive on `governance.py`,
    issue #54's own deliverable -- see this module's docstring).
    """
    for path in _external_files_to_scan():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations = _find_forbidden_literals(tree)
        assert not violations, (
            f"{path}: forbidden AC-BI-009 literal found at "
            f"{[(v.value, v.lineno) for v in violations]}"
        )


def test_files_to_scan_covers_every_known_domain_mapper_module() -> None:
    """Guards against `_files_to_scan` silently narrowing down to a
    hardcoded-looking subset -- mirrors #14's own
    `test_files_to_scan_covers_every_known_ingestion_module` regression
    guard, asserting the rglob walk actually finds every module Batches
    1-9 (plus issue #54 S3's `governance.py`) delivered. Unchanged mechanism
    from before #54 -- still a whole-package walk (`_files_to_scan`), even
    though the literal-ban test itself now scans a narrower list.
    """
    scanned_relative_paths = {
        str(path.relative_to(Path(domain_mapper_package.__file__).parent))
        for path in _files_to_scan()
    }
    expected = {
        "__init__.py",
        "errors.py",
        "models.py",
        "identity.py",
        "prompts.py",
        "extraction.py",
        "derivation.py",
        "governance.py",
        "falkordb_client.py",
        "graph_writer.py",
        "adapters/__init__.py",
        "adapters/base.py",
        "adapters/cellar_eli.py",
    }
    assert expected <= scanned_relative_paths


def test_derive_governance_artifacts_produces_policy_standard_control_for_internal_source() -> None:
    """Positive-case thin proof (issue #54 CHANGES.md F2): unlike the
    external stages this file's other tests guard, `governance.py`'s public
    action legitimately names every `_FORBIDDEN_LITERALS` term in its own
    source -- proving the narrower AC-BI-009 scan above is a deliberate
    carve-out, not an accidental gap. `tests/domain_mapper/test_governance.py`
    owns the full behavioral proof (mint/match/unmatchable, self-consistent
    ids, graph writes); this is a thin importability + literal-presence
    check only, not a duplicate of that suite.
    """
    assert callable(derive_governance_artifacts)

    # `governance.py` mints Policy/Standard/Control nodes; the
    # GOVERNED_BY/SUPPORTED_BY/IMPLEMENTED_BY edge-type literals themselves
    # live one call deeper, in `graph_writer.py::persist_governance_graph`
    # (the function `governance.py` delegates persistence to) -- combine
    # both files' source so every `_FORBIDDEN_LITERALS` term is accounted
    # for somewhere in the governance-derivation code path, not just one file.
    domain_mapper_root = Path(domain_mapper_package.__file__).parent
    governance_source = (domain_mapper_root / "governance.py").read_text(encoding="utf-8")
    graph_writer_source = (domain_mapper_root / "graph_writer.py").read_text(encoding="utf-8")
    combined_source = governance_source + graph_writer_source
    for literal in _FORBIDDEN_LITERALS:
        assert literal in combined_source


def test_find_forbidden_literals_flags_a_hypothetical_literal_anywhere_in_the_module() -> None:
    """Positive case: an ordinary (non-conditional, non-docstring) string
    literal assignment naming a forbidden governance-artifact term is
    flagged -- proving the scan is NOT restricted to a conditional's
    decision subtree (the deliberate divergence from #14's own scan, see
    this module's docstring).
    """
    tree = ast.parse('_LABEL = "Policy"\n')

    violations = _find_forbidden_literals(tree)

    assert [v.value for v in violations] == ["Policy"]


def test_find_forbidden_literals_flags_a_hypothetical_conditional_too() -> None:
    """A forbidden literal used inside a conditional's decision is also
    flagged -- the broader whole-tree scan is a superset of #14's own
    conditional-only check, not a replacement that narrows coverage.
    """
    tree = ast.parse(
        "def f(label):\n    if label == 'Control':\n        return True\n    return False\n"
    )

    violations = _find_forbidden_literals(tree)

    assert [v.value for v in violations] == ["Control"]


def test_find_forbidden_literals_ignores_docstring_examples() -> None:
    """Negative case: a docstring mentioning these terms as illustrative
    prose (e.g. explaining what AC-008 excludes, exactly like this test
    module's own docstring) is not flagged.
    """
    tree = ast.parse(
        '"""This component never derives Policy, Standard, or Control '
        "nodes, and never writes GOVERNED_BY, SUPPORTED_BY, or "
        'IMPLEMENTED_BY edges."""\n'
        "def f() -> None:\n"
        "    pass\n"
    )

    violations = _find_forbidden_literals(tree)

    assert violations == []
