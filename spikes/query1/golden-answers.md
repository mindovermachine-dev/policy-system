# Golden Answers & Rubrics

Computed live against FalkorDB's `policy_system` graph (CRA + NIS2 + GDPR +
Helvex, per [`graph-ingestion3`](../graph-ingestion3/) +
[`build_helvex_graph.py`](./build_helvex_graph.py)) as of `2026-08-06`, for
every question in [`example-questions.md`](./example-questions.md). This is
the ground truth a candidate query mechanism gets scored against per that
doc's grading column.

Two things fell out of computing these that weren't visible from reading the
catalog and specs alone:

1. **Three questions used illustrative entity names that don't exist in the
   real extraction** (S2's "CRA Article 11", S4/S6's "Maintain Security
   Monitoring" / "Maintain Structured Access Logging"). These were written
   before extraction and never reconciled against it. Fixed below and in
   `example-questions.md`.
2. **M7 and part of H1 were marked ⛔/blocked on a stale premise.** That was
   true before the Helvex spike loaded a Policy/Standard/Control layer over
   *real* CRA/NIS2/GDPR capabilities (not just Helvex-specific ones) — the
   catalog and this spike's README were never updated after that landed.
   Full GDPR Requirement→...→Control chains exist today (57 of them). See M7
   and H1 below.

Every Cypher block below can be run directly:
`docker exec -i <falkordb-container> redis-cli GRAPH.QUERY policy_system "<query>"`,
or via the `falkordb` Python client against `localhost:6379`.

---

## Simple tier

### S1 — "What roles does GDPR define?" (set-match)
```cypher
MATCH (r:Regulation {id:"GDPR-1.0"})-[:DEFINES]->(role:Role) RETURN role.name
```
**Golden:** `{Controller, Processor, Joint controller, Data Protection Officer, Representative}`

### S2 — "What's the text of CRA Article 11?" (exact-match) — **catalog fix needed**
CRA's extraction scope is Art. 13, 14, 18–24 and Annex I only (see
`cra-extraction-methodology.md`) — **Article 11 was never extracted**. The
honest golden answer today is "not in extraction scope," not a text
passage, which makes this a poor exemplar for a *retrieval* question. Reuse
CRA-1.0_req_art_13.1 (the umbrella "comply with Annex I Part I" clause,
already used for M2) or renumber the question to `CRA-1.0_req_art_13.2`.
**Golden (Art. 13.1):** "Manufacturers shall ensure that a product with
digital elements is designed, developed and produced in accordance with the
essential cybersecurity requirements set out in Part I of Annex I."

### S3 — "What obligations does the Manufacturer role carry under CRA?" (set-match)
```cypher
MATCH (:Regulation {id:"CRA-1.0"})-[:DEFINES]->(:Role {name:"Manufacturer"})-[:HAS]->(o:Obligation)
RETURN o.id, o.text ORDER BY o.id
```
**Golden:** 48 obligations (`obl_apply_data_minimisation_563d25` … `obl_take_corrective_action_for_non_conforming_products_2abebb`) — full list is the query result, too long to inline; a scoring harness should diff id sets, not eyeball this one. (Corrected from an initial manual eyeball-count of 47 — verified 48 by two independent Cypher phrasings when building `query_mechanism_v1.py`'s test harness. Lesson generalized into the FalkorDB reliability note under M7: don't trust a hand-count on a 40+ row list, verify with `count()`.)

### S4 — "What capabilities does 'Maintain Security Monitoring' require?" (set-match) — **catalog fix needed**
No obligation named "Maintain Security Monitoring" exists. Closest real
obligation: `obl_maintain_security_logging_c427be` ("Maintain Security
Logging"). Rename the question to match.
```cypher
MATCH (:Obligation {id:"obl_maintain_security_logging_c427be"})-[:REQUIRES]->(c:Capability) RETURN c.id, c.name
```
**Golden:** `{cap_security_logging_c4d9e2 — Security Logging}`

### S5 — "When does CRA become effective, and what's its current status?" (exact-match)
```cypher
MATCH (r:Regulation {id:"CRA-1.0"}) RETURN r.effective_date, r.status
```
**Golden:** `effective_date = 2027-12-11`, `status = active` (note: "active" reflects the regulation's in-force/adopted state, not that the effective date has passed — CRA phases in ahead of full applicability, same as the real regulation)

### S6 — "Which requirement does the 'Maintain Structured Access Logging' obligation satisfy?" (exact-match) — **catalog fix needed**
No obligation named "Maintain Structured Access Logging" exists either — same real obligation as S4 fits (tests the inbound `SATISFIED_BY` edge instead of `REQUIRES`, so keeping both S4 and S6 pointed at the same real obligation is fine, they exercise different edges). Rename the question to "Maintain Security Logging."
```cypher
MATCH (req:Requirement)-[:SATISFIED_BY]->(:Obligation {id:"obl_maintain_security_logging_c427be"}) RETURN req.id, req.text
```
**Golden:** `CRA-1.0_req_annex1_pt1_2l` — "Products shall provide security-related information by recording and monitoring relevant internal activity, including access to or modification of data, services or functions, with a user opt-out mechanism."

### S7 — "What policy governs the 'Security Logging' capability?" (exact-match)
```cypher
MATCH (:Capability {id:"cap_security_logging_c4d9e2"})-[:GOVERNED_BY]->(p:Policy) RETURN p.id, p.title, p.status
```
**Golden:** `pol_data_protection_security_policy_8e4c18` — "Data Protection & Security Policy" (`approved`)

### S8 — "List the standards under the Data Protection Policy." (set-match)
```cypher
MATCH (:Policy {id:"pol_data_protection_security_policy_8e4c18"})-[:SUPPORTED_BY]->(s:Standard) RETURN s.id, s.title, s.implementation_status
```
**Golden:** 3 standards — Encryption-at-Rest & In-Transit (`implemented`), Access Control/MFA & Session (`implemented`), Security Log Retention & SIEM (`reviewed`)

---

## Medium tier

### M1 — "Which capabilities are required by more than one obligation?" (set-match)
```cypher
MATCH (o:Obligation)-[:REQUIRES]->(c:Capability)
WITH c, count(DISTINCT o) AS n WHERE n > 1
RETURN c.id, n ORDER BY n DESC
```
**Golden:** 52 capabilities, ranging from `cap_data_subject_rights_fulfilment_communication_8eedf0` (45 obligations) down to several at 2. Large set — a scoring harness should diff id sets, not eyeball.

### M2 — "Trace the full path from CRA Art. 11 to whatever capability it ultimately requires." (exact-match) — **catalog fix, same as S2**
Reuse Art. 13.1:
```cypher
MATCH (req:Requirement {id:"CRA-1.0_req_art_13.1"})-[:SATISFIED_BY]->(o:Obligation)-[:REQUIRES]->(c:Capability)
RETURN req.text, o.id, c.id
```
**Golden:** `CRA-1.0_req_art_13.1` → `obl_ensure_secure_product_design_and_development_c56d3c` ("Ensure Secure Product Design and Development") → `cap_secure_development_lifecycle_9f3224` ("Secure Development Lifecycle")

### M3 — "Which obligations, across all three loaded regulations, require a 'Security Logging'-type capability?" (rubric)
Not a set-match today — no merge curation would fix it. Confirmed live:
`cap_security_logging_c4d9e2`'s only sources are `CRA-1.0` and
`HELVEX-SOP-1.0`. NIS2 and GDPR were never extracted with a distinct
logging capability — NIS2's closest is folded into
`cap_access_control_authentication_151816` ("...report on unauthorised
access attempts"); GDPR's closest, `cap_data_protection_compliance_monitoring_478cb7`,
is about compliance monitoring/audits, not log capture, and is a
legitimately separate capacity (not a near-duplicate — this was checked
against `find_capability_duplicates.py`'s output).

**Rubric — a good answer must:**
- Name `obl_maintain_security_logging_c427be` (CRA) as the real converged obligation.
- Explicitly state that NIS2 and GDPR have **no** obligation requiring this capability today — not silently omit them, not invent a plausible-sounding one.
- Not claim "all three" have coverage; correct scope is "only CRA, of the three."

### M4 — "How many obligations does GDPR place on Data Processors vs. Data Controllers?" (exact-match)
```cypher
MATCH (:Regulation {id:"GDPR-1.0"})-[:DEFINES]->(role:Role)-[:HAS]->(o:Obligation)
RETURN role.name, count(o)
```
**Golden:** Controller = 148, Processor = 55 (Data Protection Officer = 7, Joint controller = 2, Representative = 1, for completeness if the question is read more broadly)

### M5 — "Do CRA and NIS2 impose obligations on similar roles?" (rubric)
Real roles: CRA = `{Manufacturer, Importer, Distributor, Authorised representative, Open-source software steward, Substantial modifier}`; NIS2 = `{Essential entity, Important entity}`.
**Rubric — a good answer must:**
- Cite these real role sets, not invented ones.
- Recognize there's no structural equivalence to exploit (Role is deliberately non-canonical) — a good answer reasons by semantic similarity (e.g. "Manufacturer" and "Essential/Important entity" both name duty-bearing product/service operators subject to risk-management obligations) rather than claiming a graph join proves it.
- Not overclaim a precise mapping the data doesn't support.

### M6 — "Which obligations are backed by the weakest extraction confidence?" (set-match against a fixed threshold)
```cypher
MATCH (o:Obligation) WHERE o.confidence <= 0.80 RETURN o.id, o.confidence ORDER BY o.confidence
```
**Golden (threshold 0.80):** 24 obligations — 3 at 0.75 (`obl_act_with_due_care_regarding_regulatory_compliance_40310b`, `obl_inform_users_of_risks_when_maintaining_public_software_archives_23a88e`, `obl_seek_data_subjects_views_on_intended_processing_where_appropriate_9621e4`) + 21 at 0.80. Threshold choice remains a rubric call, per the catalog's own note — 0.80 is a reasonable default since it's the graph's second-lowest confidence band and yields a non-trivial (not near-empty, not near-total) set.

### M7 — "Show every path from a GDPR requirement down to a Control that verifies it." — **status correction: 🟡, not ⛔**
```cypher
MATCH (req:Requirement)-[:SATISFIED_BY]->(o:Obligation)-[:REQUIRES]->(c:Capability)-[:GOVERNED_BY]->(p:Policy)-[:SUPPORTED_BY]->(s:Standard)-[:IMPLEMENTED_BY]->(ctrl:Control)
WHERE req.id STARTS WITH "GDPR"
RETURN req.id, o.id, c.id, p.id, p.status, s.id, s.implementation_status, ctrl.id, ctrl.implementation_status
```
**Golden:** 57 full chains exist today, covering GDPR Art. 28.3f/g, 32.1a/b/c, 32.4, 33.1/2/3a-d, 37.1/5/7, 38.1/2/3/6. This directly contradicts the catalog's current claim ("no real GDPR obligation currently reaches a Policy — chain breaks after Capability") — that was true before Helvex's Policy/Standard/Control layer was wired over *real* capabilities (not just the one new Helvex-specific one). Grading should move from "exact-match refusal" to **set-match**, with an honesty caveat carried over: computing an explicit `is_current_evidence` flag per chain (`p.status = 'approved' AND s.implementation_status IN ['implemented','reviewed'] AND ctrl.implementation_status = 'implemented'`) splits this **31 current-evidence / 26 stale** — 16 chains route through the `deprecated` Legacy Asset & Personnel Security Policy (Art. 32.4, 37.*, 38.* — personnel/DPO duties) and 10 more end in a `draft` Standard / `planned` (not yet implemented) Control under the otherwise-approved Incident & Vulnerability Response Policy (Art. 28.3f, 32.1c, 33.*). A good mechanism should surface all 57 chains but flag the 26 as stale/not-current-evidence, not present them as equally trustworthy. (Prototyped in `query_mechanism_v1.py` / verified in `test_query_mechanism_v1.py`.)

**FalkorDB reliability warning surfaced while implementing this:** the same 6-hop MATCH pattern returned **33** or even **49** rows instead of 57 depending on *which columns were projected* in the `RETURN` clause (e.g. projecting only `req.id` — or `req.id` plus a few properties but not all six matched node ids — silently dropped rows; projecting all six matched ids, or using `RETURN DISTINCT` across all of them, gave the correct 57). This was confirmed against an independent Python-side join of each hop's edges pulled separately, which is the only way the discrepancy was caught. **This is a query-engine correctness issue in this FalkorDB version, not a data or governance one** — a third category on top of the two discussed earlier (data health vs. governance filtering). Practical mitigation for any mechanism built on this database: for chains of 5+ hops, always project all matched node ids (or wrap in `RETURN DISTINCT` over all of them) and spot-check long-chain aggregate counts against an independently-computed join rather than trusting a single Cypher query's row count.

### M8 — "Which capabilities does our internal Helvex SOP regulation share with CRA?" (set-match) — already ✅, confirmed prior session
```cypher
MATCH (:Obligation)-[:REQUIRES]->(c:Capability)
WITH c, count(DISTINCT c) AS x
MATCH (reg:Regulation)-[:DEFINES]->(:Role)-[:HAS]->(:Obligation)-[:REQUIRES]->(c)
WHERE reg.id IN ["CRA-1.0","HELVEX-SOP-1.0","HELVEX-SOP-2.0"]
WITH c, collect(DISTINCT reg.id) AS regs
WHERE size(regs) > 1
RETURN c.id, regs
```
**Golden:** `{cap_security_logging_c4d9e2}` — shared via `CRA-1.0` + `HELVEX-SOP-1.0`.

---

## Hard tier

### H1 — "Are we compliant with GDPR Article 32?" (rubric) — **now has a real, textured chain to reason over**
Art. 32's sub-obligations resolve as follows (from M7's chain data, filtered to Art. 32):
- **32.1a** (encryption) → `cap_data_encryption_0e50d3` → approved Policy → 3 Standards, all `implemented`/`reviewed` — **clean**
- **32.1b** (CIA/resilience) → `cap_access_control_authentication_151816` + `cap_data_configuration_integrity_protection_882f84` → same approved Policy, same clean Standards — **clean**
- **32.1c** (restore availability after incident) → `cap_business_continuity_disaster_recovery_9c1c32` → approved Incident & Vulnerability Response Policy → one `implemented` Standard/Control, one `draft`/`planned` Standard/Control — **partial**
- **32.4** (personnel process only on instructions) → `cap_asset_personnel_security_management_e68e9a` → `pol_legacy_asset_personnel_security_policy_7ed6c2`, which is **`deprecated`** with a `deprecated` Standard/Control — **stale, not current evidence**

**Rubric — a good answer must:** conclude **partial compliance**, cite the real chain per sub-clause above, explicitly flag 32.4's evidence as stale (governed only by a deprecated policy — structurally present but shouldn't be reported as passing), and flag 32.1c's second control as not-yet-implemented rather than silently rolling it into "compliant."

### H2 — "Which capabilities required by CRA have no governing Policy yet?" (set-match) — already ✅, confirmed prior session
```cypher
MATCH (c:Capability) WHERE NOT (c)-[:GOVERNED_BY]->(:Policy) RETURN c.name
```
**Golden:** 55 ungoverned capabilities (13 of 68 total are governed). Confirmed live, matches `synthetic-data-spec.md`'s corrected figure.

### H3 — "Is this new API endpoint... compliant with GDPR Article 32?" (rubric)
Grounded in H1's real chain now: "logs access but doesn't encrypt data at rest" maps to `cap_access_control_authentication_151816`/logging (Art. 32.1b territory — covered, clean chain) but fails `cap_data_encryption_0e50d3` (Art. 32.1a — the Encryption-at-Rest Standard/Control exist and are `implemented`, so there's a concrete, named control the endpoint isn't passing).
**Rubric — a good answer must:** perform this NL→Capability mapping explicitly (not silently), cite the real Encryption-at-Rest control the endpoint fails, and conclude non-compliant on 32.1a specifically rather than a vague overall verdict.

### H4 — "Show me the audit evidence that our log retention control passed last quarter." (exact-match) — already ✅
```cypher
MATCH (:Standard)-[:IMPLEMENTED_BY]->(ctrl:Control) WHERE toLower(ctrl.title) CONTAINS "log retention" RETURN ctrl.evidence_ref
```
**Golden:** `evidence://ci/log-retention-check/latest` (control: `ctrl_std_pol_data_protection_security_policy_8e4c18_v3_automated`, `implemented`, `next_review_date: 2026-11-01`), plus the required caveat that the evidence store itself is out of scope — this is an opaque pointer, not a resolved pass/fail record.

### H5 — "NIS2 was updated — which of our Policies are now potentially out of date?" (rubric) — real supersession now exists
```cypher
MATCH (a:Regulation)-[:SUPERSEDED_BY]->(b:Regulation) RETURN a.id, b.id
```
**Golden supersession:** `HELVEX-SOP-1.0 -[:SUPERSEDED_BY]-> HELVEX-SOP-2.0` (no NIS2 version supersession exists yet — the question's NIS2 premise is still hypothetical, but the mechanism now has *a* real supersession edge plus a real stale-Policy signal to reason about).
**Policy staleness signals to cite:** `pol_legacy_asset_personnel_security_policy_7ed6c2` (`deprecated`, all Standards `deprecated`), `pol_clinical_data_integrity_policy_e1a539` (`draft`, v0.3, one Standard also `draft`).
**Rubric — a good answer must:** use the real `SUPERSEDED_BY` edge as the mechanism it'd need to walk for a real NIS2 version update, and correctly flag the deprecated/draft Policies as "potentially out of date" rather than treating all 4 Policies uniformly.

### H6 — "If we adopt a 'Software Bill of Materials' capability..." (rubric) — **premise correction**
An SBOM capability already exists: `cap_component_inventory_sbom_management_b5223c`, required only by CRA's `obl_identify_and_document_components_via_software_bill_of_materials_dcfaae`. It is **not** required by any NIS2 or GDPR obligation today.
**Rubric — a good answer must:** recognize this isn't minting a new node — it's asking whether NIS2/GDPR obligations should newly converge onto the existing capability — and correctly report zero current redundant coverage (no NIS2/GDPR obligation already requires it, so there's nothing to flag as "already redundantly covered" yet).

### H7 — "Which of our automated controls are due for review in the next 30 days?" (set-match) — already ✅, confirmed prior session
```cypher
MATCH (ctrl:Control) WHERE ctrl.next_review_date >= "2026-08-01" AND ctrl.next_review_date <= "2026-08-31" RETURN ctrl.id, ctrl.title, ctrl.next_review_date
```
**Golden:** exactly 2 — `ctrl_std_pol_data_protection_security_policy_8e4c18_v1_automated` (Encryption-at-Rest, `2026-08-15`), `ctrl_std_pol_data_protection_security_policy_8e4c18_v2_automated` (Access Control & MFA, `2026-08-25`). Excludes: `2026-07-20` (overdue, before the window — a correct mechanism must not lump this in), `2026-11-01` (not due soon), `2026-01-01` (deprecated control, stale).

**Note on the date anchor:** per `synthetic-data-spec.md`, the fixture is authored as-of `2026-08-01`, not wall-clock today (`2026-08-06` as of this doc). A query mechanism needs that anchor supplied explicitly (e.g. in a system prompt) rather than computing "next 30 days" from its own clock — otherwise this golden answer silently drifts as real time passes.
