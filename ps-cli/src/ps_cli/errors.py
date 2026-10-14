"""The single user-facing error type for ps-cli, per L2's `## ps-cli` Error Handling section.

Every user-facing failure (invalid input, unreachable PS Service, a structured error
response from PS Service) is raised as a `PsCliError`. `ps_cli.cli.run()` reports it as a
normal failure (exit status 1); any other exception is treated as a bug and reported as an
unexpected error.
"""


class PsCliError(Exception):
    """A user-facing ps-cli error: an actionable message, with an optional hint."""

    def __init__(self, *, msg: str, hint: str | None = None) -> None:
        """Store the message and optional hint; format them into the exception text.

        Args:
            msg: What went wrong, worded for the operator. Kept unformatted in `.msg`.
            hint: A suggested next step, shown on its own line, or `None` for none.

        Returns:
            None.
        """
        self.msg = msg
        self.hint = hint
        super().__init__(str(self))

    def __str__(self) -> str:
        """Return the operator-facing text of the error.

        Returns:
            `❌ msg`, followed by `💡 hint` on its own line when a non-empty hint
            was given.
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
        """Store the message, optional hint, and the secret-free `detail` reason.

        Args:
            msg: What went wrong, worded for the operator.
            hint: A suggested next step, or `None` for none.
            detail: A short reason that must not contain secrets, e.g. an exception
                class name or an HTTP status; empty when there is none.

        Returns:
            None.
        """
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
    on it without parsing text.
    """

    def __init__(self, *, msg: str, hint: str | None = None, status: int | None = None) -> None:
        """Store the message, optional hint, and the backend's numeric `status` (or `None`).

        Args:
            msg: What went wrong, worded for the operator.
            hint: A suggested next step, or `None` for none.
            status: The backend's numeric status code when it reported one, else `None`.

        Returns:
            None.
        """
        self.status = status
        super().__init__(msg=msg, hint=hint)


def assert_contract(*, contract: bool, msg: str, hint: str | None = None) -> None:
    """Raise `PsCliError(msg=msg, hint=hint)` if `contract` is false; do nothing otherwise.

    Args:
        contract: The condition that must hold.
        msg: The operator-facing message used when it does not.
        hint: A suggested next step, or `None` for none.

    Returns:
        None when the contract holds.

    Raises:
        PsCliError: `contract` is false.
    """
    if not contract:
        raise PsCliError(msg=msg, hint=hint)
