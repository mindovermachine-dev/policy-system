# Threat & Coverage Map

The non-biased "what we probe / what is out of coverage" model. This is the §"coverage" artifact and
is where the global `security-engineer` skill's threat modeling (STRIDE, OWASP, SANS, supply-chain)
lands for the Policy System.

## Standing principle

**Floor, not ceiling.** This map states the *minimum* we probe. A finding discovered **outside** this
map is **welcome and expected** — note it, and (if systematic) add the threat here. Discovery is
open-ended and is **not** bounded by this list. "We tested the map" never means "we tested
everything."

## Assets & trust boundaries (from `charts/policy-system`)

- **ps-service** — serves the **REST API and the Streamable-HTTP MCP endpoint on the same host:port**
  (ClusterIP; the evaluator reaches it through the proxy on port `30443`). Auth = **IdP-agnostic OIDC bearer tokens** applied via middleware
  (`ps_service/auth/` — `verifier`, `middleware`, `discovery`, `protected_resource`, `startup`, `models`, `errors`).
- **FalkorDB** — graph store (deployment + `falkordb-service`). **We expose** a FalkorDB **browser
  UI on NodePort `30300`** (`falkordb-browser-service.yaml`).
- **LLM egress** — Azure/OpenAI via LiteLLM (`llm_interface/`); graph content flows to a third-party
  vendor for chat + embeddings.
- **CI/CD** — `.github/workflows/*` (on_dev, on_main, on_ready, on_semver, pr-to-ready,
  copilot-setup-steps) + `.github/scripts/`.
- **Secrets** — Helm `secret.yaml` template; LLM API keys; any runtime credentials the service holds.
- **ps-cli** — the user-facing client; doubles as the **black-box attack client** in tests.

> Image hygiene: `falkordb.image.tag: latest` (nondeterministic; pinning tracked in #277) and a
> floating Python base-image tag (digest pinning tracked in #278). Version-pinning of third-party
> images is itself in coverage (supply-chain), even though we do not own the images' internals.

## Scope notes for "auth or not-auth"

Today the credential model is binary: **Scenario A = no creds**, **Scenario B = with a credential**
(one role, parameterized — add roles later without restructuring). "With a credential" means "with a
legitimate, low-priv role's token" unless stated otherwise.

## Per-surface threats

Format: **Asset → Threat → Probe (what we do) → Healthy/expected → Out-of-coverage.**
Severity is **post-hoc and scenario-tagged** (a threat that succeeds in **A** with no creds is far
worse than the same success in **B** with a legitimate token).

### 1. Authentication & the REST↔MCP boundary  *(P2)*
- **Code anchors:** `ps_service/auth/` (verifier, middleware, discovery, protected resource),
  `ps_service/mcp_interface/http_transport.py` (the MCP mount shares the same verifier),
  `ps_service/api/routes.py`.
- **Threat:** unauthenticated reach to a protected endpoint; the MCP endpoint inheriting or *not*
  inheriting the REST auth guard because they share a port.
- **Probe:** enumerate REST + MCP surfaces; request each without a token; request with
  expired / wrong-audience / replayed / tampered / **`alg=none`** / wrong-issuer / wrong-scope
  tokens; check "every protected endpoint 401/403 unauth".
- **Healthy/expected:** unauth → 401/403 to *everything* the auth boundary claims to guard; MCP is a
  first-class protected surface, not a back door to the REST surface's authority.
- **Target-state (post-hoc):** no path to guarded state without a valid, in-audience, in-scope,
  in-lifetime token.
- **Out-of-coverage:** the IdP's own internal security (the issuer we trust per `issuer`/`audience`
  config).

### 2. Authorization / authz integrity  *(Scenario B, P3)*
- **Code anchors:** `ps_service/authz/` (roles, RBAC), `ps_service/policy_lifecycle/rules`,
  `ps_service/graph_cleanup`, `ps_service/passkey_signing/` (signed approvals),
  `ps_service/invitations/`, `ps_service/mcp_interface/` (`tool_core.py` gate constants, `tool_gates.py` gate registration, one `*_tools.py` per tool group).
- **Threat:** a legitimate low-priv token reaches admin/data-store/deploy surfaces; privilege
  escalation by role confusion.
- **Probe:** with a low-priv token, attempt admin, graph write/delete, and deploy-relevant surfaces.
- **Healthy/expected:** low-priv token is confined to its scope.
- **Target-state (post-hoc):** authz boundaries hold per role.

### 3. Input handling, injection & prompt-safety  *(P3)*
- **Code anchors:** `ps_service/query_engine/` (read-only guard), `ps_service/graph_gateway/`
  (label allow-list), the `*/store.py` Postgres stores (parameterized statements),
  `ps_service/api/schemas/`.
- **Threat:** injection at REST/MCP/CLI input; **prompt-injection** altering grounded answers;
  **grounding-trust erosion** (the system citing/leaking content it shouldn't); command/DB-query
  injection into the graph query engine.
- **Probe:** crafted inputs at every documented input surface; prompt-injection payloads; attempts
  to make the engine run an unintended query or return un-retrieved content.
- **Healthy/expected:** inputs are validated; grounding cannot be coerced to emit un-retrieved or
  attacker-supplied content as if it were graph-grounded.
- **Out-of-coverage:** the *correctness/quality* of natural-language answers (we test
  leakage/trust-erosion, not answer accuracy).

### 4. Data exfiltration, incl. to a third party  *(P3/P4)*
- **Code anchors:** `ps_service/llm_interface/`, `ps_service/api/error_handlers.py`
  (error-body scrubbing), `ps_service/export/`.
- **Threat:** unauthenticated exfil of graph content; **exfil via LLM egress** (graph/PII leaking to
  Azure/OpenAI through prompts/embeddings); exfil via error responses / response bodies.
- **Probe:** attempt bulk/graph read without auth (A) and beyond scope (B); try to route content into
  the LLM call and observe it on the vendor side (synthetic sentinel data only).
- **Healthy/expected:** no bulk read without proper auth; only intended content crosses the egress;
  no data leaks via errors.
- **Target-state (post-hoc):** no exfil of guarded data through any channel, incl. the LLM vendor.

### 5. Data store (FalkorDB) & our exposure of its browser UI  *(P4)*
- **Code anchors:** `ps_service/graph_gateway/` (the single write path), `charts/policy-system`
  `falkordb-*` templates and `values-prod.yaml` (browser as `ClusterIP`).
- **Threat:** the store is **reachable from the API layer** in a way that bypasses app auth; the
  FalkorDB **browser UI (NodePort 30300)** is reachable from **a network the team does not fully
  control** / is on in production.
- **Probe:** can the API layer reach the store without the app's auth? Is the browser reachable from
  outside the trusted local cluster?
- **Healthy/expected (target-state, post-hoc):** local-cluster reachability of the browser is
  acceptable; reachability from an untrusted network (or being on in prod) **is a finding**. The
  store is reached only through app auth.
- **Out-of-coverage:** the FalkorDB browser's **internal** security / its own CVEs (the FalkorDB
  team's). *Our* exposure and version-pinning (see §7) are in scope.

### 6. Secrets, RBAC, network policy & CI/CD  *(P5)*
- **Code anchors:** `charts/policy-system/templates` (secrets, deployments, network policies),
  `ps_service/config.py` (credentials default to unset), `.github/workflows`.
- **Threat:** secrets leaked to logs/error responses/CI artifacts; over-broad pod RBAC; missing
  NetworkPolicy; **CI pipeline takeover** — code with push access reaches runtime secrets or drives a
  deploy, or exfiltrates a token from `.github/`.
- **Probe:** audit `.github/workflows/*` + `.github/scripts/` for secret flow + un-pinned
  actions; `helm template` the secret/chart and check for leakage; simulate a push-from-a-fork against
  the test pipeline; check pod RBAC + NetworkPolicy posture.
- **Healthy/expected (target-state, post-hoc):** no secret in logs/errors/CI; least-privilege RBAC;
  push access cannot reach runtime secrets or a deploy.
- **Out-of-coverage:** the GitHub platform's own security.

### 7. Transport (TLS) & supply chain  *(P1, re-checked P5)*
- **Code anchors:** `Dockerfile`, `uv.lock`, outbound clients in `ps_service/curated_source`,
  `ps_service/ingestion/adapters/cellar_eli`, `ps_service/auth/discovery.py`.
- **Threat:** weak/disabled TLS (downgrade, weak ciphers/expired certs); vulnerable or
  un-pinned/vulnerable third-party dependencies and **images** (incl. `tag: latest`).
- **Probe:** `testssl.sh` on transport; `trivy`/`pip-audit` on deps; image-vuln + version-pinning
  check on every image we deploy.
- **Healthy/expected (target-state, post-hoc):** modern TLS; pinned, scanned, low-severity-clean
  third-party components.
- **Out-of-coverage:** the *interior* security of components we do not maintain (we pin + scan;
  fixing their internals is upstream's job).

## Surfaces added since the map was written

Each is covered by the sections above unless noted; probes should include them:

- **Passkey signing and approval pages** (`ps_service/passkey_signing/`): browser-facing, exempt
  from the bearer middleware by design (section 1, section 2).
- **Invitations** (`ps_service/invitations/`): service-credential call to the IdP admin API
  (section 2, section 6).
- **Audit trail** (`ps_service/audit/`): who-did-what records and the admin read tool (section 2).
- **Restore, export and curated-source egress** (`ps_service/restore/`, `export/`,
  `curated_source/`): artefact handling and operator-configured outbound fetches (section 3,
  section 7).
- **MCP tool surface** (`ps_service/mcp_interface/`): 38 tools behind per-tool gates (section 1,
  section 2).
- **Graph write gateway** (`ps_service/graph_gateway/`): the single, allow-listed write path
  (section 3, section 5).
- **ps-cli credential storage** (`ps-cli/src/ps_cli/credentials.py`): encrypted per-context token
  store (section 6).

## Not yet covered

Threat categories with no probe today (tracked where an issue exists):

- Container hardening baseline and network isolation of the service pod (#292).
- Request-size limiting on inbound and outbound payload paths (#288).
- Request throttling (#291).
- Input-validation hardening of public metadata endpoints (#289).
- CI action pinning to commit identifiers (#257).
- Recurring automated security scans in CI (no issue yet).

## Explicit out-of-coverage

- Claude Desktop Plugin (out by decision).
- Real production, real secrets, real PII (the test instance is dedicated + synthetic).
- FalkorDB browser **internal** security (the FalkorDB team's).
- Correctness/quality of LLM-generated answers.
- The trusted IdP/issuer's and the GitHub platform's own internal security.
- The *interior* security of third-party components we deploy (we pin/scan/expose-wisely; not author
  their code).
