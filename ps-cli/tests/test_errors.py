"""Tests for ps_cli.errors: PsCliError and assert_contract()."""

import pytest

from ps_cli.errors import PsCliError, assert_contract


def test_assert_contract_raises_ps_cli_error_when_contract_is_false() -> None:
    """A false contract raises PsCliError carrying the given msg and hint."""
    with pytest.raises(PsCliError) as excinfo:
        assert_contract(contract=False, msg="celex is invalid", hint="use a 10-char CELEX id")

    assert excinfo.value.msg == "celex is invalid"
    assert excinfo.value.hint == "use a 10-char CELEX id"


def test_assert_contract_is_a_no_op_when_contract_is_true() -> None:
    """A true contract does not raise."""
    assert_contract(contract=True, msg="unreachable", hint=None)


def test_ps_cli_error_string_includes_hint_when_present() -> None:
    """str(PsCliError) includes both msg and hint when hint is given."""
    error = PsCliError(msg="could not reach PS Service", hint="check PS_CLI_SERVICE_URL")

    text = str(error)

    assert "could not reach PS Service" in text
    assert "check PS_CLI_SERVICE_URL" in text


def test_ps_cli_error_string_omits_hint_when_absent() -> None:
    """str(PsCliError) is exactly msg when hint is None — no stray 'None' text."""
    error = PsCliError(msg="could not reach PS Service", hint=None)

    text = str(error)

    assert text == "❌ could not reach PS Service"
    assert "None" not in text


def test_assert_contract_default_hint_is_none() -> None:
    """Hint is optional and defaults to None."""
    with pytest.raises(PsCliError) as excinfo:
        assert_contract(contract=False, msg="bad input")

    assert excinfo.value.hint is None
