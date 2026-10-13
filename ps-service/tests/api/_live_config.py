"""Config for the live API tests that build the real app (`create_app`) against real stores."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from persistence.provisioned_postgres import Provisioned

    from ps_service.config import ServiceConfig


def live_app_config(prov: Provisioned, **overrides: object) -> ServiceConfig:
    """The environment's config on the scratch cluster, as `create_app` needs it with no IdP.

    The state Postgres is `prov`'s `ps_state` role (the graph log and audit trail live there).
    The local-test bypass stands in for the IdP, and placeholder Authentik credentials satisfy
    the startup check (no Authentik call is made: the invitation routes are never exercised).
    Tests inject their own principal through `get_principal`.
    """
    return dataclasses.replace(
        prov.state_config(),
        is_local_test_bypass_active=True,
        authentik_api_token="live-test-authentik-token",  # placeholder, never sent
        authentik_base_url="https://authentik.invalid",
        **overrides,  # pyright: ignore[reportArgumentType] -- caller supplies ServiceConfig fields
    )
