"""`user.invite` typed audit details (issue #195, AC-BI-002/009/010/013)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ps_service.audit import is_known_resource_type, resolve_details_model
from ps_service.invitations.audit_actions import USER_INVITE_ACTION, UserInviteDetails

_EMAIL = "target@example.com"


def test_user_invite_is_registered_with_a_typed_details_model() -> None:
    assert USER_INVITE_ACTION == "user.invite"
    assert resolve_details_model("user.invite") is UserInviteDetails
    assert UserInviteDetails(invitee_email=_EMAIL).invitee_email == _EMAIL


@pytest.mark.parametrize("field", ["itoken", "invite_url", "token", "error"])
def test_user_invite_details_reject_unknown_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        UserInviteDetails.model_validate({"invitee_email": _EMAIL, field: "SECRET"})


def test_user_invite_details_have_no_role_or_group_field() -> None:
    """D-D: the invite carries no role or group, so none is recorded."""
    assert set(UserInviteDetails.model_fields) == {"invitee_email", "reason_code"}


def test_user_invite_failed_details_carry_an_enumerated_reason_code_and_no_free_text() -> None:
    for code in ("upstream_http_error", "upstream_unreachable", "unexpected_error"):
        assert UserInviteDetails(invitee_email=_EMAIL, reason_code=code).reason_code == code  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationError):
        UserInviteDetails.model_validate({"invitee_email": _EMAIL, "reason_code": "HTTP 503 body"})


def test_user_resource_type_is_registered() -> None:
    assert is_known_resource_type("user")
