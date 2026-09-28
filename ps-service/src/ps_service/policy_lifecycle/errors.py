"""Domain-specific exception types for `ps_service.policy_lifecycle` (issue #134).

One exception type per distinct failure boundary this component owns, never a
generic `Exception`/`ValueError` (L1/L2 Error Handling) -- mirrors the shape
of `ps_service.domain_mapper.errors`/`ps_service.authz.errors`: plain
`Exception` subclasses (not `ps_service.api.errors.ApiError` -- that base is
reserved for the API-boundary layer; a later slice's own `ps_service.api`
translation, if any, wraps these). Every message is either fully fixed
(baked into a no-argument `__init__`, guaranteeing byte-identical text at
every raise site -- the same discipline `ps_service.mcp_interface.errors.
McpGraphUnavailableError` applies to keep host/port/driver detail off the
MCP boundary) or built only from values the caller already supplied/knows
(a `policy_id`, a `title`, a `status`) -- never internal detail such as a
host, port, or stack trace (AC-BI-015).

`AC-BI-008`'s access-denied case is deliberately **not** reimplemented here:
it reuses `ps_service.api.errors.AccessDeniedError` unchanged.
"""

from __future__ import annotations


class PolicyNotFoundError(Exception):
    """No `Policy` node exists with the given id (generic 404-shaped case).

    `policy_id` is supplied by the caller in the very request that raises
    this, so echoing it back leaks nothing new.
    """

    def __init__(self, policy_id: str) -> None:
        """Build the message from the already-caller-known `policy_id`.

        Args:
            policy_id: The id the caller looked up; not found.
        """
        super().__init__(f"no Policy exists with id {policy_id!r}")
        self.policy_id = policy_id


class PolicyDraftAccessDeniedError(Exception):
    """A caller tried to read/act on a draft `Policy` they do not own (AC-BI-002).

    The message is fixed and never reveals whether the policy exists, who
    owns it, or its current status -- distinguishing any of those would let
    a caller use repeated calls as a visibility oracle.
    """

    def __init__(self) -> None:
        """Fix the message so every raise site is byte-identical (AC-BI-015)."""
        super().__init__("you do not have access to this Policy")


class PolicyTitleAlreadyExistsError(Exception):
    """A `create-policy-draft` call's title collides with an existing Policy (AC-BI-022).

    Both interpolated values -- the caller-supplied `title` and the existing
    policy's own `id` -- are already known to the caller who just attempted
    to create that title, so naming them back is not a new leak.
    """

    def __init__(self, title: str, existing_policy_id: str) -> None:
        """Build the message from the caller-supplied title and the existing policy's id.

        Args:
            title: The title the caller attempted to use.
            existing_policy_id: The id of the Policy already using that title.
        """
        super().__init__(
            f"a Policy titled {title!r} already exists (id {existing_policy_id!r}); "
            "amend it via the supersede workflow, or choose a different title"
        )
        self.title = title
        self.existing_policy_id = existing_policy_id


class PolicyIncompleteForProposalError(Exception):
    """A `Policy` was proposed with zero `Standard`s attached (AC-BI-013)."""

    def __init__(self) -> None:
        """Fix the message so every raise site is byte-identical (AC-BI-015)."""
        super().__init__("at least one Standard is required before a Policy can be proposed")


class PolicyInvalidStatusTransitionError(Exception):
    """A lifecycle action was attempted from a status that does not permit it (AC-BI-023).

    `action` and `current_status`/`required_status` are all already known to
    the caller (the action they just attempted, and the Policy's real
    current state) -- naming them is actionable, not a leak.
    """

    def __init__(self, *, action: str, current_status: str, required_status: str) -> None:
        """Build the message from the attempted action and the Policy's actual status.

        Args:
            action: The lifecycle action the caller attempted (e.g. `"approve"`).
            current_status: The Policy's actual current status.
            required_status: The status `action` actually requires.
        """
        super().__init__(
            f"cannot {action} a Policy in status {current_status!r} "
            f"(requires status {required_status!r})"
        )
        self.action = action
        self.current_status = current_status
        self.required_status = required_status


class PolicySelfApprovalBlockedError(Exception):
    """A `Policy`'s owner attempted to approve or reject their own Policy (AC-BI-006)."""

    def __init__(self) -> None:
        """Fix the message so every raise site is byte-identical (AC-BI-015)."""
        super().__init__("you cannot approve or reject a Policy you own")


class PolicyLifecycleGraphUnavailableError(Exception):
    """The FalkorDB-backed policy graph could not be acquired for a lifecycle call.

    Covers unreachable, refused, a driver I/O failure, or invalid
    `PS_FALKORDB_*` configuration. The message is deliberately generic:
    host/port/driver/env-var detail must never cross this component's
    boundary, mirroring `ps_service.mcp_interface.errors.
    McpGraphUnavailableError`'s own discipline (AC-BI-024).
    """

    def __init__(self) -> None:
        """Fix the message so every raise site is byte-identical (AC-BI-015)."""
        super().__init__("the policy graph database is not reachable")
