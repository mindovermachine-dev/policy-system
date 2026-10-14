"""Resource-provider registration + poll for `scripts/deploy-ps-prod.sh`.

Every required provider namespace is registered and polled to `Registered` before any dependent
resource create -- a fresh subscription has only
`Microsoft.Authorization` registered by default (see the script's `REQUIRED_PROVIDERS` comment).

Scope note: this file's "registered and polled before LLM provisioning" test (below) does not
assert ordering against an actual `cognitiveservices`/`aks`/etc. call. It asserts the two things
provable without one: every required namespace ends up `Registered`, and `main()` itself
completes (exit 0). A stronger check would assert in the call log that register precedes the
first LLM-provisioning call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

VALID_CONFIG = """# scripts/ps-defaults.conf — evaluator-tunable Azure customer-tenant deployment
# defaults (issue #111). No secrets. See docs/architecture/customer-azure-deployment.md.

LLM_REGION_CANDIDATES=(swedencentral francecentral westeurope germanywestcentral)
LLM_CHAT_MODEL_NAME="gpt-5.4-mini"
LLM_CHAT_MODEL_SKU="DataZoneStandard"
LLM_CHAT_MODEL_CAPACITY=200
LLM_EMBED_MODEL_NAME="text-embedding-3-large"
LLM_EMBED_MODEL_SKU="Standard"
LLM_EMBED_MODEL_CAPACITY=350
TLS_CONTACT_EMAIL="tls-contact@example.test"
AUTHZ_OWNER_EMAIL="owner@example.test"
"""

# Must match scripts/deploy-ps-prod.sh's own REQUIRED_PROVIDERS exactly (the 9 namespaces the spike
# script's REQUIRED_PROVIDERS lists) -- verified independently here rather than imported, since
# this is a bash constant, not a Python one.
REQUIRED_PROVIDERS = (
    "Microsoft.CognitiveServices",
    "Microsoft.ContainerService",
    "Microsoft.KeyVault",
    "Microsoft.Network",
    "Microsoft.Compute",
    "Microsoft.ManagedIdentity",
    "Microsoft.OperationsManagement",
    "Microsoft.OperationalInsights",
    "Microsoft.Insights",
)

# Short enough that a genuine timeout in the fake harness resolves well within the fixture's own
# subprocess timeout, without the test waiting out deploy-ps-prod.sh's real ~5-minute default.
FAST_POLL_ENV = {
    "PROVIDER_REGISTRATION_WAIT_ATTEMPTS": "2",
    "PROVIDER_REGISTRATION_WAIT_INTERVAL_SECONDS": "0",
}


def _seed(fixture: DeployPsFixture) -> None:
    fixture.config_path.write_text(VALID_CONFIG, encoding="utf-8")
    fixture.seed_subscription()  # bundles Owner role by default


def _register_calls(fixture: DeployPsFixture) -> list[str]:
    return [line for line in fixture.read_az_log() if line.startswith("provider register")]


def test_all_providers_already_registered_makes_no_register_calls(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    for namespace in REQUIRED_PROVIDERS:
        deploy_ps_fixture.seed_provider_registered(namespace)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _register_calls(deploy_ps_fixture) == []


def test_unregistered_providers_are_registered_and_polled_to_registered_before_llm_provisioning(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    # No provider state pre-seeded -- a genuinely fresh subscription, every namespace starts
    # NotRegistered (fake `provider show`'s default when no state file exists).

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    register_calls = _register_calls(deploy_ps_fixture)
    for namespace in REQUIRED_PROVIDERS:
        assert f"provider register --namespace {namespace}" in register_calls
        assert deploy_ps_fixture.read_provider_state(namespace) == "Registered"
    # main() itself only returns 0 once ensure_providers_registered has finished (it is the last
    # step in main() as of this slice) -- see module docstring's interim-scope note.


def test_registration_timeout_fails_explicitly_naming_the_stuck_provider(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    stuck_namespace = "Microsoft.ContainerService"
    deploy_ps_fixture.seed_stuck_provider(stuck_namespace)

    run = deploy_ps_fixture.run_deploy("--yes", expect=1, extra_env=FAST_POLL_ENV)

    assert stuck_namespace in run.stderr
    assert "Timed out" in run.stderr
    # Every other required provider still finished registering -- only the stuck one is named.
    for namespace in REQUIRED_PROVIDERS:
        if namespace != stuck_namespace:
            assert namespace not in run.stderr
