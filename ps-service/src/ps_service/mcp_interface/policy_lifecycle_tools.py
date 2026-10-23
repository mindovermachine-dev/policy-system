"""Policy-lifecycle tools of the PS MCP server.

Registers `create-policy-draft`, `update-policy-draft`, `add-standard-to-draft`,
`update-standard-draft`, `add-control-to-draft`, `update-control-draft`, `get-policy`,
`propose-policy`, `approve-policy`, `reject-policy` and `revert-policy-to-draft` on the shared
`server` from `tool_core`. Each tool delegates to `ps_service.policy_lifecycle.service`, which owns
the behaviour. This module imports only `tool_core` (never another tool group); the patch seam
names are looked up there at call time as `tool_core.<Name>`.

Business Rules:
    - Draft authoring is gated by the policy-author and draft-access gates; `approve-policy` and
      `reject-policy` are body-gated, so an unknown policy id is reported before an access denial.
    - A malformed `fields` or `standards` argument is returned as an `error: <message>` string by
      the tool that received it, never raised across the MCP boundary.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, cast

from pydantic import Field

from ps_service.api.errors import (
    AccessDeniedError,
    AuthorizationStoreUnavailableError,
    GraphWriteUnavailableError,
)
from ps_service.api.graph_write_access import graph_write_access
from ps_service.authz.models import AccessRole
from ps_service.config import load_config
from ps_service.mcp_interface import tool_core
from ps_service.mcp_interface.errors import McpGraphUnavailableError
from ps_service.mcp_interface.tool_core import POLICY_AUTHOR_GATE as _POLICY_AUTHOR_GATE
from ps_service.mcp_interface.tool_core import POLICY_DRAFT_ACCESS_GATE as _POLICY_DRAFT_ACCESS_GATE
from ps_service.mcp_interface.tool_core import gate_environment as _gate_environment
from ps_service.mcp_interface.tool_core import gated_server_tool as _gated_tool
from ps_service.mcp_interface.tool_core import resolve_graph as _resolve_graph
from ps_service.mcp_interface.tool_core import (
    resolve_policy_lifecycle_actor as _resolve_policy_lifecycle_actor,
)
from ps_service.mcp_interface.tool_core import run_mcp_action as _run_mcp_action
from ps_service.mcp_interface.tool_gates import (
    BypassPolicy,
    GateKind,
    ToolGate,
    lifecycle_role_enforcer,
)
from ps_service.policy_lifecycle.errors import (
    PolicyCapabilityAlreadyGovernedError,
    PolicyCapabilityNotFoundError,
    PolicyControlNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyGovernanceConflictError,
    PolicyIncompleteForProposalError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
    PolicyNotFoundError,
    PolicySelfApprovalBlockedError,
    PolicyStandardNotFoundError,
    PolicySupersedePriorNotApprovedError,
    PolicyTitleAlreadyExistsError,
)
from ps_service.policy_lifecycle.graph_submission import lifecycle_submitter
from ps_service.policy_lifecycle.service import (
    _CONTROL_IMPLEMENTATION_STATUS_VALUES,  # pyright: ignore[reportPrivateUsage] -- same drift-avoidance reuse as `_POLICY_PATCHABLE_FIELDS`
    _CONTROL_PATCHABLE_FIELDS,  # pyright: ignore[reportPrivateUsage] -- same drift-avoidance reuse as `_POLICY_PATCHABLE_FIELDS`
    _POLICY_PATCHABLE_FIELDS,  # pyright: ignore[reportPrivateUsage] -- declared once in service.py, imported here to avoid drift between the two layers
    _STANDARD_IMPLEMENTATION_STATUS_VALUES,  # pyright: ignore[reportPrivateUsage] -- same drift-avoidance reuse as `_POLICY_PATCHABLE_FIELDS`
    _STANDARD_PATCHABLE_FIELDS,  # pyright: ignore[reportPrivateUsage] -- same drift-avoidance reuse as `_POLICY_PATCHABLE_FIELDS`
    ControlDraftInput,
    StandardDraftInput,
)
from ps_service.policy_lifecycle.service import add_control_to_draft as run_add_control_to_draft
from ps_service.policy_lifecycle.service import add_standard_to_draft as run_add_standard_to_draft
from ps_service.policy_lifecycle.service import approve_policy as run_approve_policy
from ps_service.policy_lifecycle.service import create_policy_draft as run_create_policy_draft
from ps_service.policy_lifecycle.service import get_policy as run_get_policy
from ps_service.policy_lifecycle.service import propose_policy as run_propose_policy
from ps_service.policy_lifecycle.service import reject_policy as run_reject_policy
from ps_service.policy_lifecycle.service import revert_policy_to_draft as run_revert_policy_to_draft
from ps_service.policy_lifecycle.service import update_control_draft as run_update_control_draft
from ps_service.policy_lifecycle.service import update_policy_draft as run_update_policy_draft
from ps_service.policy_lifecycle.service import update_standard_draft as run_update_standard_draft

if TYPE_CHECKING:
    from collections.abc import Mapping


_POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE = (
    "error: this action requires a real authenticated caller (the local-test bypass counts as one)"
)


# `approve-policy` / `reject-policy` keep their check order (the Policy is read first, so an unknown
# id is reported before an access denial), so the role gate is a body gate: the policy-lifecycle
# service calls the enforcer the tool hands it, at the existing position. Under the bypass the role
# is checked for the lifecycle fallback identity and the audit row names the bypass sentinel.
_POLICY_TRANSITION_GATE = ToolGate(
    kind=GateKind.BODY,
    role=AccessRole.POLICY_MANAGER,
    bypass=BypassPolicy.LIFECYCLE_IDENTITY,
    unauthenticated_message=_POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE,
)


class _MalformedPatchFieldsError(Exception):
    """A `fields` argument to one of the draft-content tools is shaped wrong.

    Raised only by `_parse_patch_fields`; each draft-content tool catches it at its own call site
    and
    returns it as an `error: ` string.
    """


def _parse_patch_fields(
    fields: dict[str, object] | None,
    *,
    allowed: frozenset[str],
    enum_checks: Mapping[str, tuple[str, ...]] = MappingProxyType({}),
) -> dict[str, object]:
    """Validate `fields` against a tool's allow-list of patchable field names.

    `None` (the parameter omitted) is a no-op update and yields `{}`. Every key must be in `allowed`
    and every value must be a string or `None`. `None` clears the field in an update tool and leaves
    it
    unset in a create tool; it is distinct from an absent key, which leaves any existing value
    alone.

    Args:
        fields: The raw `fields` argument as the MCP caller supplied it, or `None`.
        allowed: The fixed set of field names the calling tool accepts.
        enum_checks: Field name to the tuple of values that field is restricted to; checked only
            when
            the field is present and not `None`.

    Returns:
        `fields` itself once every key and value is validated, or `{}` when `fields` is `None`.

    Raises:
        _MalformedPatchFieldsError: A key is not in `allowed`, a value is neither a string nor
        `None`,
            or a restricted field holds a value outside its tuple.

    Side Effects:
        None.
    """
    if fields is None:
        return {}
    for key, value in fields.items():
        if key not in allowed:
            raise _MalformedPatchFieldsError(f"fields.{key} is not a patchable field")
        if value is not None and not isinstance(value, str):
            raise _MalformedPatchFieldsError(f"fields.{key} must be a string or null")
        allowed_values = enum_checks.get(key)
        if allowed_values is not None and value is not None and value not in allowed_values:
            raise _MalformedPatchFieldsError(
                f"fields.{key} must be one of {allowed_values} or null"
            )
    return fields


_CREATE_POLICY_DRAFT_ERRORS = (
    GraphWriteUnavailableError,
    PolicyCapabilityAlreadyGovernedError,
    PolicyCapabilityNotFoundError,
    PolicyNotFoundError,
    PolicySupersedePriorNotApprovedError,
    PolicyTitleAlreadyExistsError,
    PolicyLifecycleGraphUnavailableError,
)


_CREATE_POLICY_DRAFT_CONTROL_TYPES = ("automated", "manual")


class _MalformedPolicyDraftStandardsError(Exception):
    """The `standards` argument of `create-policy-draft` is shaped wrong.

    Raised only by `_parse_policy_draft_standards` and the helpers it calls; `create_policy_draft`
    catches it and returns it as an `error: ` string. The message names the offending
    `standards[i]` or `standards[i].controls[j]` position so a caller can locate the bad entry.
    """


def _require_standards_str_field(body: dict[str, object], field: str, *, where: str) -> str:
    """Return a required non-empty string field of one `standards` entry.

    Args:
        body: The `standards[i]` or `standards[i].controls[j]` object being parsed.
        field: Name of the key to read from `body`.
        where: Position label of `body` (for example `standards[0]`), used in the error message.

    Returns:
        The non-empty string stored under `field`.

    Raises:
        _MalformedPolicyDraftStandardsError: `field` is absent, not a string, or empty.
    """
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise _MalformedPolicyDraftStandardsError(f"{where}.{field} must be a non-empty string")
    return value


def _parse_control_draft_input(raw: object, *, where: str) -> ControlDraftInput:
    """Parse one `standards[i].controls[j]` entry into a `ControlDraftInput`.

    Args:
        raw: The entry as supplied by the caller.
        where: Position label of the entry, used in error messages.

    Returns:
        A `ControlDraftInput` with the entry's `title` and its `control_type` (default `"manual"`).

    Raises:
        _MalformedPolicyDraftStandardsError: `raw` is not an object, `title` is missing or empty, or
            `control_type` is not `"automated"` or `"manual"`.
    """
    if not isinstance(raw, dict):
        raise _MalformedPolicyDraftStandardsError(f"{where} must be an object")
    body = cast("dict[str, object]", raw)
    title = _require_standards_str_field(body, "title", where=where)
    control_type = body.get("control_type", "manual")
    if control_type not in _CREATE_POLICY_DRAFT_CONTROL_TYPES:
        raise _MalformedPolicyDraftStandardsError(
            f"{where}.control_type must be 'automated' or 'manual'"
        )
    return ControlDraftInput(title=title, control_type=control_type)


def _parse_standard_draft_input(raw: object, *, where: str) -> StandardDraftInput:
    """Parse one `standards[i]` entry, with its optional `controls`, into a `StandardDraftInput`.

    Args:
        raw: The entry as supplied by the caller.
        where: Position label of the entry, used in error messages.

    Returns:
        A `StandardDraftInput` with the entry's `title` and a tuple of parsed controls (empty when
        `controls` is omitted).

    Raises:
        _MalformedPolicyDraftStandardsError: `raw` is not an object, `title` is missing or empty,
            `controls` is not a list, or a control entry is malformed.
    """
    if not isinstance(raw, dict):
        raise _MalformedPolicyDraftStandardsError(f"{where} must be an object")
    body = cast("dict[str, object]", raw)
    title = _require_standards_str_field(body, "title", where=where)
    raw_controls = body.get("controls", [])
    if not isinstance(raw_controls, list):
        raise _MalformedPolicyDraftStandardsError(f"{where}.controls must be a list")
    controls = tuple(
        _parse_control_draft_input(item, where=f"{where}.controls[{index}]")
        for index, item in enumerate(cast("list[object]", raw_controls))
    )
    return StandardDraftInput(title=title, controls=controls)


def _parse_policy_draft_standards(
    standards: list[dict[str, object]] | None,
) -> tuple[StandardDraftInput, ...]:
    """Parse the optional `standards` argument of `create-policy-draft`, top to bottom.

    Args:
        standards: The list of Standard objects as supplied by the caller, or `None` when omitted.

    Returns:
        One `StandardDraftInput` per entry, in order; an empty tuple when `standards` is `None`.

    Raises:
        _MalformedPolicyDraftStandardsError: An entry, or a nested control, is malformed.
    """
    if standards is None:
        return ()
    return tuple(
        _parse_standard_draft_input(item, where=f"standards[{index}]")
        for index, item in enumerate(standards)
    )


@_gated_tool("create-policy-draft", _POLICY_AUTHOR_GATE)
def create_policy_draft(
    title: Annotated[str, Field(min_length=1)],
    standards: list[dict[str, object]] | None = None,
    supersedes_policy_id: Annotated[str, Field(min_length=1)] | None = None,
    capability_ids: list[Annotated[str, Field(min_length=1)]] | None = None,
) -> dict[str, object] | str:
    """Create a new draft Policy owned by the caller, or an amendment of an approved Policy.

    Requires a real authenticated session (the local-test bypass counts as one). The Policy, and
    every
    Standard and Control created with it, starts in `draft` status.

    Args:
        title: Policy title; at least 1 character.
        standards: Optional list of objects `{"title": <str>, "controls": [...]}` (`controls`
            optional,
            default `[]`); each control is `{"title": <str>, "control_type": "automated" |
            "manual"}`
            (`control_type` optional, default `"manual"`). Every `title` must be a non-empty string.
            Omit it or pass `null` for a title-only draft. Ignored when `supersedes_policy_id` is
            set.
        supersedes_policy_id: Optional id of an `approved` Policy to amend. When omitted this
            creates an
            ordinary version-1 Policy whose id is derived from `title` alone, so repeating a title
            is an
            error. When set, it creates a successor draft whose id derives from both `title` and
            this
            id, with `version` one higher (still a string), a `SUPERSEDED_BY` edge from the prior
            Policy,
            and Standard and Control children forked as new, independently editable copies of the
            prior
            Policy's current children; the prior nodes are not changed. At least 1 character.
        capability_ids: Optional ids of existing Capabilities the new Policy will govern; each at
            least
            1 character. The governing edges are written together with the Policy, so it governs
            them
            from creation, while still a draft. Duplicates are dropped, order kept. Every id must
            name
            an existing Capability that no Policy governs yet. Ignored when `supersedes_policy_id`
            is
            set: a fork's Capabilities stay with the prior Policy and move to the successor when it
                is
            approved.

    Returns:
        `{"policy_id", "title", "status", "version", "owner_subject", "standard_ids", "control_ids",
        "superseded_policy_id", "governed_capability_ids"}`. `standard_ids` and `control_ids` list
        every
        created or forked child (empty for a title-only draft) and can be used in later
        draft-editing
        calls; `superseded_policy_id` is `null` unless this was an amendment;
        `governed_capability_ids`
        is empty when none are governed or on an amendment.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `standards` or a nested `controls` entry is malformed (not objects, a missing or empty
          `title`, or an invalid `control_type`).
        - `supersedes_policy_id` names no Policy, or a Policy that is not `approved`.
        - A `capability_ids` entry names no Capability or one that is already governed; nothing is
          written.
        - The computed Policy id already exists.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the Policy and its children to the policy graph in one guarded step and records an
        audit
        event.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            parsed_standards = _parse_policy_draft_standards(standards)
        except _MalformedPolicyDraftStandardsError as exc:
            return f"error: {exc}"
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        audit_store = tool_core.PsycopgAuditStore(config)
        try:
            # Fail closed before any read or write: no write path, no lifecycle write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_create_policy_draft(
                actor=actor,
                title=title,
                standards=parsed_standards,
                supersedes_policy_id=supersedes_policy_id,
                capability_ids=tuple(capability_ids or ()),
                graph=graph,
                audit_store=audit_store,
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _CREATE_POLICY_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
            "version": result.version,
            "owner_subject": result.owner_subject,
            "standard_ids": list(result.standard_ids),
            "control_ids": list(result.control_ids),
            "superseded_policy_id": result.superseded_policy_id,
            "governed_capability_ids": list(result.capability_ids),
        }

    return _run_mcp_action("create_policy_draft", _body, entity_id=supersedes_policy_id)


_UPDATE_POLICY_DRAFT_ERRORS = (
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("update-policy-draft", _POLICY_DRAFT_ACCESS_GATE)
def update_policy_draft(
    policy_id: Annotated[str, Field(min_length=1)],
    fields: dict[str, object] | None = None,
) -> dict[str, object] | str:
    """Change a subset of a draft Policy's content fields, leaving the rest as they are.

    Usable only while the Policy is in `draft` status, and only by its owner or a caller holding
    `SystemOwner` or `SystemAdmin`. Requires a real authenticated session (the local-test bypass
    counts as one).

    Args:
        policy_id: Id of the Policy to change; at least 1 character.
        fields: Partial-update map; only the supplied keys change. Each value is a string or JSON
            `null`, which clears the field. Allowed keys: `description`, `scope_in`, `scope_out`,
            `normative_commitments`, `review_cadence`, `exception_pathway`, `measurable_outcomes`,
            `capability_grouping_rationale`. `title`, `status`, `owner_subject`, `owner_issuer` and
            `version` can never be patched. Omit it or pass `null` for a no-op update.

    Returns:
        `{"policy_id", "updated_fields"}`, where `updated_fields` is every key supplied in `fields`,
        sorted.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `fields` names an unknown key or a value that is neither a string nor `null`.
        - No Policy has `policy_id`.
        - The caller is neither the owner nor a `SystemOwner` or `SystemAdmin`.
        - The Policy is not in `draft` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the changed fields to the policy graph; no audit event is written. May also backfill
        a missing `version` and `status` on the Policy tree.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            parsed_fields = _parse_patch_fields(fields, allowed=_POLICY_PATCHABLE_FIELDS)
        except _MalformedPatchFieldsError as exc:
            return f"error: {exc}"
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        access_role_store = tool_core.PsycopgAccessRoleStore(
            config, audit_store=tool_core.PsycopgAuditStore(config)
        )
        try:
            # Fail closed before any read or write: no write path, no draft edit.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_update_policy_draft(
                actor=actor,
                policy_id=policy_id,
                fields=parsed_fields,
                graph=graph,
                access_role_store=access_role_store,
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _UPDATE_POLICY_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "updated_fields": list(result.updated_fields),
        }

    return _run_mcp_action("update_policy_draft", _body, entity_id=policy_id)


_ADD_STANDARD_TO_DRAFT_ERRORS = (
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("add-standard-to-draft", _POLICY_DRAFT_ACCESS_GATE)
def add_standard_to_draft(
    policy_id: Annotated[str, Field(min_length=1)],
    title: Annotated[str, Field(min_length=1)],
    fields: dict[str, object] | None = None,
) -> dict[str, object] | str:
    """Add a new Standard under a draft Policy.

    Usable only while the Policy is in `draft` status, and only by its owner or a caller holding
    `SystemOwner` or `SystemAdmin`. Requires a real authenticated session (the local-test bypass
    counts
    as one). The Standard is linked to the Policy with `SUPPORTED_BY` and always starts in `draft`
    governance status, whatever its `implementation_status`.

    Args:
        policy_id: Id of the draft Policy; at least 1 character.
        title: Standard title; at least 1 character.
        fields: Optional content to set at creation. Each value is a string, or JSON `null`, which
            leaves the field unset. Allowed keys: `description`, `implementation_status`,
            `procedure`,
            `implementer_role`, `reviewer_role`, `applicability_boundary`, `verification_notes`,
            `change_rationale`. `implementation_status` must be one of `draft`, `implemented`,
            `reviewed`, `deprecated`, and defaults to `draft`. `title` and `status` cannot be set
            here.

    Returns:
        `{"standard_id", "policy_id", "title", "status"}`; `standard_id` can be used in later calls
        immediately.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `fields` names an unknown key, holds a value that is neither a string nor `null`, or holds
          an
          invalid `implementation_status`.
        - No Policy has `policy_id`.
        - The caller is neither the owner nor a `SystemOwner` or `SystemAdmin`.
        - The Policy is not in `draft` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the Standard and its link to the policy graph; no audit event is written.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            parsed_fields = _parse_patch_fields(
                fields,
                allowed=_STANDARD_PATCHABLE_FIELDS,
                enum_checks={"implementation_status": _STANDARD_IMPLEMENTATION_STATUS_VALUES},
            )
        except _MalformedPatchFieldsError as exc:
            return f"error: {exc}"
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        access_role_store = tool_core.PsycopgAccessRoleStore(
            config, audit_store=tool_core.PsycopgAuditStore(config)
        )
        try:
            # Fail closed before any read or write: no write path, no draft write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_add_standard_to_draft(
                actor=actor,
                policy_id=policy_id,
                title=title,
                fields=parsed_fields,
                graph=graph,
                access_role_store=access_role_store,
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _ADD_STANDARD_TO_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "standard_id": result.standard_id,
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
        }

    return _run_mcp_action("add_standard_to_draft", _body, entity_id=policy_id)


_UPDATE_STANDARD_DRAFT_ERRORS = (
    GraphWriteUnavailableError,
    PolicyStandardNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("update-standard-draft", _POLICY_DRAFT_ACCESS_GATE)
def update_standard_draft(
    standard_id: Annotated[str, Field(min_length=1)],
    fields: dict[str, object] | None = None,
) -> dict[str, object] | str:
    """Change a subset of a draft Standard's content fields, leaving the rest as they are.

    A Standard has no owner of its own: only the owner of its parent Policy, or a caller holding
    `SystemOwner` or `SystemAdmin`, may change it. Usable only while the Standard itself (not the
    parent Policy) is in `draft` status. Requires a real authenticated session (the local-test
    bypass
    counts as one).

    Args:
        standard_id: Id of the Standard to change; at least 1 character.
        fields: Partial-update map; only the supplied keys change. Each value is a string or JSON
            `null`, which clears the field. Allowed keys: `description`, `implementation_status`,
            `procedure`, `implementer_role`, `reviewer_role`, `applicability_boundary`,
            `verification_notes`, `change_rationale`. `implementation_status` must be one of
            `draft`,
            `implemented`, `reviewed`, `deprecated`. `title` and `status` can never be patched. Omit
            it
            or pass `null` for a no-op update.

    Returns:
        `{"standard_id", "policy_id", "title", "status"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `fields` names an unknown key, holds a value that is neither a string nor `null`, or holds
          an
          invalid `implementation_status`.
        - No Standard has `standard_id`.
        - The caller is neither the parent Policy's owner nor a `SystemOwner` or `SystemAdmin`.
        - The Standard is not in `draft` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the changed fields to the policy graph; no audit event is written.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            parsed_fields = _parse_patch_fields(
                fields,
                allowed=_STANDARD_PATCHABLE_FIELDS,
                enum_checks={"implementation_status": _STANDARD_IMPLEMENTATION_STATUS_VALUES},
            )
        except _MalformedPatchFieldsError as exc:
            return f"error: {exc}"
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        access_role_store = tool_core.PsycopgAccessRoleStore(
            config, audit_store=tool_core.PsycopgAuditStore(config)
        )
        try:
            # Fail closed before any read or write: no write path, no draft write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_update_standard_draft(
                actor=actor,
                standard_id=standard_id,
                fields=parsed_fields,
                graph=graph,
                access_role_store=access_role_store,
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _UPDATE_STANDARD_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "standard_id": result.standard_id,
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
        }

    return _run_mcp_action("update_standard_draft", _body, entity_id=standard_id)


_ADD_CONTROL_TO_DRAFT_ERRORS = (
    GraphWriteUnavailableError,
    PolicyStandardNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


# `add-control-to-draft` excludes `"type"` from its `fields` allow-list: the top-level
# `control_type` parameter is the only way to set a Control's type at creation, and
# `update-control-draft` (which keeps the full `_CONTROL_PATCHABLE_FIELDS`) is the only
# post-creation path to change it.
_ADD_CONTROL_TO_DRAFT_PATCHABLE_FIELDS = _CONTROL_PATCHABLE_FIELDS - {"type"}


@_gated_tool("add-control-to-draft", _POLICY_DRAFT_ACCESS_GATE)
def add_control_to_draft(
    standard_id: Annotated[str, Field(min_length=1)],
    title: Annotated[str, Field(min_length=1)],
    control_type: Annotated[str, Field(min_length=1)] = "manual",
    fields: dict[str, object] | None = None,
) -> dict[str, object] | str:
    """Add a new Control under a draft Standard.

    Ownership comes from the Standard's parent Policy: only that Policy's owner, or a caller holding
    `SystemOwner` or `SystemAdmin`, may add a Control, and only while the Standard itself (not the
    parent Policy) is in `draft` status. Requires a real authenticated session (the local-test
    bypass
    counts as one). The Control is linked to the Standard with `IMPLEMENTED_BY` and always starts in
    `draft` governance status, whatever its `implementation_status`.

    Args:
        standard_id: Id of the draft Standard; at least 1 character.
        title: Control title; at least 1 character.
        control_type: `"automated"` or `"manual"`; default `"manual"`. This is the only way to set
            the
            type at creation.
        fields: Optional content to set at creation. Each value is a string, or JSON `null`, which
            leaves the field unset. Allowed keys: `description`, `implementation_status`,
            `execution_frequency`, `last_test_date`, `next_review_date`, `evidence_ref`,
            `pass_fail_criteria`, `execution_method`, `evidence_plan`, `executor_role`,
            `reviewer_role`,
            `risk_alignment_rationale`. `implementation_status` must be one of `planned`,
            `implemented`, `reviewed`, `deprecated`, and defaults to `planned`. `title`, `status`
            and
            `type` cannot be set here; `type` is rejected, use `control_type`.

    Returns:
        `{"control_id", "standard_id", "policy_id", "title", "status"}`; `control_id` can be used in
        later calls immediately.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `control_type` is not `"automated"` or `"manual"`.
        - `fields` names an unknown key (including `type`), holds a value that is neither a string
          nor
          `null`, or holds an invalid `implementation_status`.
        - No Standard has `standard_id`.
        - The caller is neither the parent Policy's owner nor a `SystemOwner` or `SystemAdmin`.
        - The Standard is not in `draft` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the Control and its link to the policy graph; no audit event is written.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        if control_type not in _CREATE_POLICY_DRAFT_CONTROL_TYPES:
            return "error: control_type must be 'automated' or 'manual'"
        try:
            parsed_fields = _parse_patch_fields(
                fields,
                allowed=_ADD_CONTROL_TO_DRAFT_PATCHABLE_FIELDS,
                enum_checks={"implementation_status": _CONTROL_IMPLEMENTATION_STATUS_VALUES},
            )
        except _MalformedPatchFieldsError as exc:
            return f"error: {exc}"
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        access_role_store = tool_core.PsycopgAccessRoleStore(
            config, audit_store=tool_core.PsycopgAuditStore(config)
        )
        try:
            # Fail closed before any read or write: no write path, no draft write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_add_control_to_draft(
                actor=actor,
                standard_id=standard_id,
                title=title,
                control_type=control_type,
                fields=parsed_fields,
                graph=graph,
                access_role_store=access_role_store,
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _ADD_CONTROL_TO_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "control_id": result.control_id,
            "standard_id": result.standard_id,
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
        }

    return _run_mcp_action("add_control_to_draft", _body, entity_id=standard_id)


_UPDATE_CONTROL_DRAFT_ERRORS = (
    GraphWriteUnavailableError,
    PolicyControlNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("update-control-draft", _POLICY_DRAFT_ACCESS_GATE)
def update_control_draft(
    control_id: Annotated[str, Field(min_length=1)],
    fields: dict[str, object] | None = None,
) -> dict[str, object] | str:
    """Change a subset of a draft Control's content fields, leaving the rest as they are.

    Ownership comes from the root Policy two levels up (Policy to Standard to Control): only that
    Policy's owner, or a caller holding `SystemOwner` or `SystemAdmin`, may change the Control.
    Usable
    only while the Control itself (not its Standard or Policy) is in `draft` status. Requires a real
    authenticated session (the local-test bypass counts as one).

    Args:
        control_id: Id of the Control to change; at least 1 character.
        fields: Partial-update map; only the supplied keys change. Each value is a string or JSON
            `null`, which clears the field. Allowed keys: `description`, `implementation_status`,
            `type`, `execution_frequency`, `last_test_date`, `next_review_date`, `evidence_ref`,
            `pass_fail_criteria`, `execution_method`, `evidence_plan`, `executor_role`,
            `reviewer_role`,
            `risk_alignment_rationale`. Unlike `add-control-to-draft`, `type` can be changed here,
            and
            this is the only way to change it after creation. `implementation_status` must be one of
            `planned`, `implemented`, `reviewed`, `deprecated`. `title` and `status` can never be
            patched. Omit it or pass `null` for a no-op update.

    Returns:
        `{"control_id", "standard_id", "policy_id", "title", "status"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - `fields` names an unknown key, holds a value that is neither a string nor `null`, or holds
          an
          invalid `implementation_status`.
        - No Control has `control_id`.
        - The caller is neither the root Policy's owner nor a `SystemOwner` or `SystemAdmin`.
        - The Control is not in `draft` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Writes the changed fields to the policy graph; no audit event is written.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            parsed_fields = _parse_patch_fields(
                fields,
                allowed=_CONTROL_PATCHABLE_FIELDS,
                enum_checks={"implementation_status": _CONTROL_IMPLEMENTATION_STATUS_VALUES},
            )
        except _MalformedPatchFieldsError as exc:
            return f"error: {exc}"
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        access_role_store = tool_core.PsycopgAccessRoleStore(
            config, audit_store=tool_core.PsycopgAuditStore(config)
        )
        try:
            # Fail closed before any read or write: no write path, no draft write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_update_control_draft(
                actor=actor,
                control_id=control_id,
                fields=parsed_fields,
                graph=graph,
                access_role_store=access_role_store,
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _UPDATE_CONTROL_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "control_id": result.control_id,
            "standard_id": result.standard_id,
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
        }

    return _run_mcp_action("update_control_draft", _body, entity_id=control_id)


_GET_POLICY_ERRORS = (
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("get-policy", _POLICY_DRAFT_ACCESS_GATE)
def get_policy(policy_id: Annotated[str, Field(min_length=1)]) -> dict[str, object] | str:
    """Read one Policy together with its full Standard and Control tree.

    A Policy is visible to any authenticated caller in every status, `draft` included, so a draft
    can appear beside approved content; check `status` before treating a Policy as in force.
    Requires a real authenticated session (the local-test bypass counts as one).

    Args:
        policy_id: Id of the Policy to read; at least 1 character.

    Returns:
        `{"policy_id", "title", "status", "version", "owner_subject", "owner_issuer", "standards"}`,
        where `standards` is a list of `{"standard_id", "title", "status", "controls"}` and each
        `controls` is a list of `{"control_id", "title", "control_type", "status"}`.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Reads the policy graph. When an older Policy tree is missing data this read can backfill it,
        which writes to the graph; a tree that needs no backfill is not written.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        try:
            result = run_get_policy(
                policy_id=policy_id,
                graph=graph,
                # Resolved only if a backfill must be written (a read of a backfilled tree needs
                # no gateway).
                submitter_source=lambda: lifecycle_submitter(graph_write_access(config).gateway),
                graph_name=config.falkordb_graph,
            )
        except _GET_POLICY_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "title": result.title,
            "status": result.status,
            "version": result.version,
            "owner_subject": result.owner_subject,
            "owner_issuer": result.owner_issuer,
            "standards": [
                {
                    "standard_id": standard.standard_id,
                    "title": standard.title,
                    "status": standard.status,
                    "controls": [
                        {
                            "control_id": control.control_id,
                            "title": control.title,
                            "control_type": control.control_type,
                            "status": control.status,
                        }
                        for control in standard.controls
                    ],
                }
                for standard in result.standards
            ],
        }

    return _run_mcp_action("get_policy", _body)


_PROPOSE_POLICY_ERRORS = (
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyIncompleteForProposalError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("propose-policy", _POLICY_AUTHOR_GATE)
def propose_policy(policy_id: Annotated[str, Field(min_length=1)]) -> dict[str, object] | str:
    """Move a draft Policy and its whole tree to `proposed`.

    Only the Policy's owner may propose it, and only while it is in `draft` status with at least one
    Standard attached. Requires a real authenticated session (the local-test bypass counts as one).

    Args:
        policy_id: Id of the Policy to propose; at least 1 character.

    Returns:
        `{"policy_id", "status", "standard_ids", "control_ids"}`, listing every Standard and Control
        in
        the tree.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The caller does not own the Policy.
        - The Policy is not in `draft` status, or has no Standards.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Changes the status of the Policy and every Standard and Control in its tree in one cascading
        graph write and records an audit event.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        audit_store = tool_core.PsycopgAuditStore(config)
        try:
            # Fail closed before any read or write: no write path, no lifecycle write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_propose_policy(
                actor=actor,
                policy_id=policy_id,
                graph=graph,
                audit_store=audit_store,
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _PROPOSE_POLICY_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "status": result.status,
            "standard_ids": list(result.standard_ids),
            "control_ids": list(result.control_ids),
        }

    return _run_mcp_action("propose_policy", _body)


_APPROVE_POLICY_ERRORS = (
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    AccessDeniedError,
    PolicySelfApprovalBlockedError,
    PolicyInvalidStatusTransitionError,
    PolicyGovernanceConflictError,
    PolicyLifecycleGraphUnavailableError,
    AuthorizationStoreUnavailableError,
)


@_gated_tool("approve-policy", _POLICY_TRANSITION_GATE)
def approve_policy(policy_id: Annotated[str, Field(min_length=1)]) -> dict[str, object] | str:
    """Move a proposed Policy and its whole tree to `approved`.

    Requires the `PolicyManager` role, and the caller must not be the Policy's owner: self-approval
    is
    always refused, even for a `PolicyManager` who owns it. Usable only while the Policy is in
    `proposed` status. Requires a real authenticated session (the local-test bypass counts as one).
    When the Policy amends an approved prior Policy (created with `supersedes_policy_id`), that
    prior
    tree is deprecated in the same call, recorded as its own audit event. If the prior Policy
    governs
    Capabilities, their governing edges move to the approved Policy in the same all-or-nothing write
    as
    the status change.

    Args:
        policy_id: Id of the Policy to approve; at least 1 character.

    Returns:
        `{"policy_id", "status", "standard_ids", "control_ids", "auto_deprecated_policy_id",
        "governed_capability_ids"}`. `auto_deprecated_policy_id` is `None` unless a prior Policy was
        deprecated; `governed_capability_ids` lists the Capabilities whose governance moved (empty
        when
        none).

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The caller does not hold `PolicyManager`, or is the Policy's owner.
        - The Policy is not in `proposed` status.
        - The Capabilities governed by the superseded Policy changed during approval; nothing was
          changed, retry.
        - The policy graph or the authorization store cannot be reached, or the write path is
          unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Changes the status of the whole tree (and of the superseded tree, when there is one) and
        moves
        Capability governance in one graph write, and records audit events.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        audit_store = tool_core.PsycopgAuditStore(config)
        try:
            # Fail closed before any read or write: no write path, no lifecycle write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_approve_policy(
                actor=actor,
                policy_id=policy_id,
                graph=graph,
                audit_store=audit_store,
                enforce_policy_manager=lifecycle_role_enforcer(_gate_environment, "approve-policy"),
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _APPROVE_POLICY_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "status": result.status,
            "standard_ids": list(result.standard_ids),
            "control_ids": list(result.control_ids),
            "auto_deprecated_policy_id": result.auto_deprecated_policy_id,
            "governed_capability_ids": list(result.governed_capability_ids),
        }

    return _run_mcp_action("approve_policy", _body)


_REJECT_POLICY_ERRORS = (
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    AccessDeniedError,
    PolicySelfApprovalBlockedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
    AuthorizationStoreUnavailableError,
)


@_gated_tool("reject-policy", _POLICY_TRANSITION_GATE)
def reject_policy(policy_id: Annotated[str, Field(min_length=1)]) -> dict[str, object] | str:
    """Move a proposed Policy and its whole tree back to `draft`.

    Requires the `PolicyManager` role, and the caller must not be the Policy's owner: self-rejection
    is
    always refused, even for a `PolicyManager` who owns it. Usable only while the Policy is in
    `proposed` status. Unlike approval, no prior Policy is deprecated. Requires a real authenticated
    session (the local-test bypass counts as one).

    Args:
        policy_id: Id of the Policy to reject; at least 1 character.

    Returns:
        `{"policy_id", "status", "standard_ids", "control_ids"}`, listing every Standard and Control
        in
        the tree.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The caller does not hold `PolicyManager`, or is the Policy's owner.
        - The Policy is not in `proposed` status.
        - The policy graph or the authorization store cannot be reached, or the write path is
          unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Changes the status of the Policy and every Standard and Control in its tree in one cascading
        graph write and records an audit event.
        A caller refused by the role gate gets the unchanged `error: ` string and one
        best-effort `access.gate` audit row for this operation; no row is written for a caller
        with no verified identity.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        audit_store = tool_core.PsycopgAuditStore(config)
        try:
            # Fail closed before any read or write: no write path, no lifecycle write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_reject_policy(
                actor=actor,
                policy_id=policy_id,
                graph=graph,
                audit_store=audit_store,
                enforce_policy_manager=lifecycle_role_enforcer(_gate_environment, "reject-policy"),
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _REJECT_POLICY_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "status": result.status,
            "standard_ids": list(result.standard_ids),
            "control_ids": list(result.control_ids),
        }

    return _run_mcp_action("reject_policy", _body)


_REVERT_POLICY_TO_DRAFT_ERRORS = (
    GraphWriteUnavailableError,
    PolicyNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
)


@_gated_tool("revert-policy-to-draft", _POLICY_AUTHOR_GATE)
def revert_policy_to_draft(
    policy_id: Annotated[str, Field(min_length=1)],
) -> dict[str, object] | str:
    """Move a proposed Policy and its whole tree back to `draft`, as its owner.

    Owner-only: unlike `reject-policy` there is no `PolicyManager` role check, and a `PolicyManager`
    who is not the owner is refused like any other non-owner. Usable only while the Policy is in
    `proposed` status. Requires a real authenticated session (the local-test bypass counts as one).

    Args:
        policy_id: Id of the Policy to revert; at least 1 character.

    Returns:
        `{"policy_id", "status", "standard_ids", "control_ids"}`, listing every Standard and Control
        in
        the tree.

    Errors (returned as a string beginning `error: `, never raised):
        - The caller has no real authenticated session.
        - No Policy has `policy_id`.
        - The caller does not own the Policy.
        - The Policy is not in `proposed` status.
        - The policy graph cannot be reached or its write path is unavailable.
        - Any other unexpected failure (a fixed generic message).

    Side Effects:
        Changes the status of the Policy and every Standard and Control in its tree in one cascading
        graph write and records an audit event.
    """
    config = load_config()
    actor = _resolve_policy_lifecycle_actor(config)

    def _body() -> dict[str, object] | str:
        if actor is None:
            return _POLICY_LIFECYCLE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE
        try:
            graph = _resolve_graph(config)
        except McpGraphUnavailableError as exc:
            return f"error: {exc}"
        audit_store = tool_core.PsycopgAuditStore(config)
        try:
            # Fail closed before any read or write: no write path, no lifecycle write.
            submitter = lifecycle_submitter(graph_write_access(config).gateway)
            result = run_revert_policy_to_draft(
                actor=actor,
                policy_id=policy_id,
                graph=graph,
                audit_store=audit_store,
                submitter=submitter,
                graph_name=config.falkordb_graph,
            )
        except _REVERT_POLICY_TO_DRAFT_ERRORS as exc:
            return f"error: {exc}"
        return {
            "policy_id": result.policy_id,
            "status": result.status,
            "standard_ids": list(result.standard_ids),
            "control_ids": list(result.control_ids),
        }

    return _run_mcp_action("revert_policy_to_draft", _body)
