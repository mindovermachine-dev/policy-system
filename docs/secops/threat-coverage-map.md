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
  (default NodePort `30800`). Auth = **IdP-agnostic OIDC bearer tokens** applied via middleware
  (`ps_service/auth/` — `verifier`, `middleware`, `discovery`, `protected-resource`).
- **FalkorDB** — graph store (deployment + `falkordb-service`). **We expose** a FalkorDB **browser
  UI on NodePort `30300`** (`falkordb-browser-service.yaml`).
- **LLM egress** — Azure/OpenAI via LiteLLM (`llm_interface/`); graph content flows to a third-party
  vendor for chat + embeddings.
- **CI/CD** — `.github/workflows/*` (on_dev, on_main, on_ready, on_semver, pr-to-ready,
  copilot-setup-steps) + `.github/scripts/`.
- **Secrets** — Helm `secret.yaml` template; LLM API keys; any runtime credentials the service holds.
- **ps-cli** — the user-facing client; doubles as the **black-box attack client** in tests.

> Image hygiene: `falkordb.image.tag: latest` (nondeterministic). Version-pinning of third-party
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
- **Threat:** a legitimate low-priv token reaches admin/data-store/deploy surfaces; privilege
  escalation by role confusion.
- **Probe:** with a low-priv token, attempt admin, graph write/delete, and deploy-relevant surfaces.
- **Healthy/expected:** low-priv token is confined to its scope.
- **Target-state (post-hoc):** authz boundaries hold per role.

### 3. Input handling, injection & prompt-safety  *(P3)*
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
- **Threat:** unauthenticated exfil of graph content; **exfil via LLM egress** (graph/PII leaking to
  Azure/OpenAI through prompts/embeddings); exfil via error responses / response bodies.
- **Probe:** attempt bulk/graph read without auth (A) and beyond scope (B); try to route content into
  the LLM call and observe it on the vendor side (synthetic sentinel data only).
- **Healthy/expected:** no bulk read without proper auth; only intended content crosses the egress;
  no data leaks via errors.
- **Target-state (post-hoc):** no exfil of guarded data through any channel, incl. the LLM vendor.

### 5. Data store (FalkorDB) & our exposure of its browser UI  *(P4)*
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
- **Threat:** weak/disabled TLS (downgrade, weak ciphers/expired certs); vulnerable or
  un-pinned/vulnerable third-party dependencies and **images** (incl. `tag: latest`).
- **Probe:** `testssl.sh` on transport; `trivy`/`pip-audit` on deps; image-vuln + version-pinning
  check on every image we deploy.
- **Healthy/expected (target-state, post-hoc):** modern TLS; pinned, scanned, low-severity-clean
  third-party components.
- **Out-of-coverage:** the *interior* security of components we do not maintain (we pin + scan;
  fixing their internals is upstream's job).

## Explicit out-of-coverage

- Claude Desktop Plugin (out by decision).
- Real production, real secrets, real PII (the test instance is dedicated + synthetic).
- FalkorDB browser **internal** security (the FalkorDB team's).
- Correctness/quality of LLM-generated answers.
- The trusted IdP/issuer's and the GitHub platform's own internal security.
- The *interior* security of third-party components we deploy (we pin/scan/expose-wisely; not author
  their code).
