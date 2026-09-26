"""Shared fixtures for `scripts/deploy-ps.sh` (GH issue #111).

Sibling to `ps-service/tests/deploy_llm/conftest.py` (GH issue #105) and extends the exact same
pattern for the same reason that fixture's own docstring documents: the root `pyproject.toml`'s
`testpaths` covers `ps-service/tests`/`ps-cli/tests` only, and this is the only test-collected
location for a `scripts/`-level script.

`scripts/deploy-ps.sh` takes no `--config` flag, same as `deploy-llm.sh` -- a test wanting a
malformed `scripts/ps-defaults.conf` cannot point the script at an alternate path. Instead, this
fixture copies the real `scripts/deploy-ps.sh` (plus `scripts/ps-defaults.conf` and the shared
`scripts/lib/deploy-llm-common.sh`) into an isolated `tmp_path` copy of the `scripts/` tree and
runs *that* copy -- the script's own `$SCRIPT_DIR`-relative config lookup then resolves inside
the fixture, so a test edits `DeployPsFixture.config_path` in place without ever touching the
real, checked-in defaults file or requiring a test-only flag on the script itself.

S5 (PLAN.md §5) needed only a minimal fake `az`: `account show` (subscription id) and
`account show --query user.name` (signed-in UPN). S6 adds `role assignment list` (RBAC
preflight); S7 adds `provider register`/`provider show` (resource-provider registration+poll).
S8 adds `cognitiveservices model list` (region availability + capacity range + model version),
`cognitiveservices usage list` (quota), and `cognitiveservices account deployment show`
(check_quota's "already deployed, skip" branch) -- real command shapes read from
`spikes/deploy-ps-azure/deploy-ps.sh`'s own `select_region`/`check_quota`/`deployment_exists`,
not guessed (PLAN.md §2.2 lists an approximate/older shape; the spike script itself is the
trusted empirical record per PLAN.md's own framing).
This grows across S9-S18 as later slices need more of PLAN.md §2's full API (azure-state
seeding helpers, fake `kubectl`/`helm`, and so on) -- extend `FAKE_AZ_SCRIPT`'s `case` block and
`DeployPsFixture`'s `seed_*`/`read_*` methods in place, mirroring `deploy_llm/conftest.py`'s own
S2-S13 growth.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

SUBPROCESS_TIMEOUT_SECONDS = 30.0

# Mirrors scripts/ps-defaults.conf's shipped defaults (PLAN.md §1) -- hardcoded here rather than
# parsed from the config file, same precedent as ps-service/tests/deploy_llm/conftest.py's own
# DEFAULT_* constants and test_confirmation_table.py's CONFIG_WITH_TLS_EMAIL literal.
DEFAULT_REGION_CANDIDATES = ("swedencentral", "francecentral", "westeurope", "germanywestcentral")
DEFAULT_CHAT_MODEL_NAME = "gpt-5.4-mini"
DEFAULT_CHAT_MODEL_SKU = "DataZoneStandard"
DEFAULT_CHAT_MODEL_VERSION = "2024-07-18"
DEFAULT_EMBED_MODEL_NAME = "text-embedding-3-large"
DEFAULT_EMBED_MODEL_SKU = "Standard"
DEFAULT_EMBED_MODEL_VERSION = "1"
# Ample enough that the default seeded quota never binds against the default capacities
# (200/350) -- tests that want quota to bind call seed_usage()/seed_empty_usage() themselves.
AMPLE_QUOTA_LIMIT = 10_000

# Mirrors scripts/deploy-ps.sh's own fixed AKS-node-shape literals (S12, PLAN.md §0.6) --
# hardcoded here rather than parsed from the script, same precedent as the constants above.
AKS_NODE_VM_SIZE = "Standard_D4as_v7"
AKS_NODE_VM_SIZE_FAMILY = "StandardDasv7Family"
AKS_NODE_COUNT = 2
AKS_NODE_VM_SIZE_VCPUS = 4

# Mirrors scripts/deploy-ps.sh's own AKS_RBAC_ADMIN_ROLE literal (S13). Mirrors
# scripts/lib/deploy-llm-common.sh's own RESOURCE_GROUP_NAME constant (S1's rename). Mirrors
# DeployPsFixture.seed_subscription's own default id_/user_id parameters -- named here so S13's
# new AKS-cluster/role-assignment seeding helpers can compute the exact same scope strings the
# fake `az`/the real script would, without hardcoding the literal a second time in each helper.
AKS_RBAC_ADMIN_ROLE = "Azure Kubernetes Service RBAC Cluster Admin"
RESOURCE_GROUP_NAME = "rg-policy-system"
DEFAULT_SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
DEFAULT_USER_OBJECT_ID = "22222222-3333-4444-5555-666666666666"
# S15's fetch_tenant_id reads this back via `az account show --query tenantId -o tsv` --
# deliberately a distinct-looking GUID from DEFAULT_SUBSCRIPTION_ID/DEFAULT_USER_OBJECT_ID so a
# test can't accidentally pass by confusing one identity value for another.
DEFAULT_TENANT_ID = "33333333-4444-5555-6666-777777777777"

# Mirrors scripts/deploy-ps.sh's own LLM_SECRET_NAME/CHART_REF/HELM_RELEASE_NAME literals
# (S14/S15) -- hardcoded here rather than parsed from the script, same precedent as the
# constants above.
LLM_SECRET_NAME = "policy-system-llm-credentials"
CHART_REF = "oci://ghcr.io/mindovermachine-dev/charts/policy-system"
HELM_RELEASE_NAME = "policy-system"

# Mirrors scripts/deploy-ps.sh's own S16/S17 literals (CERT_MANAGER_NAMESPACE and the three
# cert-manager deployment names, INGRESS_CLASS) -- hardcoded here rather than parsed from the
# script, same precedent as every other constant above. DEFAULT_INGRESS_IP is a fake address with
# no real-world meaning, used only so `seed_subscription`'s bundled baseline (below) gives every
# full-success run -- including S5-S15's own tests, which never call any S16-specific seed
# method -- a public IP on the very first `fetch_ingress_public_ip` poll attempt, never a real
# sleep (same bundling rationale as that method's own model-availability/vm-skus baseline).
DEFAULT_INGRESS_IP = "20.99.0.1"
CERT_MANAGER_NAMESPACE = "cert-manager"
CERT_MANAGER_RELEASE_NAME = "cert-manager"
CERT_MANAGER_DEPLOYMENTS = ("cert-manager", "cert-manager-webhook", "cert-manager-cainjector")
INGRESS_CLASS = "webapprouting.kubernetes.azure.com"
APP_ROUTING_NAMESPACE = "app-routing-system"
APP_ROUTING_SERVICE_NAME = "nginx"


def _scope_key(scope: str) -> str:
    """Sanitize a `role assignment` `--scope` value into the same filename-safe key the fake
    `az`'s own `tr -c '[:alnum:]' '_'` produces (PLAN.md §2.2's "keyed by a hash of <s>" -- a
    deterministic, scope-distinct key, not a cryptographic hash). Kept as one function so
    Python-side seeding (`DeployPsFixture._write_role_assignments`) and the fake `az`'s own
    dispatch stay in lockstep without duplicating the sanitization rule in two languages
    independently.
    """
    return re.sub(r"[^0-9A-Za-z]", "_", scope)


def _aks_cluster_resource_id(
    cluster_name: str, *, subscription_id: str, resource_group: str
) -> str:
    """The fake `az aks create`/`az aks show`'s own deterministic resource-ID shape for
    <cluster_name> -- a real Azure AKS resource ID's actual structure, reproduced here so a test
    seeding an existing cluster (`seed_aks_cluster`) computes the identical `--scope` a real
    `grant_aks_rbac_access` run would use, letting `seed_aks_rbac_granted` target that same scope.
    """
    return (
        f"/subscriptions/{subscription_id}/resourcegroups/{resource_group}"
        f"/providers/Microsoft.ContainerService/managedClusters/{cluster_name}"
    )


def _subscription_hash8(subscription_id: str) -> str:
    """Independently reproduce `subscription_hash8`'s first 8 hex chars (scripts/lib/deploy-llm-
    common.sh) -- used only internally, to compute S16's default node-resource-group baseline
    below without hardcoding a subscription-specific literal.
    """
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def _cluster_name_for(subscription_id: str) -> str:
    """Independently reproduce `aks_cluster_name`'s deterministic naming (scripts/lib/deploy-llm-
    common.sh) for <subscription_id>.
    """
    return f"aks-policy-system-{_subscription_hash8(subscription_id)}"


def _default_node_resource_group(cluster_name: str, region: str) -> str:
    """Independently reproduce the fake `az aks create`'s own deterministic
    `nodeResourceGroup` shape ("MC_<rg>_<cluster>_<region>", a real AKS-managed node resource
    group's actual naming convention) -- lets `seed_subscription`'s bundled baseline (S16) and
    `seed_aks_cluster`'s own default pre-populate a public-IP record at the exact resource group
    a real `ensure_public_exposure`-equivalent run would look under, for whichever cluster/region
    combination a test ends up exercising.
    """
    return f"MC_{RESOURCE_GROUP_NAME}_{cluster_name}_{region}"


def _usage_key(sku: str, model_name: str) -> str:
    """Independently reproduce `quota_usage_key`'s real per-model+SKU Azure usage-entry name
    (e.g. "OpenAI.DataZoneStandard.gpt-5.4-mini") -- confirmed against a real subscription
    (spikes/deploy-ps-azure/README.md "Bugs found and fixed": "Quota preflight always a no-op")
    that this is the actual shape `az cognitiveservices usage list` reports, not the literal
    "chat"/"embed" keys `scripts/deploy-llm.sh` (issue #105) still uses uncorrected.
    """
    return f"OpenAI.{sku}.{model_name}"


def _model_availability_entry(
    name: str,
    sku: str,
    *,
    is_generally_available: bool,
    capacity_minimum: int | None,
    capacity_maximum: int,
    version: str,
) -> dict[str, object]:
    """One `az cognitiveservices model list` response entry for a single model.

    `capacity_minimum=None` serializes to JSON `null` -- confirmed against a real subscription
    that Azure reports exactly this for SKUs with no enforced floor (GlobalStandard/
    DataZoneStandard), the shape `model_capacity_range`'s `// 0` coalesce exists to handle
    (spikes/deploy-ps-azure/README.md "Bugs found and fixed": "capacity.minimum: null").
    """
    return {
        "model": {
            "name": name,
            "version": version,
            "lifecycleStatus": "GenerallyAvailable" if is_generally_available else "Preview",
            "skus": [
                {
                    "name": sku,
                    "capacity": {"minimum": capacity_minimum, "maximum": capacity_maximum},
                }
            ],
        }
    }


# Copied into each test's tmp_path so the script under test always finds its config/lib next to
# itself, exactly as it would in the real repo.
DEPLOY_PS_RELATIVE_FILES = (
    Path("scripts/deploy-ps.sh"),
    Path("scripts/ps-defaults.conf"),
    Path("scripts/lib/deploy-llm-common.sh"),
)

# Fake `az` (S5 -- PLAN.md §2.2 grows this across S6-S18). Dispatches on "$1 $2", same shape as
# ps-service/tests/deploy_llm/conftest.py's FAKE_AZ_SCRIPT. Every invocation is logged to
# $PS_TEST_AZ_LOG first, unconditionally, before any dispatch runs.
FAKE_AZ_SCRIPT = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$PS_TEST_AZ_LOG"

state="$PS_TEST_AZ_STATE_DIR"

# get_arg <flag> "$@" -- prints the single token following <flag>, or "". Copied from
# ps-service/tests/deploy_llm/conftest.py's own helper (same shape, same limitation: only for
# single-value flags).
get_arg() {
  local flag="$1"; shift
  local i
  for ((i = 1; i <= $#; i++)); do
    if [[ "${!i}" == "$flag" ]]; then
      local j=$((i + 1))
      printf '%s' "${!j}"
      return 0
    fi
  done
}

case "${1:-} ${2:-}" in
  "account show")
    # account show --query {id,user.name,tenantId} -o tsv -- three distinct queries against the
    # same signed-in session (fetch_subscription_id/fetch_signed_in_user_upn/fetch_tenant_id).
    # tenantId is checked first: "user.name" and "tenantId" never co-occur in the same call, so
    # order between those two branches doesn't matter, but checking the plain "id" case last
    # (the unconditional else) keeps this future-proof against a query string that happens to
    # contain "id" as a substring of something else.
    if [[ "$*" == *"tenantId"* ]]; then cat "$state/tenant-id"
    elif [[ "$*" == *"user.name"* ]]; then cat "$state/signed-in-user-upn"
    else cat "$state/subscription-id"
    fi
    ;;
  "ad signed-in-user")
    # ad signed-in-user show --query id -o tsv (grant_keyvault_access, S9) -- same shape as
    # ps-service/tests/deploy_llm/conftest.py's own fake.
    cat "$state/signed-in-user-id"
    ;;
  "role assignment")
    # role assignment {list,create} --assignee <a> [--role <r>] --scope <s>
    # [--query [].roleDefinitionName -o tsv] -- keyed by a sanitized <s> (PLAN.md §2.2): S6's
    # subscription-scoped RBAC preflight and S13's cluster-scoped AKS RBAC grant are two
    # DISTINCT scopes, so a single flat state file (the pre-S13 shape) would conflate them.
    subverb="${3:-}"
    scope="$(get_arg --scope "$@")"
    scope_key="$(printf '%s' "$scope" | tr -c '[:alnum:]' '_')"
    mkdir -p "$state/role-assignments-by-scope"
    case "$subverb" in
      list)
        if [[ -f "$state/role-assignments-by-scope/$scope_key" ]]; then
          cat "$state/role-assignments-by-scope/$scope_key"
        fi
        ;;
      create)
        role="$(get_arg --role "$@")"
        printf '%s\n' "$role" >> "$state/role-assignments-by-scope/$scope_key"
        ;;
    esac
    ;;
  "aks show")
    # aks show --name <name> --resource-group <rg> [--query id -o tsv]
    # [--query nodeResourceGroup -o tsv] [--query ingressProfile.webAppRouting.enabled -o tsv]
    # (aks_cluster_exists/fetch_aks_cluster_id, S13; the nodeResourceGroup/ingressProfile queries,
    # S16) -- the [[ -f ]] existence check exits (under set -e) before the print branch when the
    # cluster doesn't exist yet, same idiom as "cognitiveservices account show"/"keyvault secret
    # show" above. ingressProfile.webAppRouting.enabled is NOT stored in aks/<name>.json itself --
    # it's a separate aks/<name>-approuting marker file (PLAN.md §2.1's own documented target
    # shape), toggled by "aks approuting enable" below.
    name="$(get_arg --name "$@")"
    [[ -f "$state/aks/$name.json" ]]
    if [[ "$*" == *"--query id"* ]]; then
      jq -r '.id' "$state/aks/$name.json"
    elif [[ "$*" == *"nodeResourceGroup"* ]]; then
      jq -r '.nodeResourceGroup' "$state/aks/$name.json"
    elif [[ "$*" == *"ingressProfile"* ]]; then
      if [[ -f "$state/aks/${name}-approuting" ]]; then printf 'true'; else printf 'false'; fi
    else
      cat "$state/aks/$name.json"
    fi
    ;;
  "aks create")
    # aks create --name <name> --resource-group <rg> --location <region> --node-count <n>
    # --node-vm-size <size> --tier free --enable-aad --enable-azure-rbac
    # --disable-local-accounts --network-plugin azure --network-policy azure
    # --node-os-upgrade-channel SecurityPatch --generate-ssh-keys (ensure_aks_cluster, S13). The
    # resource ID shape matches a real Azure AKS cluster's own ID structure so
    # grant_aks_rbac_access's --scope is a realistic, cluster-specific value, never the
    # subscription root. nodeResourceGroup (S16) reproduces a real AKS-managed node resource
    # group's actual "MC_<rg>_<cluster>_<region>" naming convention -- see this module's own
    # _default_node_resource_group docstring.
    name="$(get_arg --name "$@")"; rg="$(get_arg --resource-group "$@")"
    location="$(get_arg --location "$@")"
    sub="$(cat "$state/subscription-id" 2>/dev/null || printf 'unknown-subscription')"
    id="/subscriptions/$sub/resourcegroups/$rg/providers/Microsoft.ContainerService/managedClusters/$name"
    node_rg="MC_${rg}_${name}_${location}"
    mkdir -p "$state/aks"
    jq -n --arg id "$id" --arg name "$name" --arg node_rg "$node_rg" \
      '{id: $id, name: $name, nodeResourceGroup: $node_rg}' > "$state/aks/$name.json"
    ;;
  "aks approuting")
    # aks approuting enable --name <name> --resource-group <rg> (ensure_approuting, S16) -- a
    # separate aks/<name>-approuting marker file (PLAN.md §2.1's own documented target shape),
    # read back by the "aks show --query ingressProfile..." branch above.
    subverb="${3:-}"
    case "$subverb" in
      enable)
        name="$(get_arg --name "$@")"
        mkdir -p "$state/aks"
        touch "$state/aks/${name}-approuting"
        ;;
    esac
    ;;
  "aks get-credentials")
    # aks get-credentials --name <name> --resource-group <rg> --overwrite-existing
    # (ensure_aks_credentials, S13) -- no-op success; kubectl isn't exercised until S14+, this
    # slice only proves the call itself happens.
    :
    ;;
  "group show")
    # group show --name <rg> (resource_group_exists, S9)
    name="$(get_arg --name "$@")"
    [[ -f "$state/resource-groups/$name" ]]
    ;;
  "group create")
    # group create --name <rg> --location <region> (ensure_resource_group, S9)
    name="$(get_arg --name "$@")"; location="$(get_arg --location "$@")"
    mkdir -p "$state/resource-groups"
    printf '%s' "$location" > "$state/resource-groups/$name"
    ;;
  "provider register")
    # provider register --namespace <ns> -- marks <ns> Registered immediately (S7), UNLESS a
    # providers/<ns>.never-registers marker is present (seed_stuck_provider), in which case this
    # is a no-op and the namespace stays whatever it already was -- simulates a registration that
    # never completes, for the timeout test.
    ns="$(get_arg --namespace "$@")"
    mkdir -p "$state/providers"
    if [[ ! -f "$state/providers/${ns}.never-registers" ]]; then
      printf 'Registered' > "$state/providers/$ns"
    fi
    ;;
  "provider show")
    # provider show --namespace <ns> --query registrationState -o tsv
    ns="$(get_arg --namespace "$@")"
    if [[ -f "$state/providers/$ns" ]]; then cat "$state/providers/$ns"
    else printf 'NotRegistered'
    fi
    ;;
  "cognitiveservices model")
    # cognitiveservices model list --location <region> -- region-availability/capacity-range/
    # model-version probe (select_region/model_capacity_range/model_version, S8). Real command
    # shape read from spikes/deploy-ps-azure/deploy-ps.sh's own select_region, not guessed.
    region="$(get_arg --location "$@")"
    cat "$state/model-availability/$region.json" 2>/dev/null || printf '[]'
    ;;
  "cognitiveservices usage")
    # cognitiveservices usage list --location <region> -- quota preflight (check_quota, S8).
    region="$(get_arg --location "$@")"
    cat "$state/usage/$region.json" 2>/dev/null || printf '[]'
    ;;
  "cognitiveservices account")
    # cognitiveservices account {show,create,keys,deployment} -- account_exists/ensure_account
    # (S9), deployment_exists (S8, feeds check_quota's per-model "already deployed, skip"
    # branch) and ensure_deployment (S9). Real command shapes read from
    # ps-service/tests/deploy_llm/conftest.py's own equivalent fake (same Azure resource types,
    # just RG-renamed) and spikes/deploy-ps-azure/deploy-ps.sh's own ensure_account/
    # ensure_deployment.
    verb="${3:-}"; name="$(get_arg --name "$@")"
    case "$verb" in
      show)
        [[ -f "$state/accounts/$name.json" ]]
        cat "$state/accounts/$name.json"
        ;;
      create)
        mkdir -p "$state/accounts"
        printf '{"properties":{"endpoint":"https://%s.cognitiveservices.azure.com/"}}' "$name" \
          > "$state/accounts/$name.json"
        [[ -f "$state/accounts/$name-keys.json" ]] || printf '{"key1":"%s","key2":"%s"}' \
          "${PS_TEST_AZ_INITIAL_KEY1:-FAKE-KEY-1-INITIAL}" \
          "${PS_TEST_AZ_INITIAL_KEY2:-FAKE-KEY-2-INITIAL}" > "$state/accounts/$name-keys.json"
        cat "$state/accounts/$name.json"
        ;;
      keys)
        case "${4:-}" in
          list) cat "$state/accounts/$name-keys.json" ;;
          regenerate)
            # cognitiveservices account keys regenerate --name <name> --resource-group <rg>
            # --key-name <slot> (rotate_key_main, S18) -- real command shape read from
            # ps-service/tests/deploy_llm/conftest.py's own equivalent fake (same Azure resource
            # type). Never prints the new value itself; only writes it into state for a later
            # `keys list`/this same call's own stdout to read back.
            key_name="$(get_arg --key-name "$@")"
            new_value="FAKE-$(printf '%s' "$key_name" | tr '[:lower:]' '[:upper:]')-REGEN-$RANDOM"
            jq --arg k "$key_name" --arg v "$new_value" '.[$k] = $v' \
              "$state/accounts/$name-keys.json" > "$state/accounts/$name-keys.json.tmp"
            mv "$state/accounts/$name-keys.json.tmp" "$state/accounts/$name-keys.json"
            cat "$state/accounts/$name-keys.json"
            ;;
        esac
        ;;
      deployment)
        dep_name="$(get_arg --deployment-name "$@")"
        marker="$state/deployments/$name/$dep_name"
        case "${4:-}" in
          show) [[ -f "$marker" ]] ;;
          create) mkdir -p "$(dirname "$marker")"; touch "$marker" ;;
        esac
        ;;
    esac
    ;;
  "vm list-skus")
    # vm list-skus --location <region> --size <size> --all -o json (fetch_vm_sku_json /
    # vm_size_allowed, S12) -- reads pre-seeded vm-skus/<region>.json. PLAN.md §2.1: this state
    # is TEST-owned (seed_vm_skus), not written by any other fake `az` call -- it represents real
    # Azure subscription state, not something this script creates.
    region="$(get_arg --location "$@")"
    cat "$state/vm-skus/$region.json" 2>/dev/null || printf '[]'
    ;;
  "vm list-usage")
    # vm list-usage --location <region> -o json (fetch_vm_usage_json /
    # vm_family_quota_sufficient, S12) -- reads pre-seeded vm-usage/<region>.json
    # (seed_vm_usage, test-owned, same rationale as vm-skus above).
    region="$(get_arg --location "$@")"
    cat "$state/vm-usage/$region.json" 2>/dev/null || printf '[]'
    ;;
  "network public-ip")
    # network public-ip {list,show,update} (fetch_public_ip_resource_id/ensure_dns_label/
    # fetch_public_ip_fqdn, S16) -- real command shapes read from spikes/deploy-ps-azure/
    # deploy-ps.sh's own equivalents, not guessed. Records live at
    # public-ips/<node-rg>/<ip>.json (PLAN.md §2.1) -- TEST-owned state for "list" (seed_public_ip
    # stands in for real LB IP allocation, which is external to this script), but "show"/"update"
    # only ever receive a bare resource ID (--ids), never the rg/ip pair -- so a second,
    # id-keyed index (public-ip-ids/<sanitized-id>) resolves an id back to its rg/ip record.
    subverb="${3:-}"
    case "$subverb" in
      list)
        # network public-ip list --resource-group <rg> --query "[?ipAddress=='<ip>'].id | [0]"
        # -o tsv -- <ip> is embedded inside the --query string itself, not a separate flag.
        rg="$(get_arg --resource-group "$@")"
        query="$(get_arg --query "$@")"
        ip="$(printf '%s' "$query" | sed -n "s/.*ipAddress=='\([^']*\)'.*/\1/p")"
        if [[ -n "$ip" && -f "$state/public-ips/$rg/$ip.json" ]]; then
          jq -r '.id' "$state/public-ips/$rg/$ip.json"
        fi
        ;;
      show)
        # network public-ip show --ids <id> --query dnsSettings.domainNameLabel -o tsv /
        # --query dnsSettings.fqdn -o tsv
        ids="$(get_arg --ids "$@")"
        id_key="$(printf '%s' "$ids" | tr -c '[:alnum:]' '_')"
        rel="$(cat "$state/public-ip-ids/$id_key" 2>/dev/null || true)"
        [[ -n "$rel" ]]
        record="$state/public-ips/$rel.json"
        if [[ "$*" == *"domainNameLabel"* ]]; then
          jq -r '.dnsSettings.domainNameLabel // empty' "$record"
        elif [[ "$*" == *"fqdn"* ]]; then
          jq -r '.dnsSettings.fqdn // empty' "$record"
        else
          cat "$record"
        fi
        ;;
      update)
        # network public-ip update --ids <id> --dns-name <label> (ensure_dns_label, S16)
        ids="$(get_arg --ids "$@")"; label="$(get_arg --dns-name "$@")"
        id_key="$(printf '%s' "$ids" | tr -c '[:alnum:]' '_')"
        rel="$(cat "$state/public-ip-ids/$id_key" 2>/dev/null || true)"
        [[ -n "$rel" ]]
        record="$state/public-ips/$rel.json"
        region="$(jq -r '.location' "$record")"
        fqdn="${label}.${region}.cloudapp.azure.com"
        jq --arg label "$label" --arg fqdn "$fqdn" \
          '.dnsSettings.domainNameLabel = $label | .dnsSettings.fqdn = $fqdn' \
          "$record" > "$record.tmp"
        mv "$record.tmp" "$record"
        ;;
    esac
    ;;
  "keyvault show")
    # keyvault show --name <vault> (keyvault_exists, S9)
    name="$(get_arg --name "$@")"
    [[ -f "$state/keyvaults/$name.json" ]]
    ;;
  "keyvault create")
    # keyvault create --name <vault> ... (ensure_keyvault, S9)
    name="$(get_arg --name "$@")"
    mkdir -p "$state/keyvaults"; touch "$state/keyvaults/$name.json"
    ;;
  "keyvault set-policy")
    # keyvault set-policy --name <vault> --object-id <id> --secret-permissions get list set
    # (grant_keyvault_access, S9)
    name="$(get_arg --name "$@")"; object_id="$(get_arg --object-id "$@")"
    mkdir -p "$state/keyvaults"
    printf '%s %s\n' "$object_id" "$*" >> "$state/keyvaults/$name-policies.log"
    ;;
  "keyvault secret")
    # keyvault secret {show,set} --vault-name <vault> --name <secret> [--value <v>]
    # (read_secret_value/write_secret_if_changed, S9)
    verb="${3:-}"; vault="$(get_arg --vault-name "$@")"
    secret_name="$(get_arg --name "$@")"
    secret_dir="$state/keyvaults/$vault-secrets"
    case "$verb" in
      show)
        [[ -f "$secret_dir/$secret_name" ]]
        jq -n --arg v "$(cat "$secret_dir/$secret_name")" '{value: $v}'
        ;;
      set)
        value="$(get_arg --value "$@")"
        mkdir -p "$secret_dir"
        printf '%s' "$value" > "$secret_dir/$secret_name"
        ;;
    esac
    ;;
  *)
    echo "fake az: unsupported invocation '$*'" >&2
    exit 2
    ;;
esac
"""

# Fake `kubectl` (S14, PLAN.md §2.4 extended) -- `create secret --dry-run=client` is a pure,
# stateless transform (no state read/write), so it is deliberately NOT logged to
# $PS_TEST_KUBECTL_LOG: logging it would race against the concurrently-running "apply -f -" stage
# of the same pipe (both processes start at once; cross-process log order is not deterministic --
# same precedent as ps-service/tests/deploy_llm/conftest.py's own fake kubectl). Unlike that
# sibling fixture's fake (which only captures the applied manifest), "apply -f -" here ALSO
# reproduces real kubectl's own "<kind>/<name> {created,configured,unchanged}" stdout line -- the
# exact machine-readable signal `apply_output_changed` (scripts/deploy-ps.sh, S14) greps for --
# and records it to $PS_TEST_KUBECTL_APPLY_OUTPUT_LOG (test-only instrumentation; the real
# kubectl's own stdout is what the script itself reads, this is just how the test observes it
# from outside the `$(...)` capture).
FAKE_KUBECTL_SCRIPT = r"""#!/usr/bin/env bash
set -euo pipefail

applied="$PS_TEST_KUBECTL_APPLIED_DIR"
state="$PS_TEST_KUBECTL_STATE_DIR"

# get_arg <flag> "$@" -- same helper as FAKE_AZ_SCRIPT's own (prints the single token following
# <flag>, or "").
get_arg() {
  local flag="$1"; shift
  local i
  for ((i = 1; i <= $#; i++)); do
    if [[ "${!i}" == "$flag" ]]; then
      local j=$((i + 1))
      printf '%s' "${!j}"
      return 0
    fi
  done
}

case "${1:-} ${2:-}" in
  "create secret")
    # create secret generic <name> --from-literal=K=V ... --dry-run=client -o yaml -- pure,
    # stateless transform (mutates nothing), so deliberately NOT logged (see module comment
    # above).
    name="$4"
    printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\ntype: Opaque\nstringData:\n' \
      "$name"
    for arg in "$@"; do
      if [[ "$arg" == --from-literal=* ]]; then
        pair="${arg#--from-literal=}"
        printf '  %s: "%s"\n' "${pair%%=*}" "${pair#*=}"
      fi
    done
    ;;
  *)
    printf '%s\n' "$*" >> "$PS_TEST_KUBECTL_LOG"
    case "${1:-} ${2:-}" in
      "apply -f")
        manifest="$(cat)"
        kind="$(printf '%s\n' "$manifest" | sed -n 's/^kind: //p' | head -1)"
        name="$(printf '%s\n' "$manifest" | sed -n 's/^  name: //p' | head -1)"
        mkdir -p "$applied"
        dest="$applied/${kind}-${name}.yaml"
        if [[ ! -f "$dest" ]]; then
          verb="created"
        elif diff -q <(printf '%s\n' "$manifest") "$dest" >/dev/null 2>&1; then
          verb="unchanged"
        else
          verb="configured"
        fi
        printf '%s\n' "$manifest" > "$dest"
        output_line="$(printf '%s' "$kind" | tr '[:upper:]' '[:lower:]')/$name $verb"
        printf '%s\n' "$output_line" >> "$PS_TEST_KUBECTL_APPLY_OUTPUT_LOG"
        # A second, kind/name-identified line into the SAME argv log the outer branch already
        # wrote a bare "apply -f -" line to -- every "apply -f -" call is textually identical
        # otherwise, which would make an ORDERING assertion against read_kubectl_log() (S17's
        # own AC-BI-016 regression test, comparing this against a "wait" call's position) unable
        # to tell one applied resource apart from another.
        printf 'apply -f - kind=%s name=%s\n' "$kind" "$name" >> "$PS_TEST_KUBECTL_LOG"
        printf '%s\n' "$output_line"
        ;;
      "get service")
        # get service <name> --namespace <ns> --output jsonpath='...' (fetch_ingress_public_ip,
        # S16) -- <name> is the plain 3rd positional token (kubectl's own CLI shape), not a
        # flag. LB IP allocation is external to this script (PLAN.md §2.1) -- the [[ -f ]]
        # existence check exits (under set -e) before the print branch when no IP has been
        # seeded yet, mirroring a real "Service not found"/empty-jsonpath failure that
        # fetch_ingress_public_ip's own `2>/dev/null || true` wrapping tolerates.
        name="$3"
        ns="$(get_arg --namespace "$@")"
        path="$state/services/$ns/$name.json"
        [[ -f "$path" ]]
        jq -r '.status.loadBalancer.ingress[0].ip // empty' "$path"
        ;;
      "wait --for=condition=Available")
        # wait --for=condition=Available --timeout=<t> --namespace <ns> deployment/<a>
        # deployment/<b> deployment/<c> (ensure_cert_manager, S17) -- checks every
        # deployment/<name> argument generically (PLAN.md §2.4: "checks all three ... 'ready'
        # markers"), not hardcoded to cert-manager's own three deployment names, so this fake
        # stays reusable if a later slice waits on a different deployment set.
        ns="$(get_arg --namespace "$@")"
        all_ready=true
        for arg in "$@"; do
          if [[ "$arg" == deployment/* ]]; then
            dep="${arg#deployment/}"
            [[ -f "$state/deployments/$ns/$dep-ready" ]] || all_ready=false
          fi
        done
        [[ "$all_ready" == true ]]
        ;;
      "exec deployment/"*)
        # exec deployment/<name> -- psql -U <user> -d <db> -c "ALTER USER ... PASSWORD '...';"
        # (rotate_authentik_secrets_main, S9/#129) -- the live in-database password change this
        # rotation issues against the already-running Postgres pod (see that function's own
        # comment for why a plain Deployment restart cannot rotate a postgres role's password by
        # itself). Nothing for this fake to persist -- the outer branch above already logged the
        # full invocation (including the ALTER USER text) to $PS_TEST_KUBECTL_LOG, which is all
        # the tests need to observe -- this just reproduces psql's own success output.
        printf 'ALTER ROLE\n'
        ;;
      "rollout restart")
        # rollout restart deployment/<name> (rotate_authentik_secrets_main, S9/#129) -- a
        # fire-and-forget rollout trigger with no state this fake models; reproduces kubectl's
        # own "deployment.apps/<name> restarted" stdout line.
        name="${3#deployment/}"
        printf 'deployment.apps/%s restarted\n' "$name"
        ;;
      *)
        echo "fake kubectl: unsupported invocation '$*'" >&2
        exit 2
        ;;
    esac
    ;;
esac
"""

# Fake `helm` (S15, PLAN.md §2.3). `upgrade --install <release> <chart-ref> [-f <file>]
# [--set k=v ...]` parses every `--set` into the same nested JSON shape
# `release_values_json`/`helm get values -o json` use, written to
# `helm-state/releases/<release>.json`. `-f`/`--values` is consumed but never read -- the fake
# has no chart to render, and scripts/deploy-ps.sh's own no-op comparison only ever inspects the
# `--set`-sourced fields, never the `-f` file's contents (that's the exact bug this slice's own
# no-op comparison guards against -- see `ensure_release`'s comment).
FAKE_HELM_SCRIPT = r"""#!/usr/bin/env bash
set -euo pipefail

state="$PS_TEST_HELM_STATE_DIR"
mkdir -p "$state/releases"
printf '%s\n' "$*" >> "$PS_TEST_HELM_LOG"

verb="${1:-}"

case "$verb" in
  upgrade)
    shift
    [[ "${1:-}" == "--install" ]] && shift
    release="$1"; shift
    # The next positional token (unless it's a flag) is the chart ref -- consumed, never used by
    # this fake (no chart to render).
    if [[ $# -gt 0 && "$1" != -* ]]; then
      shift
    fi
    acc="{}"
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --set)
          kv="$2"
          key="${kv%%=*}"
          val="${kv#*=}"
          acc="$(jq -n --argjson acc "$acc" --arg path "$key" --arg val "$val" \
            '$acc | setpath($path | split("."); $val)')"
          shift 2
          ;;
        -f|--values|--namespace)
          shift 2
          ;;
        --create-namespace)
          shift
          ;;
        *)
          shift
          ;;
      esac
    done
    printf '%s' "$acc" > "$state/releases/${release}.json"
    ;;
  get)
    # get values <release> -o json
    release="$3"
    cat "$state/releases/${release}.json"
    ;;
  status)
    # status <release> [--namespace <ns>] -- exit 0 if the release file exists, else 1.
    release="$2"
    [[ -f "$state/releases/${release}.json" ]]
    ;;
  *)
    echo "fake helm: unsupported invocation '$*'" >&2
    exit 2
    ;;
esac
"""


@dataclass(frozen=True)
class ScriptRun:
    """Captured outcome of one script invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        """Stdout followed by stderr -- for assertions that do not care which stream."""
        return self.stdout + self.stderr


@dataclass
class DeployPsFixture:
    """`scripts/deploy-ps.sh` copied into an isolated tree, plus the fake `az`.

    Grows across S6-S18 as later slices need more of it (fake `kubectl`/`helm`, more
    `azure-state` seeding helpers -- see PLAN.md §2), mirroring `DeployLlmFixture`'s own growth.
    """

    root: Path
    home: Path
    azure_state: Path
    bin_dir: Path
    az_log: Path
    kubectl_applied: Path
    kubectl_log: Path
    kubectl_apply_output_log: Path
    kubectl_state: Path
    helm_state: Path
    helm_log: Path

    @property
    def config_path(self) -> Path:
        """This fixture's own editable copy of `scripts/ps-defaults.conf`."""
        return self.root / "scripts" / "ps-defaults.conf"

    def fill_tls_contact_email(self, email: str = "tls-contact@example.test") -> None:
        """Overwrite the shipped-blank `TLS_CONTACT_EMAIL=""` with <email> in this fixture's copy
        of `scripts/ps-defaults.conf`. S8's region/capacity/quota tests (test_region_selection.py,
        test_capacity_validation.py, test_quota_check.py) call this before `seed_subscription` --
        they don't care about `prompt_for_tls_contact_email`'s own interactive behavior (that's
        test_config_validation.py's dedicated concern) and `run_deploy`'s default closed stdin
        would otherwise leave it empty, always failing `validate_config` before region selection
        ever runs.
        """
        text = self.config_path.read_text(encoding="utf-8")
        replaced = text.replace('TLS_CONTACT_EMAIL=""', f'TLS_CONTACT_EMAIL="{email}"')
        assert replaced != text, f'{self.config_path}: TLS_CONTACT_EMAIL="" not found to replace'
        self.config_path.write_text(replaced, encoding="utf-8")

    def _environment(self) -> dict[str, str]:
        """Environment for a script run: fake `az`/`kubectl`/`helm` prepended to `PATH`,
        throwaway `HOME`, plus the `PS_TEST_AZ_*`/`PS_TEST_KUBECTL_*`/`PS_TEST_HELM_*` variables
        the fakes read/write. All three fakes are always on `PATH` for every run (S14/S15
        onwards) regardless of which slice's own test is running -- `main()` is one linear flow
        with no flag to stop before S14/S15, so any full-success run of an earlier slice's test
        now also reaches `ensure_llm_secret`/`ensure_release` and needs both fakes present (same
        precedent as IMPL_SLICE_13.md's own note about S13's unconditional `aks create` call
        reaching every earlier full-success test).
        """
        return {
            "PATH": os.pathsep.join([str(self.bin_dir), "/usr/bin", "/bin"]),
            "HOME": str(self.home),
            "PS_TEST_AZ_LOG": str(self.az_log),
            "PS_TEST_AZ_STATE_DIR": str(self.azure_state),
            "PS_TEST_KUBECTL_APPLIED_DIR": str(self.kubectl_applied),
            "PS_TEST_KUBECTL_LOG": str(self.kubectl_log),
            "PS_TEST_KUBECTL_APPLY_OUTPUT_LOG": str(self.kubectl_apply_output_log),
            "PS_TEST_KUBECTL_STATE_DIR": str(self.kubectl_state),
            "PS_TEST_HELM_STATE_DIR": str(self.helm_state),
            "PS_TEST_HELM_LOG": str(self.helm_log),
        }

    def run_deploy(
        self,
        *args: str,
        stdin: str | None = None,
        expect: int | None = 0,
        extra_env: dict[str, str] | None = None,
    ) -> ScriptRun:
        """Run this fixture's copy of `scripts/deploy-ps.sh`.

        `stdin=None` closes stdin (`/dev/null`) so an unguarded `read` fails fast instead of
        hanging; pass a string to answer a prompt. `expect=None` to inspect the code yourself
        (mirrors `DeployLlmFixture.run_deploy`). `extra_env` overrides/adds environment
        variables on top of `_environment()`'s defaults -- new in S7, for tests that need a
        short `PROVIDER_REGISTRATION_WAIT_ATTEMPTS`/`_INTERVAL_SECONDS` instead of
        `deploy-ps.sh`'s real ~5-minute worst case (see the constants' own comment in
        `scripts/deploy-ps.sh`). `deploy_llm/conftest.py` has no equivalent because
        `deploy-llm.sh` never needed a test-overridable poll timeout.
        """
        script = self.root / "scripts" / "deploy-ps.sh"
        argv = [str(script), *args]
        env = self._environment()
        if extra_env:
            env.update(extra_env)
        if stdin is None:
            completed = subprocess.run(  # noqa: S603 - script path is a fixture-owned copy; args are test literals
                argv,
                cwd=self.root,
                env=env,
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        else:
            completed = subprocess.run(  # noqa: S603 - script path is a fixture-owned copy; args are test literals
                argv,
                cwd=self.root,
                env=env,
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
                input=stdin,
            )
        run = ScriptRun(completed.returncode, completed.stdout, completed.stderr)
        if expect is not None:
            assert run.returncode == expect, (
                f"deploy-ps.sh {' '.join(args)} -> {run.returncode}, expected {expect}\n"
                f"--- stdout ---\n{run.stdout}--- stderr ---\n{run.stderr}"
            )
        return run

    def read_az_log(self) -> list[str]:
        """Every argv line the fake `az` recorded, in order."""
        return self.az_log.read_text(encoding="utf-8").splitlines() if self.az_log.exists() else []

    def seed_subscription(
        self,
        id_: str = DEFAULT_SUBSCRIPTION_ID,
        upn: str = "evaluator@example.test",
        user_id: str = DEFAULT_USER_OBJECT_ID,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        """Write the identity files the fake `az` reads for `account show`/
        `account show --query user.name`/`ad signed-in-user show` (the last one added in S9 for
        `grant_keyvault_access`), plus a fully-authorized default baseline (Owner role) so that
        S5's own tests (which call only `seed_subscription()` and run `main()`'s full linear body
        up to whatever S6+ has appended) keep passing as later slices add more unconditional
        preflight steps -- same bundling rationale as
        `deploy_llm/conftest.py::DeployLlmFixture.seed_subscription`'s own docstring. A test
        exercising RBAC's negative/edge cases overrides this by calling `seed_role_assignments`
        afterward (fully replaces the file this method wrote).
        """
        self.azure_state.mkdir(parents=True, exist_ok=True)
        (self.azure_state / "subscription-id").write_text(id_, encoding="utf-8")
        (self.azure_state / "signed-in-user-upn").write_text(upn, encoding="utf-8")
        (self.azure_state / "signed-in-user-id").write_text(user_id, encoding="utf-8")
        (self.azure_state / "tenant-id").write_text(tenant_id, encoding="utf-8")
        self.seed_role_assignments("Owner", scope=f"/subscriptions/{id_}")
        # S8 baseline: both models Generally Available with ample capacity headroom and ample
        # quota, across every configured candidate region -- same bundling rationale as
        # deploy_llm/conftest.py's own seed_subscription docstring (S5's own tests, which call
        # only seed_subscription() and run main()'s full linear body, must keep passing as S8
        # appends its own unconditional next step). A test exercising a negative/edge case
        # overrides just the one region/piece it cares about afterward.
        cluster_name = _cluster_name_for(id_)
        for region in DEFAULT_REGION_CANDIDATES:
            self.seed_model_availability(region, chat_ga=True, embed_ga=True)
            self.seed_usage(
                region,
                chat=(0, AMPLE_QUOTA_LIMIT),
                embed=(0, AMPLE_QUOTA_LIMIT),
            )
            # S12 baseline: AKS_NODE_VM_SIZE unrestricted, ample vCPU family quota, across every
            # configured candidate region -- same bundling rationale as the model-availability/
            # usage seeding just above (S5-S11's own tests, which run main()'s full linear body,
            # must keep passing as S12 appends its own unconditional next step). A test exercising
            # S12's negative cases overrides just the one region it cares about afterward.
            self.seed_vm_skus(region)
            self.seed_vm_usage(region, current=0, limit=AMPLE_QUOTA_LIMIT)
            # S16 baseline: a public IP already sitting in the node resource group a cluster
            # created in THIS region would get (region varies per test; cluster name doesn't) --
            # same bundling rationale as the model-availability/vm-skus seeding above (S5-S15's
            # own tests, whose runs now also reach ensure_approuting/fetch_ingress_public_ip/
            # ensure_dns_label unconditionally, must keep passing without a real `sleep`). A test
            # exercising S16's own negative/edge cases overrides this via seed_no_ingress_ip/
            # seed_public_ip.
            self.seed_public_ip(
                _default_node_resource_group(cluster_name, region),
                DEFAULT_INGRESS_IP,
                region=region,
            )
        self.seed_ingress_service_ip(DEFAULT_INGRESS_IP)
        # S17 baseline: cert-manager's own deployments already Available -- same bundling
        # rationale as the S16 public-IP baseline just above (S5-S16's own tests, whose runs now
        # also reach ensure_cert_manager's `kubectl wait` unconditionally, must keep passing).
        self.seed_cert_manager_ready()

    def seed_role_assignments(self, *roles: str, scope: str | None = None) -> None:
        """Write the fake `az role assignment list --scope <scope>` response -- newline-separated
        role names, scope-keyed (PLAN.md §2.2) since S13 added a SECOND, cluster-scoped role
        check distinct from this subscription-scoped one. `scope` defaults to the subscription
        root `seed_subscription`'s own default `id_` uses -- pass it explicitly when seeding
        against a `seed_subscription(id_=...)` call that used a non-default id. Empty call
        (`seed_role_assignments()`) means no roles at all (S6's RBAC preflight negative case).
        """
        resolved_scope = scope if scope is not None else f"/subscriptions/{DEFAULT_SUBSCRIPTION_ID}"
        self._write_role_assignments(resolved_scope, roles)

    def _write_role_assignments(self, scope: str, roles: tuple[str, ...]) -> None:
        """Shared by `seed_role_assignments` (S6) and `seed_aks_rbac_granted` (S13) -- one
        scope-keyed write path (`_scope_key`), matching the fake `az`'s own `role assignment`
        dispatch exactly.
        """
        directory = self.azure_state / "role-assignments-by-scope"
        directory.mkdir(parents=True, exist_ok=True)
        content = "".join(f"{role}\n" for role in roles)
        (directory / _scope_key(scope)).write_text(content, encoding="utf-8")

    def seed_aks_cluster(
        self,
        name: str,
        *,
        subscription_id: str = DEFAULT_SUBSCRIPTION_ID,
        resource_group: str = RESOURCE_GROUP_NAME,
        node_resource_group: str | None = None,
    ) -> str:
        """Pre-populate an already-existing AKS cluster (S13) -- for
        `test_rerun_with_existing_cluster_makes_no_aks_create_call`. Returns the cluster's
        deterministic fake resource ID (`_aks_cluster_resource_id`, the same shape the fake
        `az aks create` itself writes) so a test can target `seed_aks_rbac_granted` at the exact
        same scope a real `grant_aks_rbac_access` run would use, without recomputing it.

        `node_resource_group` (S16) defaults to the same "MC_<rg>_<cluster>_swedencentral" shape
        the fake `az aks create` itself computes for a fresh cluster in the baseline-selected
        region (`_default_node_resource_group`) -- a test pre-seeding an existing cluster this
        way still reaches S16's public-exposure steps without a nodeResourceGroup lookup
        returning `null`.
        """
        resource_id = _aks_cluster_resource_id(
            name, subscription_id=subscription_id, resource_group=resource_group
        )
        resolved_node_resource_group = node_resource_group or _default_node_resource_group(
            name, "swedencentral"
        )
        directory = self.azure_state / "aks"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").write_text(
            json.dumps(
                {"id": resource_id, "name": name, "nodeResourceGroup": resolved_node_resource_group}
            ),
            encoding="utf-8",
        )
        return resource_id

    def seed_approuting_enabled(self, cluster_name: str) -> None:
        """Marks the application-routing add-on as already enabled for <cluster_name> (S16) --
        for `test_rerun_with_addon_already_enabled_makes_no_approuting_enable_call`. Combine with
        `seed_aks_cluster` (the add-on can only be queried on an existing cluster).
        """
        directory = self.azure_state / "aks"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{cluster_name}-approuting").touch()

    def seed_ingress_service_ip(
        self,
        ip: str = DEFAULT_INGRESS_IP,
        *,
        namespace: str = APP_ROUTING_NAMESPACE,
        name: str = APP_ROUTING_SERVICE_NAME,
    ) -> None:
        """Pre-populate the app-routing add-on's managed ingress-nginx Service's LoadBalancer
        external IP (S16) -- LB IP allocation is external to this script (PLAN.md §2.1), so every
        test whose run reaches `fetch_ingress_public_ip` needs this pre-seeded rather than
        produced by any fake `az`/`kubectl` call itself. Bundled into `seed_subscription`'s own
        baseline below so S5-S15's own tests -- which never call this method directly -- still
        get an IP on the very first poll attempt, never a real `sleep`.
        """
        directory = self.kubectl_state / "services" / namespace
        directory.mkdir(parents=True, exist_ok=True)
        payload = {"status": {"loadBalancer": {"ingress": [{"ip": ip}]}}}
        (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")

    def seed_no_ingress_ip(
        self, *, namespace: str = APP_ROUTING_NAMESPACE, name: str = APP_ROUTING_SERVICE_NAME
    ) -> None:
        """Removes any seeded ingress-service IP (including `seed_subscription`'s own bundled
        default) -- for `test_polls_for_the_ingress_public_ip_and_times_out_with_a_clear_message_
        if_never_assigned`, which must observe `fetch_ingress_public_ip` actually exhaust its
        poll attempts. Combine with a short `extra_env={"INGRESS_IP_WAIT_ATTEMPTS": ...,
        "INGRESS_IP_WAIT_INTERVAL_SECONDS": ...}` on `run_deploy` so the test doesn't wait out the
        real ~5-minute default (same pattern as `seed_stuck_provider`'s own docstring, S7).
        """
        path = self.kubectl_state / "services" / namespace / f"{name}.json"
        if path.exists():
            path.unlink()

    def seed_public_ip(
        self,
        node_resource_group: str,
        ip: str,
        *,
        resource_id: str | None = None,
        domain_label: str | None = None,
        fqdn: str | None = None,
        region: str = "swedencentral",
    ) -> str:
        """Pre-populate a public IP resource at <node_resource_group>/<ip> (S16) -- LB IP
        allocation is external to this script (PLAN.md §2.1: TEST-owned state, not written by any
        other fake `az` call). Writes both the canonical `public-ips/<node-rg>/<ip>.json` record
        AND an id-keyed reverse index (`public-ip-ids/<sanitized-id>`) the fake `az network
        public-ip show/update` need, since those only ever receive a bare resource ID
        (`--ids`), never the rg/ip pair `list` was queried with. Returns the resolved resource
        ID so a test (or `seed_subscription`'s own bundled baseline) can pass it back into
        further seeding/assertions without recomputing it.
        """
        resolved_id = resource_id or (
            f"/subscriptions/{DEFAULT_SUBSCRIPTION_ID}/resourceGroups/{node_resource_group}"
            f"/providers/Microsoft.Network/publicIPAddresses/pip-{_scope_key(node_resource_group)}"
        )
        directory = self.azure_state / "public-ips" / node_resource_group
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": resolved_id,
            "location": region,
            "dnsSettings": {"domainNameLabel": domain_label, "fqdn": fqdn},
        }
        (directory / f"{ip}.json").write_text(json.dumps(payload), encoding="utf-8")
        index_dir = self.azure_state / "public-ip-ids"
        index_dir.mkdir(parents=True, exist_ok=True)
        (index_dir / _scope_key(resolved_id)).write_text(
            f"{node_resource_group}/{ip}", encoding="utf-8"
        )
        return resolved_id

    def read_public_ip(self, node_resource_group: str, ip: str) -> dict[str, object] | None:
        """Reads back `public-ips/<node-rg>/<ip>.json` -- the fake `az network public-ip
        show`/`update`'s own current record for that IP, or None if never seeded/created (S16).
        """
        path = self.azure_state / "public-ips" / node_resource_group / f"{ip}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def seed_cert_manager_ready(
        self,
        *,
        deployments: tuple[str, ...] = CERT_MANAGER_DEPLOYMENTS,
        namespace: str = CERT_MANAGER_NAMESPACE,
    ) -> None:
        """Marks cert-manager's controller/webhook/cainjector deployments as already `Available`
        (S17) -- the fake `kubectl wait --for=condition=Available ...` reads these markers.
        Bundled into `seed_subscription`'s own baseline below so S5-S16's own tests -- which
        never call this method directly, and whose runs now also reach `ensure_cert_manager` --
        get an immediate, non-blocking `kubectl wait` success (this fake never actually sleeps;
        it only checks marker files).
        """
        directory = self.kubectl_state / "deployments" / namespace
        directory.mkdir(parents=True, exist_ok=True)
        for deployment_name in deployments:
            (directory / f"{deployment_name}-ready").touch()

    def seed_aks_rbac_granted(self, cluster_resource_id: str) -> None:
        """Pre-populate the `AKS_RBAC_ADMIN_ROLE` as already granted at <cluster_resource_id>
        scope (S13) -- for
        `test_rerun_with_role_already_granted_makes_no_role_assignment_create_call`. Pass the
        resource ID `seed_aks_cluster` returned so both target the identical scope.
        """
        self._write_role_assignments(cluster_resource_id, (AKS_RBAC_ADMIN_ROLE,))

    def seed_provider_registered(self, namespace: str) -> None:
        """Pre-seed `azure-state/providers/<namespace>` as already `Registered` -- for
        idempotency tests proving `ensure_providers_registered` makes no `provider register`
        call for a namespace that's already registered.
        """
        providers_dir = self.azure_state / "providers"
        providers_dir.mkdir(parents=True, exist_ok=True)
        (providers_dir / namespace).write_text("Registered", encoding="utf-8")

    def seed_stuck_provider(self, namespace: str) -> None:
        """Mark <namespace> so the fake `az provider register` call for it is a no-op forever --
        simulates a provider registration that never completes, for the registration-timeout
        test. Combine with a short `extra_env={"PROVIDER_REGISTRATION_WAIT_ATTEMPTS": "2", ...}`
        on `run_deploy` so the test doesn't actually wait out the real ~5-minute default.
        """
        providers_dir = self.azure_state / "providers"
        providers_dir.mkdir(parents=True, exist_ok=True)
        (providers_dir / f"{namespace}.never-registers").touch()

    def read_provider_state(self, namespace: str) -> str | None:
        """The fake `az`'s recorded registrationState for <namespace>, or None if never
        written.
        """
        path = self.azure_state / "providers" / namespace
        return path.read_text(encoding="utf-8") if path.exists() else None

    def seed_model_availability(
        self,
        region: str,
        *,
        chat_ga: bool,
        embed_ga: bool,
        chat_capacity_minimum: int | None = None,
        chat_capacity_maximum: int = 3000,
        embed_capacity_minimum: int | None = None,
        embed_capacity_maximum: int = 700,
        chat_version: str = DEFAULT_CHAT_MODEL_VERSION,
        embed_version: str = DEFAULT_EMBED_MODEL_VERSION,
    ) -> None:
        """Write `azure-state/model-availability/<region>.json` -- the fake `az cognitiveservices
        model list --location <region>` response `select_region`/`validate_capacity_range`/
        `model_version` (S8) parse. One entry per configured model; `lifecycleStatus` is
        `GenerallyAvailable` when its `*_ga` flag is true, `Preview` otherwise.

        `chat_capacity_minimum`/`embed_capacity_minimum` default to `None` (JSON `null`) rather
        than `1` -- unlike `deploy_llm/conftest.py`'s own equivalent, this default deliberately
        matches the real Azure shape confirmed for the DataZoneStandard/Standard SKUs
        `scripts/ps-defaults.conf` actually configures (spikes/deploy-ps-azure/README.md "Bugs
        found and fixed"), so every test using the baseline -- not just the one dedicated bugfix
        test -- exercises `model_capacity_range`'s `// 0` coalesce.
        """
        payload = [
            _model_availability_entry(
                DEFAULT_CHAT_MODEL_NAME,
                DEFAULT_CHAT_MODEL_SKU,
                is_generally_available=chat_ga,
                capacity_minimum=chat_capacity_minimum,
                capacity_maximum=chat_capacity_maximum,
                version=chat_version,
            ),
            _model_availability_entry(
                DEFAULT_EMBED_MODEL_NAME,
                DEFAULT_EMBED_MODEL_SKU,
                is_generally_available=embed_ga,
                capacity_minimum=embed_capacity_minimum,
                capacity_maximum=embed_capacity_maximum,
                version=embed_version,
            ),
        ]
        directory = self.azure_state / "model-availability"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{region}.json").write_text(json.dumps(payload), encoding="utf-8")

    def seed_usage(
        self,
        region: str,
        *,
        chat: tuple[float, float],
        embed: tuple[float, float],
    ) -> None:
        """Write `azure-state/usage/<region>.json` -- the fake `az cognitiveservices usage list
        --location <region>` response `check_quota` (S8) parses. `chat`/`embed` are each
        `(current_value, limit)`, keyed by the real per-model+SKU usage-entry name
        (`quota_usage_key`'s shape, e.g. "OpenAI.DataZoneStandard.gpt-5.4-mini") -- not the
        literal "chat"/"embed" keys `deploy_llm/conftest.py`'s own `seed_usage` still writes for
        `scripts/deploy-llm.sh`'s uncorrected quota_usage_key equivalent.

        Values may be floats (e.g. `950.0`) -- confirmed against a real subscription that Azure
        reports `currentValue`/`limit` as floats, which `model_remaining_quota`'s `jq floor` must
        handle instead of bash `$(( ))` arithmetic (which cannot parse a decimal point).
        """
        chat_current, chat_limit = chat
        embed_current, embed_limit = embed
        payload = [
            {
                "name": {"value": _usage_key(DEFAULT_CHAT_MODEL_SKU, DEFAULT_CHAT_MODEL_NAME)},
                "currentValue": chat_current,
                "limit": chat_limit,
            },
            {
                "name": {"value": _usage_key(DEFAULT_EMBED_MODEL_SKU, DEFAULT_EMBED_MODEL_NAME)},
                "currentValue": embed_current,
                "limit": embed_limit,
            },
        ]
        directory = self.azure_state / "usage"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{region}.json").write_text(json.dumps(payload), encoding="utf-8")

    def seed_empty_usage(self, region: str) -> None:
        """Write an explicit empty `az cognitiveservices usage list` response for <region> --
        confirmed against a real subscription/region with zero prior deployments that Azure
        reports no usage entries at all, not a "0 used, full limit available" baseline
        (spikes/deploy-ps-azure/README.md "Bugs found and fixed": "Empty usage list on a
        subscription/region with zero prior deployments"). Behaviorally identical to never
        calling `seed_usage` for <region> at all (the fake `az`'s own absent-file fallback is
        also `[]`) -- this method exists so a test can say so explicitly rather than relying on
        that fallback silently.
        """
        directory = self.azure_state / "usage"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{region}.json").write_text("[]", encoding="utf-8")

    def seed_vm_skus(
        self,
        region: str,
        *,
        restricted: bool = False,
        restriction_reason: str = "NotAvailableForSubscription",
    ) -> None:
        """Write `azure-state/vm-skus/<region>.json` -- the fake `az vm list-skus --location
        <region> --size <size> --all -o json` response `vm_size_allowed`/`vm_size_restricted`
        (S12) parse. Defaults to unrestricted (empty `restrictions[]`) so `seed_subscription`'s
        bundled baseline never blocks on this preflight -- same bundling rationale as
        `seed_model_availability`/`seed_usage`'s own docstrings. PLAN.md §2.1: this state is
        TEST-owned, not written by any other fake `az` call in this fixture -- it stands in for
        real Azure subscription state (what the subscription's SKU catalog actually reports), not
        something `deploy-ps.sh` itself creates.
        """
        payload = [
            {
                "resourceType": "virtualMachines",
                "name": AKS_NODE_VM_SIZE,
                "locations": [region],
                "restrictions": (
                    [{"type": "Location", "values": [region], "reasonCode": restriction_reason}]
                    if restricted
                    else []
                ),
            }
        ]
        directory = self.azure_state / "vm-skus"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{region}.json").write_text(json.dumps(payload), encoding="utf-8")

    def seed_vm_usage(
        self, region: str, *, current: float = 0, limit: float = AMPLE_QUOTA_LIMIT
    ) -> None:
        """Write `azure-state/vm-usage/<region>.json` -- the fake `az vm list-usage --location
        <region> -o json` response `vm_family_quota_sufficient` (S12) parses. One entry, for
        AKS_NODE_VM_SIZE_FAMILY only -- real `az vm list-usage` returns every core-count family on
        the subscription, but `deploy-ps.sh` only ever reads the one it needs. PLAN.md §2.1: same
        TEST-owned state as `seed_vm_skus` above.
        """
        payload = [
            {
                "name": {"value": AKS_NODE_VM_SIZE_FAMILY},
                "currentValue": current,
                "limit": limit,
            }
        ]
        directory = self.azure_state / "vm-usage"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{region}.json").write_text(json.dumps(payload), encoding="utf-8")

    def seed_existing_deployment(self, account_name: str, deployment_name: str) -> None:
        """Pre-populate an already-existing Cognitive Services deployment -- for
        `check_quota`'s per-model "already deployed, skip" branch (S8's rerun-idempotency
        bugfix): confirmed against a real idempotent rerun that an already-created deployment's
        own allocated capacity counts against `currentValue`, so re-requesting that same
        capacity again reads as "0 remaining" even though no *new* capacity is actually needed.
        """
        marker = self.azure_state / "deployments" / account_name / deployment_name
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()

    def seed_existing_resource_group(
        self, name: str = "rg-policy-system", *, location: str = "swedencentral"
    ) -> None:
        """Pre-populate an already-existing resource group (S9). Proving "zero create calls" on
        a full rerun requires every create-if-absent target -- the resource group included -- to
        already exist, not just the account/deployments/vault. Same state shape as
        `deploy_llm/conftest.py::DeployLlmFixture.seed_existing_resource_group`'s own equivalent.
        """
        directory = self.azure_state / "resource-groups"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(location, encoding="utf-8")

    def seed_existing_account(
        self,
        name: str,
        *,
        endpoint: str | None = None,
        key1: str = "FAKE-KEY-1-INITIAL",
        key2: str = "FAKE-KEY-2-INITIAL",
    ) -> None:
        """Pre-populate an already-existing AIServices account and its key pair (S9) -- for
        idempotency tests that need an account without going through a prior `deploy-ps.sh` run.
        Defaults match the fake `az`'s own `cognitiveservices account create` defaults, so a
        seeded account looks like one this script itself would have just created.
        """
        resolved_endpoint = endpoint or f"https://{name}.cognitiveservices.azure.com/"
        directory = self.azure_state / "accounts"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").write_text(
            json.dumps({"properties": {"endpoint": resolved_endpoint}}), encoding="utf-8"
        )
        (directory / f"{name}-keys.json").write_text(
            json.dumps({"key1": key1, "key2": key2}), encoding="utf-8"
        )

    def seed_existing_keyvault(self, name: str) -> None:
        """Pre-populate an already-existing Key Vault marker (S9)."""
        directory = self.azure_state / "keyvaults"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").touch()

    def seed_existing_secret(self, vault: str, name: str, value: str) -> None:
        """Pre-populate an already-existing Key Vault secret value (S9)."""
        directory = self.azure_state / "keyvaults" / f"{vault}-secrets"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(value, encoding="utf-8")

    def read_secret(self, vault: str, name: str) -> str | None:
        """Reads back `keyvaults/<vault>-secrets/<name>`, or None if never written."""
        path = self.azure_state / "keyvaults" / f"{vault}-secrets" / name
        return path.read_text(encoding="utf-8") if path.exists() else None

    def read_keyvault_policies(self, vault: str) -> list[str]:
        """Every `keyvault set-policy` line recorded for <vault>, in order."""
        path = self.azure_state / "keyvaults" / f"{vault}-policies.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def read_kubectl_log(self) -> list[str]:
        """Every argv line the fake `kubectl` recorded, in order (S14) -- `create secret
        --dry-run=client` is deliberately unlogged (a pure, stateless transform; see
        `FAKE_KUBECTL_SCRIPT`'s own module comment), so only `apply -f` lines appear here.
        """
        return (
            self.kubectl_log.read_text(encoding="utf-8").splitlines()
            if self.kubectl_log.exists()
            else []
        )

    def read_kubectl_applied(self, kind: str, name: str) -> str | None:
        """Reads back `kubectl-applied/<kind>-<name>.yaml` -- the manifest the fake
        `kubectl apply -f -` last captured for that kind/name, or None if `apply` was never
        called for it. Overwritten (not appended) on rerun.
        """
        path = self.kubectl_applied / f"{kind}-{name}.yaml"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def read_kubectl_apply_output_log(self) -> list[str]:
        """Every `<kind>/<name> {created,configured,unchanged}` line the fake `kubectl apply -f
        -` printed to its own stdout, in order (S14) -- the exact machine-readable signal
        `apply_output_changed` (scripts/deploy-ps.sh) greps for, captured here as test-only
        instrumentation since the script itself only ever sees it inside a `$(...)` command
        substitution.
        """
        return (
            self.kubectl_apply_output_log.read_text(encoding="utf-8").splitlines()
            if self.kubectl_apply_output_log.exists()
            else []
        )

    def read_helm_log(self) -> list[str]:
        """Every argv line the fake `helm` recorded, in order (S15)."""
        return (
            self.helm_log.read_text(encoding="utf-8").splitlines() if self.helm_log.exists() else []
        )

    def read_helm_release_values(
        self, release: str = HELM_RELEASE_NAME
    ) -> dict[str, object] | None:
        """The fake `helm`'s own `helm-state/releases/<release>.json` -- the full nested values
        shape a real `helm get values -o json` would return for this release, or None if no
        `helm upgrade --install` has ever landed (nor been seeded via `seed_helm_release`) for
        it.
        """
        path = self.helm_state / "releases" / f"{release}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def seed_helm_release(
        self, values: dict[str, object], release: str = HELM_RELEASE_NAME
    ) -> None:
        """Pre-populate an already-deployed Helm release's full `helm get values` shape (S15) --
        for idempotent-rerun tests, and for
        `test_rerun_only_compares_the_five_script_set_fields_not_falkordb_or_llm_provider_from_values_prod`,
        which seeds this with extra `-f values-prod.yaml`-sourced fields
        `scripts/deploy-ps.sh` never sets itself (e.g. `falkordb.persistence.durableStorageClass`)
        alongside the 5 fields it does, proving the no-op comparison still reports "unchanged"
        despite the extra fields it was never asked to compare.
        """
        directory = self.helm_state / "releases"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{release}.json").write_text(json.dumps(values), encoding="utf-8")


def _copy_deploy_ps_files(root: Path) -> None:
    """Copy whichever of `DEPLOY_PS_RELATIVE_FILES` currently exist into `root`.

    `copy2` preserves the executable bit, so the script/lib executable-vs-not distinction
    (scripts/deploy-ps.sh executable, scripts/lib/*.sh not) survives the copy unchanged.
    """
    for relative in DEPLOY_PS_RELATIVE_FILES:
        source = REPO_ROOT / relative
        if not source.exists():
            continue
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


@pytest.fixture
def deploy_ps_fixture(tmp_path: Path) -> DeployPsFixture:
    """Build an isolated copy of the `scripts/deploy-ps.sh` tree under `tmp_path`, plus the fake
    `az`/`kubectl`/`helm` (this module's `FAKE_AZ_SCRIPT`/`FAKE_KUBECTL_SCRIPT`/
    `FAKE_HELM_SCRIPT`) on the same shared `bin/` directory -- all three always present (S14/S15
    onwards), since `main()` is one linear flow every full-success run now reaches (see
    `_environment`'s own comment).
    """
    _copy_deploy_ps_files(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_az = bin_dir / "az"
    fake_az.write_text(FAKE_AZ_SCRIPT, encoding="utf-8")
    fake_az.chmod(0o755)
    fake_kubectl = bin_dir / "kubectl"
    fake_kubectl.write_text(FAKE_KUBECTL_SCRIPT, encoding="utf-8")
    fake_kubectl.chmod(0o755)
    fake_helm = bin_dir / "helm"
    fake_helm.write_text(FAKE_HELM_SCRIPT, encoding="utf-8")
    fake_helm.chmod(0o755)
    return DeployPsFixture(
        root=tmp_path,
        home=home,
        azure_state=tmp_path / "azure-state",
        bin_dir=bin_dir,
        az_log=tmp_path / "az.log",
        kubectl_applied=tmp_path / "kubectl-applied",
        kubectl_log=tmp_path / "kubectl.log",
        kubectl_apply_output_log=tmp_path / "kubectl-apply-output.log",
        kubectl_state=tmp_path / "kubectl-state",
        helm_state=tmp_path / "helm-state",
        helm_log=tmp_path / "helm.log",
    )
