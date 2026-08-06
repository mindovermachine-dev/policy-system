# Synthetic Data Spec — Fictional Pharma/Biotech Company

Design for the Policy/Standard/Control (and one internal Regulation) layer
we'll fabricate and load on top of the real CRA/NIS2/GDPR graph from
[`graph-ingestion3`](../graph-ingestion3/), so the blocked questions in
[`example-questions.md`](./example-questions.md) (S7, S8, M7, M8, H1, H2,
H4, H5, H7) become answerable against real graph structure instead of
staying hypothetical. Written before any JSON is generated, per the
spec-first sequencing decision — the shape below is derived directly from
what those specific questions need, including deliberate negative cases.

Loads the same way every other regulation file does: a JSON in
`{ graph_name, nodes, edges }` shape, `python load_graph.py --file
helvex.json --graph-name policy_system` (no `--reset` — layers on top of
CRA/NIS2/GDPR already loaded).

## Company

**Helvex Biotech ApS** — fictional, EU-based, mid-size biotech running
clinical trials and a patient-data platform. Picked because pharma/biotech
gives us a domain (GxP / clinical data integrity) that CRA/NIS2/GDPR don't
fully cover, which is useful for testing what happens when an internal
source mints a genuinely new Capability rather than converging on an
existing one — both cases need to be in the fixture, not just the tidy
convergence case the toy example already shows.

## Reference date

The fixture is authored **as of `2026-08-01`**. Date-range questions (H7:
"due for review in the next 30 days") need a fixed anchor so the golden
answer is reproducible regardless of when a test actually runs — dates in
the fixture are chosen relative to this anchor, not to wall-clock "today."

## New internal Regulation (2 versions, to give H5 real data)

Mirrors the `ENGPRAC-2.1` pattern from the domain doc's worked example, but
loaded as real data rather than staying toy-only.

| id | title | status | note |
|---|---|---|---|
| `HELVEX-SOP-1.0` | Helvex Data Security & Quality SOP | `superseded` | original version |
| `HELVEX-SOP-2.0` | Helvex Data Security & Quality SOP | `active` | `HELVEX-SOP-1.0 -SUPERSEDED_BY-> HELVEX-SOP-2.0`; tightens the logging requirement and adds a new clinical-data-integrity requirement not present in v1.0 |

**Roles:** `Clinical Data Steward`, `QA Manager` (both `DEFINES`-ed by
`HELVEX-SOP-2.0`; `Clinical Data Steward` also carried by v1.0 to give the
supersession something to actually change).

**Requirements/Obligations (illustrative, finalized when we write the
JSON):**
- v1.0: "Service owners shall log access to clinical trial records" → Obligation converging on the *existing real* `cap_security_logging_c4d9e2` (Security Logging) — same capability CRA/NIS2 already require, demonstrating internal-source convergence on real data, not just the toy example.
- v2.0: same logging requirement, tightened, **plus** a new requirement — "Clinical Data Stewards shall maintain a tamper-evident audit trail of all clinical trial record changes, per GxP" → mints a **new** Capability, `cap_clinical_data_integrity_helvex` ("Clinical Trial Data Integrity / GxP Audit Trail"), since nothing in CRA/NIS2/GDPR covers this. This is the deliberate non-convergence case.

## Capability selection

Rather than inventing a parallel set, the Policy layer governs a subset of
**real** Capabilities already extracted from CRA/NIS2/GDPR, plus the one new
`cap_clinical_data_integrity_helvex` above. Split deliberately into
*governed* (has a Policy) and *ungoverned* (doesn't) — the ungoverned set is
what makes H2 a real gap-analysis question instead of a trivial "all of
them."

**Governed** (13 total — 12 real + 1 new):
`cap_data_encryption_0e50d3`, `cap_access_control_authentication_151816`,
`cap_security_logging_c4d9e2`, `cap_data_configuration_integrity_protection_882f84`,
`cap_security_incident_reporting_449fa4`, `cap_incident_handling_4cf73e`,
`cap_vulnerability_management_55d0c4`, `cap_business_continuity_disaster_recovery_9c1c32`,
`cap_asset_personnel_security_management_e68e9a`, `cap_secure_data_removal_portability_3d7885`,
`cap_data_protection_impact_assessment_a51acb`, `cap_data_protection_officer_management_ec3cd2`,
`cap_clinical_data_integrity_helvex` (new)

**Deliberately ungoverned**: everything else. Only 12 real capabilities (+
the 1 new Helvex one) got a Policy — realistically, a company doesn't
author a Policy for every capability that two dozen EU regulations happen
to touch. Confirmed against the live graph: 68 total capabilities (67 real
+ 1 new), 13 governed, **55 ungoverned** — that 55-capability set (via
`MATCH (c:Capability) WHERE NOT (c)-[:GOVERNED_BY]->(:Policy) RETURN
c.name`) *is* the golden answer to H2, not a small hand-picked list.

## Policies (4 — spread across the status lifecycle on purpose)

| id (slug) | title | status | governs | why this status |
|---|---|---|---|---|
| `pol_data_protection_security` | Data Protection & Security Policy | `approved` | Data Encryption, Access Control & Authentication, Security Logging, Data & Configuration Integrity Protection, Secure Data Removal & Portability | the "everything's fine" baseline case |
| `pol_incident_vulnerability_response` | Incident & Vulnerability Response Policy | `approved` | Security Incident Reporting, Incident Handling, Vulnerability Management, Business Continuity & Disaster Recovery | second clean baseline, exercises M7/M2-style chains on a different branch |
| `pol_clinical_data_integrity` | Clinical Data Integrity Policy | `draft` | Clinical Trial Data Integrity (Helvex), Data Protection Impact Assessment | **not yet approved** — deliberately gives H1 ("are we compliant with GDPR Art. 32") a non-trivial, partial answer for anything routed through DPIA, and gives H5 something real to flag as "not ready" |
| `pol_legacy_asset_security` | Legacy Asset & Personnel Security Policy | `deprecated` | Asset & Personnel Security Management, DPO Management | **stale on purpose** — superseded in practice by policy not yet written; exercises "is this policy still trustworthy" reasoning distinct from the draft case above |

## Standards (2 per approved/draft Policy, 1 for the deprecated one)

Following the `std_{POLICY}_{VERSION}` identity pattern from the domain doc.
Titles TBD at JSON-authoring time; each just needs `implementation_status`
spread across `implemented` / `reviewed` / `draft` so Standard-level status
questions aren't uniform either.

## Controls (deliberate date/status spread, anchored to `2026-08-01`)

This is the table that actually answers H7 ("due for review in the next 30
days") and gives H1/H4 real evidence pointers to cite instead of nothing.

| Control (under) | `implementation_status` | `next_review_date` | why |
|---|---|---|---|
| Encryption-at-rest check | `implemented` | `2026-08-15` | due within 30 days → **should** show up in H7's answer |
| Access control / MFA audit | `implemented` | `2026-08-25` | due within 30 days → **should** show up |
| Log retention integrity check | `implemented` | `2026-11-01` | not due soon → **should not** show up in H7 |
| Incident triage SLA check | `implemented` | `2026-07-20` | **overdue** relative to the `2026-08-01` anchor — tests whether a mechanism distinguishes "overdue" from "upcoming" rather than lumping both into one bucket |
| Vulnerability patch SLA check | `planned` | *(none yet)* | not implemented — tests status filtering (shouldn't be claimed as a passing control) |
| Legacy asset inventory check | `deprecated` | `2026-01-01` (stale) | under the deprecated Policy — control still exists but shouldn't be reported as current evidence |

Each `implemented`/`reviewed` Control gets a plausible `evidence_ref`
(opaque pointer, per the domain doc — we don't build a real evidence store)
so H4 has something concrete to return plus the "evidence store is out of
scope" caveat.

## What this unblocks, concretely

| Question | Before | After |
|---|---|---|
| S7, S8 | 🟡 toy data only | ✅ real Policy/Standard exist |
| M7 | ⛔ chain breaks after Capability | ✅ full chain exists for at least the two approved-Policy branches |
| M8 | 🟡 toy internal reg only | ✅ real `HELVEX-SOP-*` converges on `cap_security_logging_c4d9e2`, same as CRA |
| H1 | ⛔ | rubric-graded, but now has real (partial) chains to reason over instead of nothing |
| H2 | ⛔ (trivial "all of them") | ✅ non-trivial set-match — golden answer is the 55-capability ungoverned set above (confirmed live) |
| H4 | ⛔ | ✅ exact-match — real `evidence_ref` values to return |
| H5 | ⛔ | rubric-graded, but now `HELVEX-SOP-1.0 -SUPERSEDED_BY-> HELVEX-SOP-2.0` plus the deprecated/draft Policies give it real signal |
| H7 | ⛔ (toy data only) | ✅ set-match — golden answer is exactly the two `2026-08-15`/`2026-08-25` controls |

## Status: generated and loaded

Implemented as [`helvex_source.json`](./helvex_source.json) (intermediate
authoring format) expanded and loaded by
[`build_helvex_graph.py`](./build_helvex_graph.py) into
[`helvex.json`](./helvex.json) (29 nodes, 43 edges), layered onto the
existing `policy_system` graph in FalkorDB alongside CRA/NIS2/GDPR — no
`--reset`. See that script's docstring for the intermediate-format
rationale (hand-typing `pol_{slug}_{hash}`-style ids invites typos; the
script resolves human-readable keys instead and validates every capability
reference against the real CRA/NIS2/GDPR extraction).

Verified live against FalkorDB (`localhost:6379`, graph `policy_system`):
- H7's golden answer is exactly the 2 controls designed for it (`2026-08-15`, `2026-08-25`), nothing else.
- H2's golden answer is 55 ungoverned capabilities (see correction above).
- Convergence works on real data, not just the toy example: CRA's "Manufacturer" obligation and Helvex's "Clinical Data Steward" obligation both resolve to `cap_security_logging_c4d9e2`.

To reload after edits: `python build_helvex_graph.py` (regenerates
`helvex.json` from `helvex_source.json` and re-loads; MERGE-based, so
re-running is idempotent). Use `--no-load` to only regenerate the JSON.
