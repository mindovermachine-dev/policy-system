"""Domain exception types for `ps_service.policy_lifecycle`.

One exception type per distinct failure boundary, never a bare `Exception`/`ValueError`. They are
plain `Exception` subclasses, not `ps_service.api.errors.ApiError`, which is reserved for the API
boundary; callers translate them. Every message is either fully fixed (baked into a no-argument
`__init__`, so the text is byte-identical at every raise site) or built only from values the caller
already supplied or is allowed to know (a `policy_id`, a `title`, a `status`), and never contains
internal detail such as a host, port or stack trace.

An access denial on a non-draft action reuses `ps_service.api.errors.AccessDeniedError` rather
than a type defined here.
"""

from __future__ import annotations


class PolicyNotFoundError(Exception):
    """No `Policy` node exists with the given id.

    Raised by the lifecycle service when a Policy is read, transitioned or edited by an id that
    matches nothing. Callers should report not-found and let the user correct the id.

    Attributes:
        policy_id: The id the caller looked up. It came from the caller's own request, so echoing
            it back leaks nothing new.
    """

    def __init__(self, policy_id: str) -> None:
        """Build the not-found message from the caller-supplied `policy_id`.

        Args:
            policy_id: The id the caller looked up; not found.
        """
        super().__init__(f"no Policy exists with id {policy_id!r}")
        self.policy_id = policy_id


class PolicyDraftAccessDeniedError(Exception):
    """A caller tried to read or act on a draft `Policy` they do not own.

    The message is fixed and never reveals whether the Policy exists, who owns it or its current
    status; distinguishing any of those would let a caller use repeated calls as a visibility
    oracle. Callers should report a generic access denial.
    """

    def __init__(self) -> None:
        """Take no arguments and fix the message so every raise site is byte-identical."""
        super().__init__("you do not have access to this Policy")


class PolicyTitleAlreadyExistsError(Exception):
    """A `create-policy-draft` call's title collides with an existing Policy.

    Raised when creating a draft whose title is already used. Callers should choose another title
    or amend the existing Policy through the supersede workflow.

    Attributes:
        title: The title the caller attempted to use.
        existing_policy_id: The id of the Policy already using that title. Both values are known to
            the caller who just attempted to create that title, so naming them is not a new leak.
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


class PolicyStandardNotFoundError(Exception):
    """No `Standard` node exists with the given id.

    Named with the `Policy` prefix like every exception of this module, although it names a
    Standard. Raised when a Standard is edited or read by an id that matches nothing; callers
    should report not-found.

    Attributes:
        standard_id: The id the caller looked up. It came from the caller's own request, so
            echoing it back leaks nothing new.
    """

    def __init__(self, standard_id: str) -> None:
        """Build the not-found message from the caller-supplied `standard_id`.

        Args:
            standard_id: The id the caller looked up; not found.
        """
        super().__init__(f"no Standard exists with id {standard_id!r}")
        self.standard_id = standard_id


class PolicyControlNotFoundError(Exception):
    """No `Control` node exists with the given id.

    Named with the `Policy` prefix like every exception of this module, although it names a
    Control. Raised when a Control is edited or read by an id that matches nothing; callers should
    report not-found.

    Attributes:
        control_id: The id the caller looked up. It came from the caller's own request, so echoing
            it back leaks nothing new.
    """

    def __init__(self, control_id: str) -> None:
        """Build the not-found message from the caller-supplied `control_id`.

        Args:
            control_id: The id the caller looked up; not found.
        """
        super().__init__(f"no Control exists with id {control_id!r}")
        self.control_id = control_id


class PolicyIncompleteForProposalError(Exception):
    """A `Policy` was proposed while it has no `Standard` attached.

    A Policy needs at least one Standard before it can move from draft to proposed. Callers should
    add a Standard and propose again.
    """

    def __init__(self) -> None:
        """Take no arguments and fix the message so every raise site is byte-identical."""
        super().__init__("at least one Standard is required before a Policy can be proposed")


class PolicyInvalidStatusTransitionError(Exception):
    """A lifecycle action was attempted from a status that does not permit it.

    Callers should report the current and required status so the user can pick a valid action.

    Attributes:
        action: The lifecycle action the caller attempted.
        current_status: The Policy's actual status.
        required_status: The status the action requires. All three values are already known to the
            caller (the action just attempted and the Policy's real state), so naming them is
            actionable, not a leak.
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
    """A `Policy`'s owner attempted to approve or reject their own Policy.

    Approval needs a second person. Callers should ask a different approver to act.
    """

    def __init__(self) -> None:
        """Take no arguments and fix the message so every raise site is byte-identical."""
        super().__init__("you cannot approve or reject a Policy you own")


class PolicySupersedePriorNotApprovedError(Exception):
    """`supersedes_policy_id` names a Policy that is not `approved`.

    Only an approved Policy can be forked into a superseding draft. Callers should supersede an
    approved Policy instead.

    Attributes:
        policy_id: The `supersedes_policy_id` the caller supplied.
        actual_status: That Policy's actual status. Both values are already known to the caller, so
            naming them is actionable, not a leak.
    """

    def __init__(self, policy_id: str, actual_status: str) -> None:
        """Build the message from the caller-supplied `policy_id` and its actual status.

        Args:
            policy_id: The `supersedes_policy_id` the caller attempted to fork.
            actual_status: That Policy's actual current status.
        """
        super().__init__(
            f"Policy {policy_id!r} cannot be superseded: current status is "
            f"{actual_status!r}, requires 'approved'"
        )
        self.policy_id = policy_id
        self.actual_status = actual_status


class PolicyCapabilityNotFoundError(Exception):
    """`create-policy-draft` named Capability ids that do not exist.

    Callers should check the Capability ids and retry.

    Attributes:
        capability_ids: The caller-supplied ids that match no Capability; echoing them back leaks
            nothing new.
    """

    def __init__(self, capability_ids: tuple[str, ...]) -> None:
        """Build the message from the caller-supplied, unknown `capability_ids`.

        Args:
            capability_ids: The requested ids that match no Capability.
        """
        super().__init__(
            f"no Capability exists with id(s) {', '.join(map(repr, capability_ids))}; "
            "check the Capability ids and retry"
        )
        self.capability_ids = capability_ids


class PolicyCapabilityAlreadyGovernedError(Exception):
    """`create-policy-draft` named Capability ids already governed by a Policy.

    Callers should amend the governing Policy through the supersede workflow instead.

    Attributes:
        capability_ids: The caller-supplied ids that already have a governing Policy. The
            governing Policy's own id is never named, because the caller may not be allowed to see
            it (draft visibility).
    """

    def __init__(self, capability_ids: tuple[str, ...]) -> None:
        """Build the message from the caller-supplied, already-governed `capability_ids`.

        Args:
            capability_ids: The requested ids that already have a governing Policy.
        """
        super().__init__(
            f"Capability id(s) {', '.join(map(repr, capability_ids))} already governed "
            "by a Policy; amend that Policy via the supersede workflow instead"
        )
        self.capability_ids = capability_ids


class PolicyGovernanceConflictError(Exception):
    """`approve-policy` found that the superseded Policy's governed Capabilities changed.

    The governed set read before the write no longer matched at write time, so the guarded write
    was refused: no edge moved and the Policy's status is unchanged. Callers should retry the
    approval.

    Attributes:
        policy_id: The Policy whose approval was not applied.
    """

    def __init__(self, policy_id: str) -> None:
        """Build the message from the Policy id the caller asked to approve.

        Args:
            policy_id: The Policy whose approval was not applied.
        """
        super().__init__(
            f"Policy {policy_id!r} was not approved: the Capabilities governed by the "
            "Policy it supersedes changed during approval and nothing was changed; "
            "retry the approval"
        )
        self.policy_id = policy_id


class PolicyLifecycleGraphUnavailableError(Exception):
    """The FalkorDB-backed policy graph could not be read or written for a lifecycle call.

    Covers an unreachable or refusing graph, a driver I/O failure or invalid `PS_FALKORDB_*`
    configuration on a read, and every gateway failure on a write: a refused group, an unreachable
    graph log or graph, a group committed but not yet applied. The message is deliberately generic:
    host, port, driver and environment-variable detail never cross this component's boundary,
    mirroring `ps_service.mcp_interface.errors.McpGraphUnavailableError`. Callers should report the
    graph as temporarily unavailable and retry later.

    With no argument the message is the fixed text, so every read-side raise site is
    byte-identical. A write-side raise passes `FalkorDB write failed: <sanitized gateway text>`
    (the gateway's texts are fixed and carry no host, port or content) and chains the gateway
    error as `__cause__`.
    """

    def __init__(self, message: str | None = None) -> None:
        """Carry `message`, or the fixed text when none is given.

        Args:
            message: The sanitized text of a write-side failure. `None` selects the fixed
                "the policy graph database is not reachable" text.
        """
        super().__init__(
            "the policy graph database is not reachable" if message is None else message
        )


class StaleGroupError(Exception):
    """A guarded group was refused because the graph log moved after its guard was read.

    Internal to `policy_lifecycle`: raised by `graph_submission.submit_group` when the gateway
    refuses a group's `ExpectedPosition` (`StaleGraphStateError`, chained as `__cause__`), so each
    guarded caller (fork repoint, capability-claim create) can translate it into its own named
    error. Nothing was logged for the refused group; the caller re-reads and decides again, or
    translates it. Not part of the component's public surface.
    """

    def __init__(self) -> None:
        """Take no arguments and carry a fixed message; nothing of the group or graph is named."""
        super().__init__("the policy graph changed after the guard was read")
