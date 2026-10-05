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


class CannotVerifyError(PsCliError):
    """A `PsCliError` raised when a check could not be completed, not when it failed.

    Raised for transport-level failures (connection error, timeout, TLS failure) and
    server-side 5xx responses from the IdP or PS Service -- outcomes that say nothing
    about whether a stored credential is valid. Subclassing `PsCliError` keeps every
    existing `except PsCliError` site and its user-facing wording unchanged; only
    `auth status` distinguishes it. `detail` is a short, secret-free reason (an
    exception class name or an HTTP status) for callers that report it.
    """

    def __init__(self, *, msg: str, hint: str | None = None, detail: str = "") -> None:
        """Store the message, optional hint, and the secret-free `detail` reason."""
        self.detail = detail
        super().__init__(msg=msg, hint=hint)


class CredentialStoreError(PsCliError):
    """A `PsCliError` raised when the OS credential backend itself fails.

    Distinct from "no credential stored" and "credential rejected": the store could not
    be read or written (e.g. a locked keychain). Subclassing `PsCliError` leaves every
    existing `except PsCliError` site and its wording unchanged; `auth status` uses the
    type to report the store problem as-is instead of calling the credential unusable.
    `status` is the backend's numeric status (e.g. macOS `-67701`) when it reported one --
    a secret-free integer, mirroring `CannotVerifyError.detail` -- so callers can branch
    on it without parsing text (issue #181).
    """

    def __init__(self, *, msg: str, hint: str | None = None, status: int | None = None) -> None:
        """Store the message, optional hint, and the backend's numeric `status` (or `None`)."""
        self.status = status
        super().__init__(msg=msg, hint=hint)


def assert_contract(*, contract: bool, msg: str, hint: str | None = None) -> None:
    """Raise PsCliError(msg=msg, hint=hint) if contract is False; no-op otherwise."""
    if not contract:
        raise PsCliError(msg=msg, hint=hint)
