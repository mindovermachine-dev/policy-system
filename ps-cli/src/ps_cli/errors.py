"""The single user-facing error type for ps-cli, per L2's `## ps-cli` Error Handling section.

Every user-facing failure (invalid input, unreachable PS Service, a structured error
response from PS Service) is raised as a `PsCliError`. It is the only exception type
caught by `ps_cli.cli.run()` — anything else is treated as a bug and propagates with a
full traceback.
"""


class PsCliError(Exception):
    """A user-facing ps-cli error: an actionable message, with an optional hint."""

    def __init__(self, *, msg: str, hint: str | None = None) -> None:
        """Store the message and optional hint; format them into the exception text."""
        self.msg = msg
        self.hint = hint
        super().__init__(str(self))

    def __str__(self) -> str:
        """Return `❌ msg`, plus `💡 hint` on its own line when a hint was given.

        Mirrors gh-tt's `ContractError` formatting (`utils.py::assert_contract`).
        """
        text = f"❌ {self.msg}"
        if self.hint:
            text += f"\n💡 {self.hint}"
        return text


def assert_contract(*, contract: bool, msg: str, hint: str | None = None) -> None:
    """Raise PsCliError(msg=msg, hint=hint) if contract is False; no-op otherwise."""
    if not contract:
        raise PsCliError(msg=msg, hint=hint)
