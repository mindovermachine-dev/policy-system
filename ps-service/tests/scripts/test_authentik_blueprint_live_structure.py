"""Structural guards for `test_authentik_blueprint_apply_live.py`.

The live tests need a container runtime, so these checks read the module's syntax tree instead.
They keep the module's tests independent of each other and of the order they run in, and keep
the one shared environment the only place a container pair is started.
"""

from __future__ import annotations

import ast
from pathlib import Path

LIVE_MODULE = Path(__file__).with_name("test_authentik_blueprint_apply_live.py")
ENVIRONMENT_FIXTURE = "shared_environment"
ALLOWED_TEST_PARAMETERS = {ENVIRONMENT_FIXTURE, "tmp_path"}
SCRIPT_PHASE_OPTIONS = {"--start-environment", "--stop-environment"}
PHASE_HELPERS = {"_start_environment", "_stop_environment"}
SCRIPT_RUNNER = "_run_script"
MUTABLE_DISPLAYS = (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)


def _live_tree() -> ast.Module:
    return ast.parse(LIVE_MODULE.read_text(encoding="utf-8"))


def _functions(tree: ast.Module) -> list[ast.FunctionDef]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]


def _tests(tree: ast.Module) -> list[ast.FunctionDef]:
    return [function for function in _functions(tree) if function.name.startswith("test_")]


def _names_used_in(function: ast.FunctionDef) -> set[str]:
    return {node.id for node in ast.walk(function) if isinstance(node, ast.Name)}


def _strings_in(function: ast.FunctionDef) -> set[str]:
    return {
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_the_module_defines_a_module_scoped_environment_fixture() -> None:
    (fixture,) = [
        function for function in _functions(_live_tree()) if function.name == ENVIRONMENT_FIXTURE
    ]

    scopes = [
        keyword.value.value
        for decorator in fixture.decorator_list
        if isinstance(decorator, ast.Call)
        for keyword in decorator.keywords
        if keyword.arg == "scope" and isinstance(keyword.value, ast.Constant)
    ]
    assert scopes == ["module"]


def test_every_live_test_takes_the_shared_environment_and_nothing_else_it_shares() -> None:
    tests = _tests(_live_tree())

    assert len(tests) >= 5
    for test in tests:
        parameters = {argument.arg for argument in test.args.args}
        assert parameters <= ALLOWED_TEST_PARAMETERS, f"{test.name} takes {sorted(parameters)}"
    shared = {test.name for test in tests if ENVIRONMENT_FIXTURE in {a.arg for a in test.args.args}}
    assert len(shared) >= 5


def test_the_module_keeps_no_mutable_state_the_tests_could_share() -> None:
    tree = _live_tree()

    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Global)]
    mutable = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, MUTABLE_DISPLAYS)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]
    assert mutable == []


def test_no_live_test_starts_its_own_container_pair() -> None:
    for test in _tests(_live_tree()):
        assert SCRIPT_RUNNER not in _names_used_in(test), f"{test.name} runs the script directly"
        assert not PHASE_HELPERS & _names_used_in(test), (
            f"{test.name} starts or stops an environment"
        )
        assert not SCRIPT_PHASE_OPTIONS & _strings_in(test), f"{test.name} names a script phase"


def test_only_the_environment_helpers_name_the_start_and_stop_phases() -> None:
    naming = {
        function.name
        for function in _functions(_live_tree())
        if SCRIPT_PHASE_OPTIONS & _strings_in(function)
    }

    assert naming == PHASE_HELPERS
