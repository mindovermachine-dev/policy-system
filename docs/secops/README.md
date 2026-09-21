# SecOps — Policy System security posture test

A **repeatable, scenario-driven penetration/posture test** of the Policy System, runnable ad-hoc
with an agent as operator. It produces a **posture score + trend**, a **findings list**, and a
**triage signal**, and it *grows* across runs so posture is tracked over time.

## Why this shape

- **Discovery, not conformance.** This is *not* a test to a fixed pass/fail spec. Pre-declared
  "must-pass" criteria bias the test toward confirming priors and produce false confidence. Instead
  the run *finds* what it can and a **human evaluates the report after**.
- **Floor, not ceiling.** `threat-coverage-map.md` states a *minimum* of what we probe; findings
  outside it are welcome and expected.
- **"Shoulds" are target-states.** Things like "the FalkorDB browser must not be reachable from
  an untrusted network" are written into a finding's *remediation*, after discovery — never supplied
  as something the test checks *toward* up front.

## Scope

In: `ps-service` REST **and** MCP API · Helm-deployed infra on kind/cluster (FalkorDB store; *our
exposure of* the browser UI) · LLM/prompt egress (Azure/OpenAI via LiteLLM) · GitHub Actions CI/CD ·
`ps-cli` as an attack client · TLS/transport · third-party components we deploy (CVE scan +
version-pinning check).

Out: Claude Desktop Plugin · real production · real PII · FalkorDB browser *internal* security
(the FalkorDB team's; we test *our exposure* of it, not the component).

## How to run

Invoke the **`ps-pentest`** skill ("run the pen test" / "scenario A" / "scenario B"). The skill is the
agent-runnable procedure; the files below are its data + audit trail. You may also run pieces by hand
with the tools named in `ps-pentest` and the `security-engineer` skill.

## Files

| File | What it is |
|------|------------|
| `threat-coverage-map.md` | The non-biased "what we probe / out-of-coverage" model (STRIDE + supply-chain + CI), per surface. |
| `baseline.yaml` | Versioned, growing **known-findings registry** + severity/scoring config. The **diff target** for regression. Starts empty on purpose. |
| `posture-history.md` | Rolling **posture score + trend** table — one row per run. |
| `run-report-template.md` | The per-run **3-part output** shape (posture · findings · triage signal). |

## Ownership & cadence

- **Owner:** (the team running it). Per run, the agent executes and *updates* `baseline.yaml` +
  `posture-history.md`; a **human** reviews the report and decides what to fix.
- **Cadence:** ad-hoc today. The structure supports a later CI/scheduled baseline (automated checks
  like `trivy`/`pip-audit`/`gitleaks`/`nmap`) — deferred.
- **No commit by the agent:** the agent creates/updates these artifacts but does **not** stage or
  commit; the user commits after review.

> Note: `docs/audits/` also exists in this repo. Artifacts live under `docs/secops/` by decision;
> link or move here if the team prefers to co-locate with `audits/`.
