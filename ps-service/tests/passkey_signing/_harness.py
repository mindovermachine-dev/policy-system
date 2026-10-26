# pyright: reportPrivateUsage=false
"""Public re-exports of the real-WebAuthn signing harness for cross-package tests.

`test_signing_ceremony.py` owns the hand-built, genuinely verifiable WebAuthn assertion
construction; tests in other packages (graph-cleanup executor dispatch) reuse it through this
module instead of reaching into private names themselves.
"""

from __future__ import annotations

from passkey_signing import test_signing_ceremony as _ceremony

ACTOR_ISSUER = _ceremony._ACTOR_ISSUER
ACTOR_SUBJECT = _ceremony._ACTOR_SUBJECT
ORIGIN = _ceremony._ORIGIN
RP_ID = _ceremony._RP_ID
INVALID_ERROR_CODE = _ceremony._INVALID_ERROR_CODE
Authenticator = _ceremony._Authenticator
MergeCall = _ceremony._MergeCall
client = _ceremony._client
enroll = _ceremony._enroll
seed_pending_approval = _ceremony._seed_pending_approval
sign_challenge = _ceremony._independent_sign_challenge

__all__ = [
    "ACTOR_ISSUER",
    "ACTOR_SUBJECT",
    "INVALID_ERROR_CODE",
    "ORIGIN",
    "RP_ID",
    "Authenticator",
    "MergeCall",
    "client",
    "enroll",
    "seed_pending_approval",
    "sign_challenge",
]
