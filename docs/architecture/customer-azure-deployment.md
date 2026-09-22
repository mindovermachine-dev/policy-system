<!-- © 2026 Cartman ApS. All rights reserved. -->
# Policy System - Customer-Managed Azure Production Deployment - Architecture

**Status:** Shipped
**Container:** Deployment Tooling (`scripts/`, `charts/policy-system`)

---

## Table of Contents

1. [Overview](#overview)
2. [Threat Model & Scope](#threat-model--scope)
3. [Flow](#flow)
4. [Components](#components)
   - [deploy-ps.sh](#deploy-pssh)
     - [Configuration](#configuration)
     - [Entra app registrations](#entra-app-registrations)
     - [AKS cluster](#aks-cluster)
     - [Helm release and chart hardening profile](#helm-release-and-chart-hardening-profile)
     - [Public exposure and TLS](#public-exposure-and-tls)
     - [`--rotate-key`](#--rotate-key)
5. [Naming & Idempotency](#naming--idempotency)
6. [Region Selection & Quota](#region-selection--quota)
7. [Security & Permission Model](#security--permission-model)
8. [Out of Scope](#out-of-scope)
9. [Open Risks & Follow-ups](#open-risks--follow-ups)

---

## Overview

`docs/architecture/customer-azure-llm-bootstrap.md` describes the Local Test path: a single
evaluator standing up just the LLM backend (`scripts/deploy-llm.sh`) for a local `kind` cluster.
This document describes the separate, production/customer-tenant path: `scripts/deploy-ps.sh`,
which provisions a **complete**, internet-reachable Policy System deployment in a customer's own
Azure subscription — the LLM backend, the Entra ID app registrations PS-Cli and PS Service
authenticate through, an AKS cluster, the Helm release itself, and public HTTPS exposure with a
Let's Encrypt certificate. It is a new sibling script, not a wrapper around `deploy-llm.sh` — it
carries its own copy of the LLM-provisioning logic (region/capacity/quota/account/deployment/
vault/secrets), with six numeric-correctness bugfixes baked in that are deliberately not ported
back into `deploy-llm.sh` (see [Region Selection & Quota](#region-selection--quota)). Both
scripts remain real, independently useful, and continue to serve their own distinct paths — see
`customer-azure-llm-bootstrap.md`'s own "See also" note.

Like `deploy-llm.sh`, `deploy-ps.sh` is a single idempotent Bash script run locally under the
evaluator/operator's own `az` CLI session, against their own subscription — no CI/CD pipeline,
no template repo, no persisted state beyond what already exists in Azure and the target AKS
cluster.

---

## Threat Model & Scope

The trust boundary is still the deploying operator's own machine and Azure subscription — the
same single-identity, no-service-principal model `customer-azure-llm-bootstrap.md` establishes
(no non-interactive/service-principal auth branch; explicitly out of scope for this script too).
What's different from the Local Test path is the blast radius and the audience of what gets
created:

- The result is a real, internet-facing HTTPS endpoint (public DNS label, Let's Encrypt
  certificate, an Ingress), not a `kind` cluster on the operator's own laptop. Once provisioned,
  PS Service is reachable by anyone who can resolve the hostname, gated by the OIDC bearer-token
  validation PS Service itself already enforces (issue #58) — this script's job is standing the
  endpoint up correctly (real audience/scopes wiring, a real TLS certificate), not adding a new
  authorization layer of its own.
- The AKS cluster is hardened relative to a bare cluster: Entra ID (AAD) integrated
  authentication, Azure RBAC authorization, local (certificate-based) Kubernetes accounts
  disabled, and Azure CNI network policy enabled — see [Security & Permission
  Model](#security--permission-model).
- The subscription-scope preflight (Owner/Contributor) and the user-only identity model are
  unchanged accepted tradeoffs, same as `deploy-llm.sh` — a genuinely least-privilege custom RBAC
  role and managed-identity auth remain open follow-ons, not resolved here (see [Open
  Risks](#open-risks--follow-ups)).

---

## Flow

```mermaid
graph TB
    Operator((Operator))

    subgraph Azure["Customer's Azure Subscription"]
        RG["Resource Group\nrg-policy-system"]
        AIS["AIServices Account\npolicy-system-llm-&lt;hash8&gt;"]
        Dep1["Deployment: LLM_CHAT_MODEL_NAME"]
        Dep2["Deployment: LLM_EMBED_MODEL_NAME"]
        KV["Key Vault\nkv-ps-llm-&lt;hash8&gt;"]
        Entra["Entra ID\nAPI app + CLI app + SPs"]
        AKS["AKS cluster\naks-policy-system-&lt;hash8&gt;\n(AAD + Azure RBAC)"]
        PublicIP["Public IP\nps-&lt;hash8&gt;.&lt;region&gt;.cloudapp.azure.com"]
    end

    subgraph Cluster["AKS cluster"]
        Secret["Secret: policy-system-llm-credentials"]
        Release["Helm release: policy-system"]
        CertMgr["cert-manager +\nClusterIssuer letsencrypt-prod"]
        Ingress["Ingress: policy-system-ps-service\n(TLS via cert-manager)"]
    end

    Operator -- "1. deploy-ps.sh\n(az CLI, idempotent)" --> RG
    RG --> AIS --> Dep1
    AIS --> Dep2
    AIS -- "keys/endpoint" --> KV
    Operator -- "2. Entra app registrations" --> Entra
    Operator -- "3. AKS create + RBAC grant" --> AKS
    KV -- "synced as k8s Secret" --> Secret
    Operator -- "4. helm upgrade --install\n(scopes/audience from Entra)" --> Release
    Secret --> Release
    Entra -- "issuer/audience/cliClientId/scopes" --> Release
    Operator -- "5. app-routing add-on + DNS label" --> PublicIP
    Operator -- "6. cert-manager + ClusterIssuer" --> CertMgr
    Operator -- "7. PS Service Ingress" --> Ingress
    PublicIP --> Ingress
    CertMgr --> Ingress
    Release --> Ingress
```

---

## Components

### deploy-ps.sh

Bash script (`scripts/deploy-ps.sh`) wrapping `az`, `kubectl`, and `helm`. `main()`'s
provisioning order, each step create-if-absent unless noted:

1. **Load and validate configuration** (`load_config`, `prompt_for_tls_contact_email`,
   `validate_config`) from `scripts/ps-defaults.conf` (see [Configuration](#configuration)).
2. **Compute deterministic resource names** (see [Naming & Idempotency](#naming--idempotency))
   and **show a confirmation table** (`print_confirmation_table`) listing every resource type
   before anything is created; `Proceed with these values? [Y/n]` (`confirm_or_exit`) — `N` exits
   0 with no Azure calls beyond `az account show`, `--yes` skips the prompt.
3. **RBAC preflight** (`rbac_preflight`) — read-only `az role assignment list` for the signed-in
   user at subscription scope, requiring `Owner` or `Contributor`; fails with an actionable fix
   command before touching anything else. User-only, no service-principal branch (reuses
   `deploy-llm.sh`'s own identity pattern).
4. **Resource-provider registration** (`ensure_providers_registered`) — registers and polls all
   nine namespaces in `REQUIRED_PROVIDERS` (`Microsoft.CognitiveServices`,
   `Microsoft.ContainerService`, `Microsoft.KeyVault`, `Microsoft.Network`, `Microsoft.Compute`,
   `Microsoft.ManagedIdentity`, `Microsoft.OperationsManagement`, `Microsoft.OperationalInsights`,
   `Microsoft.Insights`) to `Registered` before any dependent resource is created — a fresh
   subscription has only `Microsoft.Authorization` registered by default.
5. **Region selection, capacity, and quota** (`select_region`, `validate_capacity_range`,
   `check_quota`) — see [Region Selection & Quota](#region-selection--quota).
6. **Core LLM resources** (`ensure_resource_group`, `ensure_account`, `ensure_deployment` ×2,
   `ensure_keyvault`, `grant_keyvault_access`, `write_secret_if_changed` ×3) — resource group,
   `AIServices` account (kind `AIServices`, SKU `S0`), the two model deployments, an
   access-policy-based Key Vault, and the three `AZURE-API-BASE`/`AZURE-API-KEY`/
   `AZURE-API-VERSION` secrets. Own copy of this chain, not shared with/sourced from
   `deploy-llm.sh` — see [Overview](#overview).
7. **Entra app registrations** — see [below](#entra-app-registrations).
8. **AKS node VM-size and quota preflight** (`check_aks_vm_size`) — see [AKS
   cluster](#aks-cluster).
9. **AKS cluster** — see [below](#aks-cluster).
10. **LLM secret sync into the cluster** (`ensure_llm_secret`) — the same three LLM credentials,
    already in Key Vault, written into the cluster as the `policy-system-llm-credentials`
    Kubernetes Secret (underscore-keyed), same idiom as `sync-llm-secrets-to-kind.sh`.
11. **Helm release** — see [below](#helm-release-and-chart-hardening-profile).
12. **Public exposure and TLS** — see [below](#public-exposure-and-tls).
13. **Provisioning summary** (`print_provisioning_summary`) — names which secrets exist (never
    their values) and prints the resulting `https://<hostname>` URL.

### Configuration

`scripts/ps-defaults.conf` is a checked-in, no-secrets, evaluator-tunable defaults file — the
production-path counterpart to `scripts/llm-defaults.conf`, but a separate file, not a shared
one, because the two scripts' default SKUs genuinely differ (see [Region Selection &
Quota](#region-selection--quota)):

- `LLM_REGION_CANDIDATES` — ordered EU region candidate list
- `LLM_CHAT_MODEL_NAME` / `LLM_CHAT_MODEL_SKU` / `LLM_CHAT_MODEL_CAPACITY`
- `LLM_EMBED_MODEL_NAME` / `LLM_EMBED_MODEL_SKU` / `LLM_EMBED_MODEL_CAPACITY`
- `TLS_CONTACT_EMAIL` — Let's Encrypt registration contact; if left blank, `deploy-ps.sh` prompts
  for it interactively before validating the rest of the config (`prompt_for_tls_contact_email`)

Unlike `llm-defaults.conf`, both model SKUs are evaluator-tunable fields here (validated
non-empty by `validate_config`), not hardcoded literals — the spike this script is built from
found that different SKUs have non-zero default quota on a fresh subscription than
`deploy-llm.sh`'s hardcoded choice.

### Entra app registrations

`ensure_api_app_registration` creates (if absent) the **API app** (`API_APP_NAME`, "Policy System
API") — the resource server PS Service represents — its service principal
(`ensure_service_principal`, closing the `AADSTS650052` "no service principal" gap), sets
`api.identifierUris`, and PATCHes in an `access_as_user` `oauth2PermissionScope` with
`api.requestedAccessTokenVersion: 2` (a Graph-API-created registration defaults to v1 tokens
unless this is set explicitly, per `docs/artifacts/idp-configuration-contract.md`'s documented
pitfall). If the signed-in identity can't create app registrations,
`print_app_registration_manual_steps` prints the exact `az ad app create`/`az ad sp create`/
`az ad app update` commands for a privileged colleague to run, and the operator re-runs the
script afterward.

`ensure_cli_app_registration` then creates (if absent) the **CLI app** (`CLI_APP_NAME`, "Policy
System CLI") — a public client with the native-client redirect URI
(`CLI_REDIRECT_URI`) — its service principal, and a delegated-permission grant on the API app's
`access_as_user` scope. Before attempting the privileged `az ad app permission admin-consent`
write, it checks `admin_consent_granted` (an unprivileged `az ad app permission list-grants`
read filtered to `consentType=='AllPrincipals'`) — so a rerun after a colleague already granted
consent out of band doesn't re-attempt (and re-fail) the same write. A non-admin operator whose
consent attempt fails gets `print_admin_consent_manual_step`'s exact command instead of a bare
`az` error.

### AKS cluster

Before creating the cluster, `check_aks_vm_size` runs a preflight against the fixed node shape
this script always requests — `AKS_NODE_VM_SIZE="Standard_D4as_v7"`, `AKS_NODE_COUNT=2` (not
evaluator-tunable):

- **Allowlist** (`vm_size_allowed`/`vm_size_restricted`) — `az vm list-skus --size
  Standard_D4as_v7 --all` for the target region; fails if any entry's `restrictions[]` marks the
  size `NotAvailableForSubscription`, or restricts the region specifically.
- **vCPU quota** (`vm_family_quota_sufficient`) — `az vm list-usage`; fails unless
  `standardDASv7Family`'s remaining quota covers `AKS_NODE_COUNT × AKS_NODE_VM_SIZE_VCPUS` (2 × 4
  = 8 vCPUs).

Both checks fail with the actual restriction reason / quota numbers shown, before `az aks create`
is ever called. **This mechanism has no precedent in the exploratory spike this script is built
from** — it is new design against documented `az` CLI surfaces, not yet empirically verified
against a live subscription the way the rest of this script's Azure interactions are (see [Open
Risks](#open-risks--follow-ups)).

`ensure_aks_cluster` then creates the cluster (if absent) with `--enable-aad
--enable-azure-rbac --disable-local-accounts --network-plugin azure --network-policy azure
--tier free --node-os-upgrade-channel SecurityPatch`. `--network-plugin azure` must accompany
`--network-policy azure` — omitting it fails `az aks create` outright. `grant_aks_rbac_access`
then grants the deploying identity the built-in "Azure Kubernetes Service RBAC Cluster Admin"
role, scoped to this cluster's own resource ID only (never the subscription root) — without it,
the `--enable-azure-rbac` cluster rejects every subsequent `kubectl`/`helm` call regardless of
the operator's subscription-level Owner/Contributor role. `ensure_aks_credentials` then points
the local `kubectl`/`helm` at the cluster.

### Helm release and chart hardening profile

`ensure_release` runs `helm upgrade --install policy-system oci://ghcr.io/mindovermachine-dev/
charts/policy-system -f charts/policy-system/values-prod.yaml`, resolving the chart's own
production values file directly (`VALUES_PROD_FILE`, no locally-copied duplicate that could
drift), plus five explicit `--set` overrides: `llm.existingSecret`, `psService.auth.issuer`
(`https://login.microsoftonline.com/<tenant-id>/v2.0`), `psService.auth.audience` (the **bare**
API app GUID — never the `api://...` URI form; using the URI form here is the exact bug that
makes login succeed but every API call 401), `psService.auth.cliClientId`, and
`psService.auth.scopes` (the `api://<api-app-id>/access_as_user` URI form — the opposite
convention, used for the OAuth scope request, not audience validation). A rerun compares these
same five fields, extracted from `helm get values -o json`, against the desired values — not the
whole values object, which would also echo back `values-prod.yaml`'s own `falkordb.*`/
`llm.provider` fields this script never sets and would permanently defeat no-op detection. An
unchanged rerun makes no `helm upgrade` call at all.

Because the release is always installed with `-f values-prod.yaml`, it always carries that
values file's own hardening profile: the durable, `Premium_LRS`/`Retain` FalkorDB `StorageClass`
(`falkordb.persistence.durableStorageClass.enabled: true`) and the unconditional FalkorDB
`NetworkPolicy` restricting ingress to `ps-service` pods only — chart features that exist
independently of this script (rendered by the chart itself) but are only active in the profile
`deploy-ps.sh` deploys, not the Local Test path's default `values.yaml`.

### Public exposure and TLS

`ensure_approuting` enables the AKS application-routing add-on (a managed NGINX ingress
controller) if not already on. `fetch_ingress_public_ip` polls the add-on's own `nginx` Service
in the `app-routing-system` namespace for its LoadBalancer IP, `fetch_public_ip_resource_id`
resolves that IP's Azure resource ID (it lives in the AKS-managed node resource group, not
`rg-policy-system`), and `ensure_dns_label` sets Azure's own public-IP DNS label (`dns_label`,
see [Naming](#naming--idempotency)) — giving a `<label>.<region>.cloudapp.azure.com` hostname
with no customer-owned domain or DNS zone required.

`ensure_cert_manager` then installs cert-manager itself via its own published OCI chart
(`oci://quay.io/jetstack/charts/cert-manager`, namespace `cert-manager`) if absent, and — only on
a fresh install — waits (`kubectl wait --for=condition=Available`) for its controller, webhook,
and cainjector deployments to report `Available` before returning. The AKS application-routing
add-on does **not** bundle cert-manager; without this step there is nothing to issue a
certificate. `ensure_cluster_issuer` creates the `letsencrypt-prod` `ClusterIssuer` — solving
ACME HTTP-01 through the app-routing add-on's own ingress class
(`webapprouting.kubernetes.azure.com`) — strictly after cert-manager is confirmed `Available`;
applying it immediately after a fresh cert-manager install can otherwise fail webhook admission.

`ensure_ps_service_ingress` creates the TLS-terminated `Ingress` for PS Service itself
(`policy-system-ps-service`, matching the chart's own rendered Service name), annotated with the
`ClusterIssuer` above and a `tls:` block naming the resolved hostname.

### `--rotate-key`

`scripts/deploy-ps.sh --rotate-key` branches immediately after flag parsing (`rotate_key_main`),
before config validation, the confirmation table, RBAC preflight, or any region/quota/AKS/Helm
step — none of those matter for rotating an already-provisioned account's key. It requires a
prior successful deploy (`require_account_exists`/`require_keyvault_exists` fail clearly
otherwise), compares the currently-stored `AZURE-API-KEY` secret against the account's live
`key2` to determine the active slot (`active_key_slot`), regenerates the other, inactive slot
(`az cognitiveservices account keys regenerate`), and writes the new value back to Key Vault —
carried over near-verbatim from `deploy-llm.sh`'s own proven `--rotate-key` implementation. Never
prints a key value, old or new.

---

## Naming & Idempotency

Every name is deterministic — computed from the subscription ID via `subscription_hash8`
(`scripts/lib/deploy-llm-common.sh`, first 8 hex characters of `sha256(subscription-id)`) — so a
rerun against the same subscription always computes the same names and finds what a prior run
already created.

| Resource | Name pattern | Function | Shared with `deploy-llm.sh`? |
| --- | --- | --- | --- |
| Resource group | `rg-policy-system` | `RESOURCE_GROUP_NAME` constant | Yes — renamed from `rg-policy-system-llm` for this issue; both scripts now target the same group |
| `AIServices` account | `policy-system-llm-<hash8>` | `llm_account_name` | Yes — same function, same computed name for a given subscription |
| Key Vault | `kv-ps-llm-<hash8>` | `llm_keyvault_name` | Yes — same function, same computed name |
| AKS cluster | `aks-policy-system-<hash8>` | `aks_cluster_name` | No — new in this script |
| Public DNS label | `ps-<hash8>` | `dns_label` | No — new in this script |

`aks_cluster_name`/`dns_label` were added to the existing, shared
`scripts/lib/deploy-llm-common.sh` rather than a new lib — one naming lib, one
`RESOURCE_GROUP_NAME`, sourced by both scripts. Because the resource-group, account, and Key
Vault naming functions are shared, running both scripts against the *same* subscription resolves
to the *same* resource group, `AIServices` account, and Key Vault (see [Open
Risks](#open-risks--follow-ups)).

---

## Region Selection & Quota

Same four EU-only candidate regions as `deploy-llm.sh` (`swedencentral`, `francecentral`,
`westeurope`, `germanywestcentral`), validated by `validate_region_candidates` against that fixed
allowlist. `select_region` probes each in configured order via `az cognitiveservices model list`
and picks the first where both configured models are `GenerallyAvailable` at the configured SKU;
`validate_capacity_range` then checks the configured capacities against that region's live
reported min/max.

`deploy-ps.sh` carries its own copy of this logic with six numeric-correctness fixes found
during the exploratory spike that are not present in `deploy-llm.sh`:

1. **Null capacity minimum** — `model_capacity_range` coalesces a `null` `capacity.minimum` (SKUs
   with no enforced floor, e.g. `DataZoneStandard`) to `0` instead of crashing bash arithmetic on
   the literal string `"null"`.
2. **Real quota-usage key** — `quota_usage_key` builds the actual `az cognitiveservices usage
   list` entry key (`OpenAI.<Sku>.<ModelName>`, e.g. `OpenAI.DataZoneStandard.gpt-5.4-mini`)
   instead of `deploy-llm.sh`'s uncorrected literal `"chat"`/`"embed"` keys, which never match any
   real entry.
3. **Empty usage list is not a hard fail** — `model_usage_entry_exists` skips (with a printed
   note) the quota check for a model/SKU Azure reports no usage entry for at all, rather than
   reading that as zero remaining quota.
4. **Float-safe quota arithmetic** — `model_remaining_quota` computes `limit - currentValue` in
   `jq` (with `floor`), since Azure reports these as floats bash's `$(( ))` cannot parse.
5. **No false quota failure on rerun** — `check_quota` skips a model's quota check entirely once
   its deployment already exists (`deployment_exists`), since an existing deployment's own
   capacity already counts against `currentValue`, which would otherwise make an unchanged rerun
   read as "0 remaining."
6. **`--model-version` is resolved and passed** — `model_version` reads `.model.version` from the
   already-fetched `model list` response and feeds it into `ensure_deployment`'s
   `--model-version` flag, which Azure's `account deployment create` now hard-requires.

**Quota is pooled per subscription/data-zone, not per region** — switching candidate regions
cannot work around exhausted quota; a quota failure is a hard stop with a message to request an
increase, and no other region is tried in response to it (region fallback happens only on a
model-*availability* failure).

---

## Security & Permission Model

- **RBAC preflight is read-only**, same posture as `deploy-llm.sh` — the script never grants
  subscription-scope roles itself; it stops and tells the operator what's missing.
- **No CI/CD identity, no stored long-lived credentials.** Everything runs under the operator's
  own interactive `az login` session. No service-principal branch exists (§0.5 of this issue's
  implementation plan) — deferred as a follow-on, same as `deploy-llm.sh`.
- **AKS is AAD- and Azure-RBAC-integrated**, with local (certificate-based) Kubernetes accounts
  disabled (`--disable-local-accounts`) — cluster access is gated through Entra identities, not a
  shared cluster-admin certificate.
- **The AKS RBAC grant is cluster-scoped, not subscription-scoped** — the deploying identity gets
  "Azure Kubernetes Service RBAC Cluster Admin" only at this specific cluster's resource ID.
- **Azure CNI network policy is enabled at the cluster level** (`--network-policy azure`), and
  the chart's own `NetworkPolicy` (S3/S4 of this issue) restricts FalkorDB ingress to `ps-service`
  pods only — the cluster flag alone creates no restriction by itself; the chart resource is the
  actual enforcement.
- **Key Vault uses access-policy authorization** (matching `deploy-llm.sh`'s reference vault),
  granting the deploying identity `get/list/set` on secrets, scoped to that vault only.
- **Public exposure is real** — unlike the Local Test path, this script's endpoint is reachable
  over the public internet once DNS propagates, secured by a genuine Let's Encrypt certificate
  and PS Service's own existing OIDC bearer-token validation (issue #58), not by network
  isolation.
- **Key rotation is manual, not automatic** — `--rotate-key` on demand, same hygiene-tool posture
  as `deploy-llm.sh`; nothing rotates on a schedule.
- **Secrets are never logged.** Raw API key/token values are only ever compared or forwarded to
  `az`/`kubectl`, never printed — the closing summary names secret *identifiers* only, and the
  HTTPS URL, never a value.

---

## Out of Scope

- **No service-principal/non-interactive auth branch** — user-only identity resolution, same as
  `deploy-llm.sh`; tracked as a follow-on, not implemented here.
- **No least-privilege custom RBAC role** — the subscription-scope Owner/Contributor preflight is
  an accepted tradeoff, not a production-grade least-privilege design.
- **No automated teardown.** Cleanup remains a documented, not scripted, set of commands.
- **No multi-region/multi-cluster/HA topology** — one fixed 2-node AKS cluster shape
  (`Standard_D4as_v7`) per subscription, not evaluator-tunable.
- **No chart version pinning by this script** — `CHART_REF` tracks no explicit `--version`; a
  chart release landing between runs is picked up on the next `helm upgrade --install`.

---

## Open Risks & Follow-ups

- **AC-BI-011's AKS VM-size/quota preflight mechanism (`check_aks_vm_size`) is new design, not
  empirically verified against a live subscription** — unlike every other Azure interaction in
  this script, the exploratory spike this script is built from left this specific check
  undischarged (documented there as a manual step). Re-verification against a real subscription
  is recommended before relying on it at the same confidence level as the rest of this script.
- **Running `deploy-llm.sh` and `deploy-ps.sh` against the same subscription targets the same
  resource group, `AIServices` account, and Key Vault** — both scripts share `RESOURCE_GROUP_NAME`
  and the `llm_account_name`/`llm_keyvault_name` naming functions in
  `scripts/lib/deploy-llm-common.sh`. This is not a defect in either script individually, but an
  operator moving from evaluating locally (Local Test path) to a production deployment in the
  *same* subscription should be aware the LLM backend resources are reused, not duplicated.
  Whether that reuse is desirable or should be forced apart (e.g. a distinguishing name segment)
  has not been decided.
- **No live-Azure re-verification of AC-BI-017's full TLS chain by this script's own test
  suite** — the unauthenticated-401 half is proven by PS Service's existing OIDC test suite
  (issue #58), and the Ingress/TLS manifest shape is proven locally, but the live "a real
  certificate is issued and `/health` returns 200" claim is evidenced only by a one-time
  empirical run during this script's development, not re-exercised automatically on every change.
- **Depends on the same open items `customer-azure-llm-bootstrap.md` already tracks** for the
  shared LLM-provisioning half: quota-exhaustion has no automated remedy beyond detection, and a
  subscription lacking access to all four candidate regions is not yet handled with a specific
  remedy.
