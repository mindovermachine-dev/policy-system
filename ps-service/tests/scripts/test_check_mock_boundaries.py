"""Unit tests for `scripts/check_mock_boundaries.py` (issue #163 Slice B, AC-BI-009 groundwork).

Pure AST analysis over in-memory/tmp-path fixture source text -- no subprocess, no FalkorDB/
Postgres/Docker dependency -- so this runs in the default hermetic suite (mirrors the
`ps-service/tests/scripts/test_verify_chart_independence.py` precedent of testing a repo-level
`scripts/*` file from inside `ps-service/tests/`, per `pyproject.toml`'s `testpaths`).

Boundary-list fixtures are self-contained (written to `tmp_path`, never read from the real
`docs/coding-standards/approved-mock-boundaries.yaml`) so these tests do not depend on Slice A's
file having landed. `FULL_BOUNDARY_YAML` below is copied verbatim from PLAN.md §3's schema.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import types

    import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_mock_boundaries.py"

# Verbatim from `.orchestrator/tracker/issue-163/PLAN.md` §3 (Slice A's committed file content).
FULL_BOUNDARY_YAML = """\
falkordb:
  - ps_service.ingestion.falkordb_client.connect
  - ps_service.ingestion.falkordb_client.connect_from_config
  - ps_service.domain_mapper.falkordb_client.connect
  - ps_service.domain_mapper.falkordb_client.connect_from_config
  - ps_service.company_merge.falkordb_client.connect
  - ps_service.company_merge.falkordb_client.connect_from_config
  - ps_service.change_monitor.falkordb_client.connect
  - ps_service.change_monitor.falkordb_client.connect_from_config
  - ps_service.query_engine.falkordb_client.connect
  - ps_service.query_engine.falkordb_client.connect_from_config
  - ps_service.export.falkordb_connection.raw_connection

postgres:
  - ps_service.authz.store.PsycopgAccessRoleStore
  - ps_service.authz.store.connect_from_config
  - ps_service.audit.store.connect_from_config
  - ps_service.audit.store.PsycopgAuditStore
  - ps_service.passkey_signing.store.PsycopgPendingApprovalStore
  - ps_service.passkey_signing.store.connect_from_config
  - ps_service.passkey_signing.signing_credential_store.PsycopgSigningCredentialStore

llm:
  - ps_service.llm_interface.client.litellm.completion
  - ps_service.llm_interface.client.litellm.embedding
  - ps_service.llm_interface.client.default_completion_caller
  - ps_service.llm_interface.client.default_embedding_caller
  - ps_service.llm_interface.structured_completion.supports_response_schema

http:
  - ps_service.auth.discovery.fetch_discovery_document
  - ps_service.invitations.client.create_invitation
  - ps_service.ingestion.adapters.cellar_eli.fetch.fetch_xhtml
  - ps_service.ingestion.adapters.cellar_eli.fetch.fetch_rdf
  - ps_service.mcp_interface.mcp_server.build_default_curated_catalog_dependencies
  - ps_service.mcp_interface.mcp_server.build_default_change_check_dependencies
  - msal_extensions.build_encrypted_persistence
  - ps_cli.credentials._build_production_persistence

process_os:
  - sys.stdin
  - atexit.register
  - uvicorn.run
  - tempfile.mkdtemp
  - secrets.token_urlsafe
  - ps_cli.cli.installed_version
  - ps_service.mcp_interface.mcp_server._domain_concepts_path
"""


def _load_check_mock_boundaries() -> types.ModuleType:
    """Import `scripts/check_mock_boundaries.py` by path (it is not part of any package)."""
    spec = importlib.util.spec_from_file_location("check_mock_boundaries", SCRIPT_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"could not load spec for {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses' own machinery needs the module registered
    spec.loader.exec_module(module)
    return module


cmb = _load_check_mock_boundaries()


def _write_boundary_yaml(tmp_path: Path, content: str) -> Path:
    boundary_file = tmp_path / "approved-mock-boundaries.yaml"
    boundary_file.write_text(content, encoding="utf-8")
    return boundary_file


def _full_boundary_entries(tmp_path: Path) -> frozenset[str]:
    return cmb.load_boundary_entries(_write_boundary_yaml(tmp_path, FULL_BOUNDARY_YAML))


# --- (a) not-on-boundary-list target, no exception comment: reported + non-zero exit ---------


def test_disallowed_target_reported_with_file_line_target_and_nonzero_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tests_dir = tmp_path / "ps-service" / "tests"
    tests_dir.mkdir(parents=True)
    test_file = tests_dir / "test_offender.py"
    test_file.write_text(
        "from unittest import mock\n"
        "\n"
        "\n"
        "def test_something() -> None:\n"
        '    with mock.patch("ps_service.some_module.SomeInternalThing.do_stuff"):\n'
        "        pass\n",
        encoding="utf-8",
    )
    boundary_file = _write_boundary_yaml(tmp_path, FULL_BOUNDARY_YAML)

    exit_code = cmb.main([str(tests_dir), "--boundary-file", str(boundary_file)])

    assert exit_code != 0
    out = capsys.readouterr().out
    expected_line = (
        f"{test_file}:5: disallowed mock target "
        "'ps_service.some_module.SomeInternalThing.do_stuff' "
        "(not in approved-mock-boundaries.yaml, no # detroit-exception comment)"
    )
    assert expected_line in out

    # Also verify at the scan_source level for a precise, decoupled-from-CLI assertion.
    violations = cmb.scan_source(
        test_file.read_text(encoding="utf-8"), str(test_file), _full_boundary_entries(tmp_path)
    )
    assert len(violations) == 1
    assert violations[0].file == str(test_file)
    assert violations[0].line == 5
    assert violations[0].message == (
        "disallowed mock target 'ps_service.some_module.SomeInternalThing.do_stuff' "
        "(not in approved-mock-boundaries.yaml, no # detroit-exception comment)"
    )


# --- (b) boundary-list target: silently accepted -----------------------------------------------


def test_boundary_list_target_silently_accepted(tmp_path: Path) -> None:
    source = (
        "from unittest import mock\n"
        "\n"
        "\n"
        "def test_something() -> None:\n"
        '    with mock.patch("ps_service.ingestion.falkordb_client.connect"):\n'
        "        pass\n"
    )
    violations = cmb.scan_source(source, "test_ok.py", _full_boundary_entries(tmp_path))
    assert violations == []


# --- (c) `# detroit-exception:` comment suppresses an otherwise out-of-boundary target ----------


def test_detroit_exception_comment_suppresses_violation(tmp_path: Path) -> None:
    boundary_entries = _full_boundary_entries(tmp_path)

    same_line_source = (
        "from unittest import mock\n"
        "\n"
        "\n"
        "def test_something() -> None:\n"
        '    with mock.patch("ps_service.some_module.SomeInternalThing.do_stuff"):'
        "  # detroit-exception: legacy interaction assertion, see issue #163\n"
        "        pass\n"
    )
    assert cmb.scan_source(same_line_source, "test_exempt.py", boundary_entries) == []

    line_above_source = (
        "from unittest import mock\n"
        "\n"
        "\n"
        "def test_something() -> None:\n"
        "    # detroit-exception: legacy interaction assertion, see issue #163\n"
        '    with mock.patch("ps_service.some_module.SomeInternalThing.do_stuff"):\n'
        "        pass\n"
    )
    assert cmb.scan_source(line_above_source, "test_exempt2.py", boundary_entries) == []


# --- (d0) f-string (unresolvable) setattr target: flagged unless exception-commented ------------
# Gap 1, issue #163 interstitial fix (`.orchestrator/tracker/issue-163/IMPL_SLICE_11.md`): the
# f-string form `monkeypatch.setattr(f"...{name}", ...)` used to resolve to `None` and be
# silently skipped by `_target_violations`. It must now require the same
# `# detroit-exception:` suppression as a resolvable-but-disallowed target.


def test_fstring_setattr_target_with_no_exception_comment_is_flagged(tmp_path: Path) -> None:
    source = (
        "from unittest import mock\n"
        "\n"
        "\n"
        "def test_something(monkeypatch: object) -> None:\n"
        '    name = "do_stuff"\n'
        '    monkeypatch.setattr(f"ps_service.some_module.{name}", lambda: None)\n'
    )
    violations = cmb.scan_source(source, "test_fstring.py", _full_boundary_entries(tmp_path))
    assert len(violations) == 1
    assert violations[0].line == 6
    assert violations[0].message == (
        "unresolvable dynamic mock target -- add # detroit-exception: <reason> if intentional"
    )


def test_fstring_setattr_target_with_exception_comment_is_not_flagged(tmp_path: Path) -> None:
    source = (
        "from unittest import mock\n"
        "\n"
        "\n"
        "def test_something(monkeypatch: object) -> None:\n"
        '    name = "do_stuff"\n'
        "    # detroit-exception: dynamic target, see issue #163\n"
        '    monkeypatch.setattr(f"ps_service.some_module.{name}", lambda: None)\n'
    )
    violations = cmb.scan_source(source, "test_fstring_ok.py", _full_boundary_entries(tmp_path))
    assert violations == []


def test_bare_builtin_setattr_is_not_classified_as_a_mock_boundary_call(tmp_path: Path) -> None:
    """Regression guard surfaced while building Gap 1's fix: the bare builtin
    `setattr(obj, attr, value)` (e.g. `test_models.py`'s frozen-dataclass `_mutate` helper,
    used to bypass basedpyright's static attribute check -- nothing to do with mocking) must
    never be classified as a `monkeypatch.setattr(...)`-shaped call. Before this guard, it
    resolved to `None` via a variable (non-literal) `attr` argument and, after Gap 1's fix
    stopped silently accepting `None`, would have been wrongly flagged as an unresolvable
    dynamic mock target.
    """
    source = (
        "def _mutate(obj: object, attr: str, value: object) -> None:\n"
        "    setattr(obj, attr, value)\n"
    )
    violations = cmb.scan_source(source, "test_bare_setattr.py", _full_boundary_entries(tmp_path))
    assert violations == []


def test_fstring_setattr_target_with_multiline_comment_block_above_is_not_flagged(
    tmp_path: Path,
) -> None:
    """`_exception_comment_present` must walk the whole contiguous comment block above the
    call, not just the single line immediately above it -- mirrors
    `test_signing_hardening.py:516-522`'s real shape, where the `# detroit-exception:` marker
    is the comment block's *first* line, several lines above the call it documents.
    """
    source = (
        "from unittest import mock\n"
        "\n"
        "\n"
        "def test_something(monkeypatch: object) -> None:\n"
        '    name = "do_stuff"\n'
        "    # detroit-exception: dynamic target, see issue #163\n"
        "    # (multi-line explanatory comment, marker is the first line)\n"
        "    # more explanation here.\n"
        '    monkeypatch.setattr(f"ps_service.some_module.{name}", lambda: None)\n'
    )
    violations = cmb.scan_source(
        source, "test_fstring_multiline_ok.py", _full_boundary_entries(tmp_path)
    )
    assert violations == []


# --- (d) 3-arg monkeypatch.setattr with an aliased import resolves via suffix-match -------------


def test_aliased_import_setattr_resolves_via_suffix_match(tmp_path: Path) -> None:
    boundary_entries = cmb.load_boundary_entries(
        _write_boundary_yaml(tmp_path, "restore:\n  - ps_service.restore.staging.stage_graph\n")
    )
    source = (
        "from ps_service.restore import staging as s\n"
        "\n"
        "\n"
        "def test_something(monkeypatch: object) -> None:\n"
        '    monkeypatch.setattr(s, "stage_graph", lambda: None)\n'
    )
    violations = cmb.scan_source(source, "test_alias.py", boundary_entries)
    assert violations == []


# --- (e) bare Mock() passed directly as a constructor kwarg is flagged --------------------------


def test_bare_mock_as_constructor_kwarg_is_flagged(tmp_path: Path) -> None:
    source = (
        "from unittest.mock import Mock\n"
        "\n"
        "\n"
        "def test_something() -> None:\n"
        "    service = MyService(store=Mock())\n"
    )
    violations = cmb.scan_source(source, "test_bare_ctor.py", _full_boundary_entries(tmp_path))
    assert len(violations) == 1
    assert violations[0].line == 5
    assert violations[0].message == (
        "disallowed bare 'Mock(' construction (no mock.patch/monkeypatch.setattr target "
        "resolves to approved-mock-boundaries.yaml; construct via a real collaborator or an "
        "approved-boundary fake, or add # detroit-exception: <reason>)"
    )


# --- (f) m = Mock() traced to a later approved-boundary setattr: NOT flagged (regression guard) -


def test_traced_mock_to_approved_setattr_not_flagged(tmp_path: Path) -> None:
    source = (
        "from unittest.mock import Mock\n"
        "from ps_service.company_merge import falkordb_client\n"
        "\n"
        "\n"
        "def test_something(monkeypatch: object) -> None:\n"
        "    m = Mock()\n"
        '    monkeypatch.setattr(falkordb_client, "connect", m)\n'
    )
    violations = cmb.scan_source(source, "test_traced_ok.py", _full_boundary_entries(tmp_path))
    assert violations == []


# --- (g) m = Mock() traced to a later non-approved setattr: IS flagged (ordinary check fires) ---


def test_traced_mock_to_non_approved_setattr_is_flagged(tmp_path: Path) -> None:
    source = (
        "from unittest.mock import Mock\n"
        "from ps_service.some_module import SomeInternalThing\n"
        "\n"
        "\n"
        "def test_something(monkeypatch: object) -> None:\n"
        "    m = Mock()\n"
        '    monkeypatch.setattr(SomeInternalThing, "do_stuff", m)\n'
    )
    violations = cmb.scan_source(source, "test_traced_bad.py", _full_boundary_entries(tmp_path))

    target_violations = [v for v in violations if "disallowed mock target" in v.message]
    assert len(target_violations) == 1
    assert target_violations[0].line == 7
    assert "SomeInternalThing.do_stuff" in target_violations[0].message
