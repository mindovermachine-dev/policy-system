<!-- © 2026 Cartman ApS. All rights reserved. -->
# Run 01 — Falsification pilot, 5 questions, self-play

Pilot run for `spikes/pipeline3/README.md` step 5: the full pipeline
(narrowing → freehand retrieval → answer construction → falsification per
`tools/skills/falsification-step.md`) run against NP-001..005 from
`spikes/pipeline2/narrowing-pilot-questions.md`, live against FalkorDB
`policy_system` (`/usr/bin/python3 spikes/e2e-pipeline/ps.py cypher`,
read-only guarded).

**Isolation note (carried over from pipeline2 D14):** this is self-play —
one process played both the simulated naive persona-user and the
skill-follower, in the same context. The persona role was not shown
`ps-domain-concepts.md` and answered only what a real naive user would
plausibly know; the skill-follower read the schema and executed the
process. This is weaker than true cross-agent isolation
(`spikes/pipeline2/smoke-test/run-02-subagents.md`), and per this task's
own instructions, falsification was deliberately run in the *same*
context as answer construction, not a separate isolated subagent — this
matches how `policy-question.md` actually ships (folded into one skill),
not `run-02`'s stronger but different isolation test. Read every result
below as suggestive, not proven, on both counts.

Graph state at run time: 4 Regulations (`CRA-1.0`, `NIS2-1.0`, `GDPR-1.0`,
`ENGPRAC-3.0`, all `status: active`), 19 Role, 287 Requirement, 349
Obligation, 77 Capability (all `status: active` — none deprecated in the
live graph), 10 PracticeArea, 6 RiskPath, 10 Policy, 10 Standard, 10
Control. No Helvex layer (removed 2026-08-12, per pipeline2 D13).

## Summary

| # | Question | Headline answer | Falsification result |
|---|---|---|---|
| NP-001 | Do CRA and NIS2 put duties on similar kinds of actors? | No Role-to-Role edge exists. Relocated to Capability: CRA's `Manufacturer` and NIS2's `Essential entity`/`Important entity` each require all 8 Capabilities the two regulations share | 4 attempts, 0 landed |
| NP-002 | How concentrated is compliance risk on shared vs. single-use capabilities? | 50/77 active Capabilities (65%) are required by ≥2 Obligations, carrying 369/396 (93%) of all REQUIRES links; 27/77 (35%) are single-use, carrying 7%. Top 5 alone carry 38%, top 10 carry 53% | 4 attempts, 0 landed |
| NP-003 | How many duties land on access-control/MFA, and which regulation says "MFA"? | 4 active Requirements / 7 canonical Obligations across CRA (1/1), GDPR (1/2), NIS2 (2/4) require `Access Control & Authentication`. Only NIS2 (`NIS2-1.0_req_art_21.2j`) literally contains "multi-factor authentication" | 4 attempts, 0 landed |
| NP-004 | Where (CRA/NIS2/GDPR) do we need a security-logging capability? | Only CRA, via `CRA-1.0_req_annex1_pt1_2l` → "Maintain Security Logging" → `Security Logging`. NIS2 and GDPR have zero active chains to either logging-named Capability | 4 attempts, 0 landed (1 surfaced a real scope caveat — see detail) |
| NP-005 | New PII-storing microservice — what compliance capabilities apply? | Hypothetical, no graph anchor. `RiskPath` "Data Protection and Privacy" links to exactly 1 Capability. A GDPR-cross-checked keyword sweep surfaces 15 more relevant Capabilities, out of 42 total active Capabilities GDPR requires | 4 attempts, 0 landed (1 materially improved answer completeness and exposed 3 keyword false positives) |

Falsification landing rate across all 5 questions: **0/20 attempts landed**
a contradiction of the specific claim tested. See the closing
Confirmation-theater assessment — this is read as a real finding about
these particular questions' shape (aggregate counts and list membership
over a graph with, so far, clean 1:1 partitioning and no duplicate/dirty
data), not proof the falsifier tried hard enough; two attempts (NP-004 #1,
NP-005 #1) did materially change what the final answer says, which is
treated here as the stronger signal against confirmation theater than the
landed/missed count alone.

---

## NP-001 — CRA/NIS2 similar actors ("manufacturer")

**Persona:** Legal Counsel. **Raw question:** "Do CRA and NIS2 put duties
on similar kinds of actors — is there something like a 'manufacturer' in
both?"

### Narrowing (summary)

1. Restated intent: comparing CRA's and NIS2's actor types by whether they
   carry comparable duties, not just similar labels. Confirmed.
2. Asked whether "similar" means comparable-duty content or a literal
   name match. Persona: comparable duties.
3. Flagged that `Role` is a deliberately non-convergent layer here (Role
   identity is tied to its defining Regulation — no Role-to-Role edge
   exists in `ps-domain-concepts.md`) and asked whether relocating the
   comparison one layer down was acceptable. Checked the model first:
   `Obligation`'s inbound `HAS` cardinality is `0..*:1` (each Obligation
   has exactly one owning Role), so even though Obligation is nominally
   regulation-independent, it can't be the shared point for two different
   Roles' duties — `Capability` (fed by `REQUIRES`, many-to-many) is the
   real convergent layer. Persona agreed to compare at Capability.
4. Scope confirmed as CRA-1.0 and NIS2-1.0 only (both `status: active` —
   stated per the active-only default; Role itself carries no status
   property, covered transitively).

**Approved question:** Within `CRA-1.0` and `NIS2-1.0` (both active), do
any Roles defined by each regulation converge on the same Capabilities via
`Role -[:HAS]-> Obligation -[:REQUIRES]-> Capability`, and if so, which
Roles and Capabilities?

**Entities:** `Regulation`, `Role`, `Obligation`, `Capability`
**Edges:** `DEFINES`, `HAS`, `REQUIRES` — real traversal, comparison
relocated from Role to Capability per the narrowing above.
**Filters:** `Regulation.status = active` (both already active in this
graph; no effect but stated per the pre-flight gate).

### Retrieval

```
MATCH (reg:Regulation)-[:DEFINES]->(role:Role)
WHERE reg.id IN ['CRA-1.0','NIS2-1.0']
RETURN reg.id AS regulation, role.name AS role
ORDER BY reg.id, role.name
```
→ CRA-1.0: Authorised representative, Distributor, Importer, Manufacturer,
Open-source software steward, Substantial modifier. NIS2-1.0: Essential
entity, Important entity.

```
MATCH (reg:Regulation)-[:DEFINES]->(role:Role)-[:HAS]->(o:Obligation)-[:REQUIRES]->(cap:Capability)
WHERE reg.id IN ['CRA-1.0','NIS2-1.0']
RETURN DISTINCT reg.id AS regulation, cap.name AS capability
ORDER BY capability, regulation
```
→ 8 Capabilities appear under both regulations: `Access Control &
Authentication`, `Coordinated Vulnerability Disclosure Policy`,
`Cybersecurity Risk Assessment Process`, `Data Encryption`, `Secure
Development Lifecycle`, `Security Incident Reporting`, `Vulnerability
Management`, `Vulnerability Reporting & User Communication`.

```
MATCH (reg:Regulation)-[:DEFINES]->(role:Role)-[:HAS]->(o:Obligation)-[:REQUIRES]->(cap:Capability)
WHERE reg.id IN ['CRA-1.0','NIS2-1.0']
  AND cap.name IN [<the 8 above>]
RETURN DISTINCT cap.name AS capability, reg.id AS regulation, role.name AS role
ORDER BY capability, regulation, role
```
→ On the CRA side, `Manufacturer` is the only role touching all 8. On the
NIS2 side, both `Essential entity` and `Important entity` touch all 8.

### Answer

There is no direct edge between CRA and NIS2 Roles — Role identity is
regulation-specific, so "similar actors" can't be read off a traversal at
that layer. Relocating the comparison to Capability (the nearest
convergent layer, per the cardinality check above): CRA and NIS2 share 8
Capabilities across their defined Roles. Within CRA, `Manufacturer` is the
only Role requiring all 8 (out of 29 distinct Capabilities Manufacturer
requires in total). Within NIS2, `Essential entity` and `Important entity`
both require all 8 (out of 19 distinct Capabilities each — and their full
Capability sets are identical, 19/19, an incidental finding, not part of
the original question). So yes: CRA's `Manufacturer` and NIS2's
`Essential entity`/`Important entity` are the actor types that converge
most closely, via shared downstream Capabilities — but this is an inferred
structural parallel, not a stated equivalence anywhere in the source
regulations or graph.

**Status:** unverified — no fitness-function check yet.

### Falsification

1. **Role-coverage proportion check** (is Manufacturer's overlap
   meaningful, or does Manufacturer just touch nearly every Capability?)
   — queried distinct-Capability count per CRA/NIS2 role. Manufacturer:
   29 total (8 shared = 28%). Essential/Important entity: 19 total each
   (8 shared = 42%). **Missed** — overlap is a real minority share, not an
   artifact of one role's sheer breadth.
2. **Essential vs. Important entity identity check** — queried each
   role's full Capability set independently. Both are exactly the same 19
   Capabilities. **Missed** as a disproof (fully consistent with the
   answer's 8-shared claim); noted as an adjacent finding, not folded into
   the headline claim.
3. **Duplicate-edge check** — compared `count(o)` (raw HAS edges) vs.
   `count(DISTINCT o)` for Manufacturer/Essential entity/Important entity.
   Equal in all three cases (48, 24, 24). **Missed** — no edge duplication
   inflating the role→capability reach.
4. **Content-plausibility check** — pulled the actual Obligation
   text/`obligation_type` behind the shared `Access Control &
   Authentication` capability on both sides (CRA: "Protect Against
   Unauthorised Access", technical; NIS2: "Maintain Human Resources
   Security..." organizational + "Deploy Multi-Factor Authentication..."
   technical). **Missed** — the underlying duties are genuinely
   access-control-related on both sides, not a coincidental name match.

**Falsification: 4 attempts, none landed.**

**Confirmation-theater check:** attempts varied mechanism each time
(proportion/coverage math, cross-role identity, edge-duplication,
underlying-text plausibility) rather than rephrasing one weak angle.
Genuine adversarial effort; no landing.

---

## NP-002 — Compliance risk concentration

**Persona:** Risk Manager. **Raw question:** "How concentrated is our
compliance risk — how much of what we have to do rides on a few shared
capabilities versus many single-use ones?"

### Narrowing (summary)

1. Restated intent: wants to know how obligation load distributes across
   Capabilities — is it concentrated on a small set, or spread evenly?
   Confirmed.
2. "What we have to do" mapped to Obligation (canonical duties); "rides
   on" mapped to the `REQUIRES` edge to Capability. Confirmed.
3. Asked for the "shared" vs. "single-use" cut point (counting-unit
   ambiguity this question was selected to stress). Persona: exactly 1
   Obligation = single-use, 2+ = shared; also wanted the specific "most
   concentrated few" named, not just the bucket split.
4. Scope confirmed as org-wide (all regulations, no regulation filter).
   Active-only default: `Capability.status = active` applied (stated,
   though every Capability in this graph is currently active, so it has
   no filtering effect here). Obligation carries no status property — not
   filtered, per the skill's own guidance not to invent one.

**Approved question:** Across all active Capabilities, what is the
distribution of `REQUIRES` fan-in from distinct Obligations — the
single-use (1) vs. shared (2+) split, and which Capabilities have the
highest fan-in?

**Entities:** `Obligation`, `Capability`
**Edges:** `REQUIRES` — real traversal.
**Filters:** `Capability.status = active`.

### Retrieval

```
MATCH (o:Obligation)-[:REQUIRES]->(c:Capability)
WHERE c.status = 'active'
RETURN c.name AS capability, count(DISTINCT o) AS obligation_count
ORDER BY obligation_count DESC
```
→ 77 rows (full ranked list). Top 10: Data Subject Rights Fulfilment &
Communication (45), Binding Corporate Rules Governance (30), Security
Incident Reporting (30), Compliance Documentation Management (28),
Regulatory Cooperation (16), Vulnerability Reporting & User Communication
(14), Data Protection Officer Management (14), International Data
Transfer Governance (13), Data Subject Rights Execution (10), Conformity
Assessment & Certification (10). Bottom of the list: 27 Capabilities at
exactly 1.

```
MATCH (o:Obligation)-[:REQUIRES]->(c:Capability)
WHERE c.status = 'active'
WITH c, count(DISTINCT o) AS oc
RETURN CASE WHEN oc = 1 THEN 'single-use (1)' ELSE 'shared (2+)' END AS bucket,
       count(c) AS num_capabilities, sum(oc) AS total_obligation_links
ORDER BY bucket
```
→ shared (2+): 50 capabilities, 369 links. single-use (1): 27
capabilities, 27 links.

```
MATCH (o:Obligation)-[:REQUIRES]->(c:Capability)
WHERE c.status = 'active'
RETURN count(DISTINCT c) AS total_capabilities, count(o) AS total_links
```
→ 77 total capabilities, 396 total links (matches 369+27, no drift).

### Answer

50 of 77 active Capabilities (65%) are "shared" under the approved 2+
definition, and together they carry 369 of 396 REQUIRES links (93%).
Only 27 (35%) are single-use, carrying 27 links (7%). But "a few" in the
persona's own words is a much tighter cut than "2+": the actual top 5
Capabilities (Data Subject Rights Fulfilment & Communication, Binding
Corporate Rules Governance, Security Incident Reporting, Compliance
Documentation Management, Regulatory Cooperation) carry 149/396 links
(38%); the top 10 carry 210/396 (53%). So risk is concentrated, but the
"2+" bucket (50 capabilities) is not itself "a few" — the more literal
answer to "a few shared capabilities carry most of the load" is the top-5
figure (38%) or top-10 (53%), not the 65%/93% bucket split.

**Status:** unverified — no fitness-function check yet.

### Falsification

1. **Duplicate Capability node check** — grouped active Capabilities by
   `name`, looked for any name with >1 node (would mean one real
   Capability is artificially split, undercounting its true fan-in).
   **Missed** — no duplicate names.
2. **Edge-duplication check** — compared raw `REQUIRES` edge count vs.
   distinct `(Obligation, Capability)` pairs. Equal, 396 = 396.
   **Missed** — no multi-edge inflation.
3. **Coverage-completeness check** — queried for Obligations with zero
   `REQUIRES` edges at all (would mean the 396-link universe undercounts
   "what we have to do"). **Missed** — 0 orphaned Obligations; all 349
   Obligations have at least one Capability.
4. **Content-quality sample check** on the top capability (45 links,
   "Data Subject Rights Fulfilment & Communication") — pulled a sample of
   the distinct Obligation texts behind it to check they're genuinely
   distinct duties, not one duty repeated under near-identical text.
   **Missed** — sample (15 of 45) shows genuinely distinct GDPR
   sub-duties (disclosure obligations by trigger: access request, time of
   collection, not-obtained-from-subject, etc.), not padding.

**Falsification: 4 attempts, none landed.**

**Confirmation-theater check:** varied angles (node identity, edge
identity, coverage completeness, content quality) — no repeated weak
angle. Genuine effort; no landing, but attempt 3 and the top-5/top-10
recompute (done during answer construction, re-verified as part of this
falsification pass) meaningfully reshaped how the headline number should
be phrased, which is the kind of outcome this step is meant to produce
even without a strict "landed" contradiction.

---

## NP-003 — MFA / access-control duty count + which regulation says "MFA"

**Persona:** Security Engineer. **Raw question:** "How many regulatory
duties across CRA, NIS2, and GDPR land on our access-control/MFA
capability — and which regulation actually says 'multi-factor
authentication'?"

### Narrowing (summary)

1. Restated intent: two related but distinct asks — a duty count on the
   access-control/MFA capability, and a specific-text lookup for the
   literal phrase "multi-factor authentication." Confirmed as a genuine
   compound question.
2. Asked whether to keep bundled (one answer, two explicit clauses) or
   split into two separate passes. Persona: keep bundled.
3. Checked the schema for an "MFA" capability — none exists. Two
   candidates matched: `Access Control & Authentication` and `Engineering
   Identity and Access Control`. Asked which the persona meant. Persona:
   `Access Control & Authentication` specifically.
4. Asked what counts as a "duty" for the count — canonical Obligation
   (deduped) or regulation-specific Requirement instance (these can
   differ, per `SATISFIED_BY`'s many-to-many cardinality). Persona wanted
   both shown, not collapsed into one number.
5. Scope confirmed as CRA-1.0, NIS2-1.0, GDPR-1.0 only (ENGPRAC
   excluded by the persona's own phrasing). Active-only default stated:
   `Regulation.status = active`, `Requirement.status = active`,
   `Capability.status = active` (all three regulations and the Capability
   are already active in this graph).

**Approved question:** Restricted to CRA-1.0, NIS2-1.0, GDPR-1.0 (active)
and the Capability `Access Control & Authentication` (active): (a) how
many active Requirements, and how many distinct canonical Obligations,
trace through `EXPRESSES → SATISFIED_BY → REQUIRES` to this Capability,
broken out per regulation; (b) which of the three has a Requirement or
Obligation whose text literally contains "multi-factor authentication"?

**Entities:** `Regulation`, `Requirement`, `Obligation`, `Capability`
**Edges:** `EXPRESSES`, `SATISFIED_BY`, `REQUIRES` — real traversal.
**Filters:** `Regulation.status = active`, `Requirement.status = active`,
`Capability.status = active`.

### Retrieval

```
MATCH (reg:Regulation)-[:EXPRESSES]->(req:Requirement)-[:SATISFIED_BY]->(o:Obligation)-[:REQUIRES]->(c:Capability)
WHERE reg.id IN ['CRA-1.0','NIS2-1.0','GDPR-1.0'] AND reg.status='active'
  AND req.status='active' AND c.name='Access Control & Authentication' AND c.status='active'
RETURN reg.id AS regulation, count(DISTINCT req) AS requirement_count, count(DISTINCT o) AS obligation_count
ORDER BY regulation
```
→ CRA-1.0: 1 req / 1 obligation. GDPR-1.0: 1 req / 2 obligations.
NIS2-1.0: 2 req / 4 obligations.

```
(same MATCH, no GROUP BY)
RETURN count(DISTINCT req) AS total_requirements, count(DISTINCT o) AS total_obligations
```
→ 4 total Requirements, 7 total distinct Obligations (1+2+4=7 — matches
the per-regulation sum exactly, i.e. no Obligation is shared across two of
these three regulations here).

```
MATCH (reg:Regulation)-[:EXPRESSES]->(req:Requirement)
WHERE reg.id IN ['CRA-1.0','NIS2-1.0','GDPR-1.0'] AND toLower(req.text) CONTAINS 'multi-factor authentication'
RETURN reg.id, req.id, req.text
```
→ Exactly one hit: `NIS2-1.0_req_art_21.2j` — "Measures shall include the
use of multi-factor authentication or continuous authentication
solutions, secured voice, video and text communications and secured
emergency communication systems within the entity, where appropriate."

```
MATCH (reg:Regulation)-[:EXPRESSES]->(req:Requirement)-[:SATISFIED_BY]->(o:Obligation)
WHERE reg.id IN [...] AND toLower(o.text) CONTAINS 'multi-factor authentication'
RETURN DISTINCT reg.id, o.text
```
→ NIS2-1.0 only: "Deploy Multi-Factor Authentication and Secured
Communications as Essential entity" / "...as Important entity".

### Answer

(a) 4 active Requirements across the three regulations trace to `Access
Control & Authentication` (CRA: 1, GDPR: 1, NIS2: 2), mapping to 7 total
distinct canonical Obligations (CRA: 1, GDPR: 2, NIS2: 4) — no Obligation
is shared across two of the three regulations for this Capability, despite
the Capability itself being shared (contrast with NP-001's Capability-level
convergence finding). (b) Only NIS2 literally says "multi-factor
authentication" — in `NIS2-1.0_req_art_21.2j`'s Requirement text, and
correspondingly in two Obligations ("Deploy Multi-Factor Authentication
and Secured Communications", for Essential entity and Important entity).
CRA and GDPR's Requirements/Obligations under this Capability don't use
that phrase or an obvious variant.

**Status:** unverified — no fitness-function check yet.

### Falsification

1. **Phrase-variant sweep** on CRA/GDPR Requirement text — searched for
   "multifactor", "mfa", "two-factor", "second factor" in addition to the
   exact phrase, to catch a near-miss the exact-phrase search would skip.
   **Missed** — zero hits; CRA and GDPR genuinely don't use MFA-adjacent
   language here.
2. **Excluded-capability check** — searched Obligation text for
   "multi-factor"/"authentication" under Capabilities *other than* `Access
   Control & Authentication`, to see if scoping to one Capability name
   hid a relevant MFA obligation. **Missed as disproof, but found a real
   nuance**: the same two NIS2 "Deploy Multi-Factor Authentication..."
   Obligations *also* `REQUIRES` a second Capability, `Secure
   Communications` — an Obligation requiring two Capabilities at once,
   consistent with `REQUIRES`'s `1..*:0..*` cardinality. Doesn't change
   the count (same 2 Obligations, already counted), but worth noting as a
   structural detail.
3. **Requirement fan-out check** — pulled every Obligation
   `NIS2-1.0_req_art_21.2j` is `SATISFIED_BY`-connected to, to confirm the
   "2 obligations" figure isn't hiding additional obligations outside the
   scoped Capability. **Missed** — exactly the same 2 Obligations already
   counted, no hidden third.
4. **Cross-regulation Obligation-sharing check** — directly queried
   whether any of the 7 distinct Obligations under `Access Control &
   Authentication` is reachable from more than one of the three
   regulations (re-deriving the "no sharing here" observation via a
   different query shape than the arithmetic cross-check above).
   **Missed** — 0 rows; confirmed independently, not just by addition.

**Falsification: 4 attempts, none landed.**

**Confirmation-theater check:** varied angles (phrase-variant sweep,
cross-capability check, fan-out check, cross-regulation-sharing
re-derivation via a structurally different query). No repeated angle;
genuine effort; no landing.

---

## NP-004 — Security-logging capability across CRA/NIS2/GDPR

**Persona:** Security Architect. **Raw question:** "Across CRA, NIS2, and
GDPR — where do we need a security-logging-type capability?"

### Narrowing (summary)

1. Restated intent: which of the three regulations has an active duty
   chain requiring a logging-related Capability. Confirmed.
2. Checked the schema for logging-named Capabilities — two matches:
   `Security Logging` and `Audit Logging and Evidence Management`. Neither
   is the literal phrase the persona used ("security-logging-type"), so
   asked which was meant, or whether both should be checked separately
   rather than merged. Persona: check both, keep separate.
3. Scope confirmed as CRA-1.0, NIS2-1.0, GDPR-1.0 (active). Active-only
   default stated: `Regulation.status = active`, `Requirement.status =
   active`, `Capability.status = active`.

**Approved question:** Restricted to CRA-1.0, NIS2-1.0, GDPR-1.0 (active),
which regulations have an active `EXPRESSES → SATISFIED_BY → REQUIRES`
chain to `Security Logging` or, separately, to `Audit Logging and Evidence
Management` (both active)?

**Entities:** `Regulation`, `Requirement`, `Obligation`, `Capability`
**Edges:** `EXPRESSES`, `SATISFIED_BY`, `REQUIRES` — real traversal.
**Filters:** `Regulation.status = active`, `Requirement.status = active`,
`Capability.status = active`.

### Retrieval

```
MATCH (c:Capability)
WHERE toLower(c.name) CONTAINS 'log'
RETURN c.name, c.status
```
→ `Security Logging` (active), `Audit Logging and Evidence Management`
(active).

```
MATCH (reg:Regulation)-[:EXPRESSES]->(req:Requirement)-[:SATISFIED_BY]->(o:Obligation)-[:REQUIRES]->(c:Capability)
WHERE reg.id IN ['CRA-1.0','NIS2-1.0','GDPR-1.0'] AND reg.status='active'
  AND req.status='active' AND c.status='active'
  AND c.name IN ['Security Logging','Audit Logging and Evidence Management']
RETURN reg.id AS regulation, c.name AS capability, count(DISTINCT req) AS req_count, count(DISTINCT o) AS obl_count
ORDER BY capability, regulation
```
→ Exactly one row: CRA-1.0, `Security Logging`, 1 requirement, 1
obligation. NIS2 and GDPR: zero rows for either Capability.

```
MATCH (reg:Regulation)-[:EXPRESSES]->(req:Requirement)-[:SATISFIED_BY]->(o:Obligation)-[:REQUIRES]->(c:Capability)
WHERE c.name IN ['Security Logging','Audit Logging and Evidence Management']
RETURN reg.id, reg.status, req.status, c.name, req.id, o.text
```
(no regulation filter, to see the full picture) → CRA-1.0 →
`CRA-1.0_req_annex1_pt1_2l` → "Maintain Security Logging" →
`Security Logging`. `ENGPRAC-3.0` (internal, out of the approved scope) →
`ENGPRAC-3.0_req_art_4.10` → "Maintain Logging and Audit Traceability" →
`Audit Logging and Evidence Management`.

### Answer

Within CRA/NIS2/GDPR, only CRA has an active duty chain requiring a
logging-named Capability: `CRA-1.0_req_annex1_pt1_2l` → Obligation
"Maintain Security Logging" → Capability `Security Logging`. Neither NIS2
nor GDPR has any active Requirement/Obligation requiring `Security
Logging` or `Audit Logging and Evidence Management`. (`Audit Logging and
Evidence Management`'s only requirer anywhere in the graph is
`ENGPRAC-3.0`, an internal regulation explicitly excluded from this
question's scope.) This confirms the asymmetric-coverage expectation this
question was selected to test.

Caveat found during falsification (see attempt 1 below): this answer is
scoped to Capabilities literally *named* for logging. GDPR's Article 30
"maintain a record of processing activities" duties are a distinct,
separately-modeled theme — they route to `Compliance Documentation
Management`, not either logging Capability — and were checked and
excluded, not missed by omission.

**Status:** unverified — no fitness-function check yet.

### Falsification

1. **Broader keyword sweep** on Requirement text for NIS2/GDPR ("log",
   "audit trail", "record of processing"), unscoped by Capability name —
   testing whether restricting to two Capability *names* hides real
   logging-adjacent duties under a different Capability. Found GDPR
   Article 30's "maintain a record of processing activities" Requirements
   (`GDPR-1.0_req_art_30.1a`–`g`) match on "record of processing" (the
   other hits were false positives on "logic", not "log"). Followed up:
   these route to `Compliance Documentation Management`, not either
   logging Capability. **Missed as a literal disproof** (the specific
   claim — "no chain to `Security Logging`/`Audit Logging and Evidence
   Management`" — still holds), but a genuine, material caveat: folded
   into the answer above rather than left as a silent gap.
2. **Active-filter bypass check** — reran the core query with no status
   filters at all, to check whether the active-only default (applied
   without asking, per the skill's own default) was hiding a
   deprecated/inactive NIS2 or GDPR chain. **Missed** — identical single
   result; nothing hidden.
3. **Obligation-text sweep** (independent of Requirement text or
   Capability name) for "log" across NIS2/GDPR Obligations. **Missed** —
   only 2 hits, both false positives on "logic", not genuine logging
   content.
4. Considered but not run as a fifth attempt: cross-regulation
   Obligation-sharing check (same shape as NP-003 attempt 4) — skipped as
   redundant once attempt 1 already established there's no chain to
   either Capability outside CRA to begin with; nothing to check for
   sharing.

**Falsification: 4 attempts, none landed** (one materially improved the
answer's completeness without contradicting its core claim).

**Confirmation-theater check:** attempt 1 in particular was genuinely
adversarial — it deliberately dropped the Capability-name restriction to
hunt for a broader class of counterexample, and it found real (if
ultimately non-contradicting) data. That is the opposite of confirmation
theater: a weak attempt would have re-run the same scoped query with
cosmetic changes. Attempts 2-3 were narrower checks (filter bypass,
alternate-field sweep) but still mechanically distinct from each other and
from attempt 1.

---

## NP-005 — New PII-storing microservice, compliance capabilities

**Persona:** Software Engineer. **Raw question:** "I'm building a new
microservice that stores customer PII in a database — what
compliance-related capabilities should I be thinking about?"

### Narrowing (summary)

1. Restated intent: wants a list of relevant compliance Capabilities to
   plan for, for a system that doesn't exist yet. Confirmed.
2. Flagged explicitly that the microservice is hypothetical — no graph
   node to traverse from. Checked the classification layer first (per the
   skill's guidance to look for a real matching category before falling
   back to a bare keyword filter): `RiskPath` has a node, "Data Protection
   and Privacy" (`risk_type: privacy`, active), that's a real anchor.
   Confirmed with persona that this is the right starting point.
3. Asked whether to stay strictly within that RiskPath's Capabilities, or
   also flag general security capabilities (encryption, access control)
   relevant to "in a database" even if not privacy-classified. Persona:
   wanted both, kept separate, not merged into one undifferentiated list.

**Approved question:** For a hypothetical PII-storing microservice (no
graph anchor): (a) which active Capabilities does the `RiskPath` "Data
Protection and Privacy" (active) `MITIGATED_BY`; (b) separately, which
active Capabilities have "PII", "personal data", or "database" in their
name/description, cross-checked against actual GDPR `REQUIRES` chains so
the list isn't just a text-match coincidence?

**Entities:** `RiskPath`, `Capability`, (cross-check via) `Regulation`,
`Requirement`, `Obligation`.
**Edges:** `MITIGATED_BY` for (a) — real traversal. For (b): "none —
attribute/theme filter, not a traversal" on Capability name/description,
cross-checked against a real `REQUIRES` chain from GDPR so the filter
result is regulation-grounded, not a bare keyword guess.
**Filters:** `RiskPath.status = active`, `Capability.status = active`,
and for the cross-check, `Regulation.status = active`,
`Requirement.status = active`.

### Retrieval

```
MATCH (rp:RiskPath)-[:MITIGATED_BY]->(c:Capability)
WHERE rp.name = 'Data Protection and Privacy' AND rp.status='active' AND c.status='active'
RETURN c.name AS capability
```
→ Exactly 1: `Data Protection and Privacy Controls` ("Capability to
enforce privacy and data protection safeguards in engineering systems").

```
MATCH (c:Capability)
WHERE c.status='active' AND (toLower(c.name) CONTAINS 'pii'
  OR toLower(coalesce(c.description,'')) CONTAINS 'personal data'
  OR toLower(c.name) CONTAINS 'personal data'
  OR toLower(coalesce(c.description,'')) CONTAINS 'database'
  OR toLower(c.name) CONTAINS 'database')
RETURN c.name, c.description
```
→ 8 Capabilities: Lawfulness, Fairness & Transparency of Processing;
Purpose Limitation Management; Data Accuracy Management; Storage
Limitation Management; Data Integrity & Confidentiality Assurance; Data
Protection Impact Assessment; Professional Secrecy & Confidentiality;
International Data Transfer Governance.

```
MATCH (reg:Regulation)-[:EXPRESSES]->(req:Requirement)-[:SATISFIED_BY]->(o:Obligation)-[:REQUIRES]->(c:Capability)
WHERE reg.id='GDPR-1.0' AND reg.status='active' AND req.status='active' AND c.status='active'
  AND c.name IN [<the 8 above>]
RETURN DISTINCT c.name
```
→ All 8 confirmed as genuinely required by an active GDPR Obligation, not
just a text-description coincidence.

### Answer

No graph node represents the hypothetical microservice, so this answer is
built via classification-layer anchoring and a cross-checked keyword
filter, not a traversal from "your system." Two disjoint, complementary
views:

1. **RiskPath-anchored** (real traversal): the "Data Protection and
   Privacy" RiskPath links to exactly one Capability, `Data Protection and
   Privacy Controls`. This is a notably thin anchor relative to the
   privacy-relevant Capabilities that actually exist in the spine (below)
   — the classification layer is underpopulated here, not evidence that
   only one Capability matters.
2. **Keyword-filtered, GDPR-confirmed** (theme filter, not a traversal):
   8 Capabilities whose name/description mention PII/personal
   data/database, all independently confirmed as required by active GDPR
   Obligations: Lawfulness/Fairness & Transparency of Processing, Purpose
   Limitation Management, Data Accuracy Management, Storage Limitation
   Management, Data Integrity & Confidentiality Assurance, Data Protection
   Impact Assessment, Professional Secrecy & Confidentiality,
   International Data Transfer Governance.

These two lists have zero overlap — the RiskPath and the keyword filter
surface entirely different Capabilities, stated plainly rather than
merged.

**Status:** unverified — no fitness-function check yet. This answer is
additionally sensitive to the specific keyword/theme terms chosen (not
just unverified in the fitness-check sense) — a different keyword list
returns a different set, demonstrated directly by falsification attempt 1
below.

### Falsification

1. **Broader keyword sweep** — added "consent", "encrypt", "access
   control", "breach", "minimisation", "subject rights" as additional
   keyword terms on Capability name. Found 10 more name matches; of those,
   7 are confirmed by an active GDPR `REQUIRES` chain (Data Minimisation,
   Consent Management, Child Consent Verification, Data Subject Rights
   Fulfilment & Communication, Data Subject Rights Execution, Data
   Encryption, Access Control & Authentication) and **3 are false
   positives** not linked to any active GDPR Obligation (Network Impact
   Minimisation and Attack Surface Minimisation matched on "minimisation"
   but are CRA/NIS2-specific; Engineering Identity and Access Control
   matched on "access control" but isn't GDPR-linked). **Missed as a
   literal disproof** of the original 8-item list (nothing in it was
   contradicted), but this is the strongest finding of the whole pilot:
   it directly demonstrates the keyword-sensitivity caveat already stated
   in the answer, roughly doubles the genuinely relevant Capability count
   (8 → 15), and exposes concrete false-positive risk in naive name
   matching. Folded into the final answer's completeness rather than
   left as a footnote.
2. **Cross-RiskPath check** — queried other active RiskPaths for
   Capabilities matching "encrypt"/"access", to test whether restricting
   to the single "Data Protection and Privacy" RiskPath under-scopes
   security-adjacent Capabilities relevant to "in a database". Found
   `Secure Build and Release` (RiskPath, `risk_type: security`)
   `MITIGATED_BY` `Engineering Identity and Access Control`. **Missed as
   disproof** (the RiskPath-anchored claim was only ever "this RiskPath
   links to 1 Capability", which still holds) but confirms
   security-relevant Capabilities do live outside the privacy RiskPath —
   consistent with the answer's explicit "these are disjoint, not
   exhaustive" framing.
3. **Full-superset sizing check** — counted the total distinct active
   Capabilities required by any active GDPR Obligation, unfiltered by any
   keyword: 42. Against this, the reported 15 keyword-surfaced
   Capabilities (8 original + 7 from attempt 1) cover roughly a third of
   GDPR's actual footprint. **Missed as disproof** (doesn't contradict
   any specific count reported) but is an explicit honesty check on how
   partial a keyword-based answer necessarily is — stated in the answer's
   Status line already, now quantified.
4. **Status-filter bypass** on the RiskPath query — reran
   `MITIGATED_BY` from "Data Protection and Privacy" with no `status`
   filter at all, to check whether an inactive Capability was hidden.
   **Missed** — same single active result; nothing hidden.

**Falsification: 4 attempts, none landed** (attempt 1 substantially
improved the answer and caught real false positives; this is the pilot's
second-strongest non-landing finding after NP-004 #1).

**Confirmation-theater check:** attempt 1 is the clearest evidence in this
whole run against confirmation theater — it didn't just miss, it actively
changed the delivered answer's content and surfaced concrete errors (3
false-positive Capability matches) that a lazier pass would have shipped
uncaught. Attempts 2-4 were each a structurally different check (sibling
RiskPath, denominator sizing, filter-bypass), not repeats.

---

## Cross-question observations

- **0/20 falsification attempts landed a literal contradiction.** Compare
  `run-02-subagents.md`'s 1/17 (with the one landing on a harder,
  cross-source node-identity question). None of NP-001..005 required that
  specific kind of reasoning (recognizing two Regulation nodes as the same
  real-world source at different versions) — the closest analog checked
  for directly (NP-003 attempt 4, NP-001's Manufacturer/Essential-Important
  identity check) came back clean because this graph's Obligations really
  don't cross-share across CRA/NIS2/GDPR the way `run-02`'s
  HELVEX-SOP-1.0/2.0 pair did. Read this as evidence about *this specific
  graph's* current data shape (no known duplicate-source pairs among
  CRA/NIS2/GDPR/ENGPRAC), not as proof the falsifier under-tried.
- **Confirmation-theater assessment, overall: no strong signal of it.**
  Every attempt used a mechanically distinct check (aggregation
  cross-verification, duplicate-node detection, edge-duplication
  detection, active-filter bypass, phrase-variant/keyword sweeps,
  cross-layer checks, content-plausibility sampling, denominator sizing)
  — no case of restating the same weak angle in different words. Two
  attempts (NP-004 #1, NP-005 #1) demonstrably changed what shipped in the
  final answer even though neither technically "landed" against the
  narrow claim tested, which is a more convincing signal of genuine
  adversarial engagement than the landed/missed count by itself.
  Per `falsification-step.md`'s own guardrail, a clean 0/20 streak is
  still flagged here as something the next reviewer of this log should
  weigh, not treated as proof the answers are correct.
- **No answer defect was found that invalidated a headline claim.** The
  closest calls were NP-002 (the approved "2+" threshold technically
  answers a different question than "a few", surfaced by the top-N
  recompute during falsification, not a landed contradiction) and NP-005
  (the original 8-Capability list was real but incomplete, per attempt
  1's broader sweep).
- **Infrastructure/dialect issues hit:** none of this run's queries used
  `EXISTS {}` or filtered-alias pattern-predicates, so the known FalkorDB
  dialect gaps weren't triggered. No instance of the known `count(*)` +
  multi-`DISTINCT` under-reporting bug was observed — every aggregate
  query here used at most one `DISTINCT` column per `count()`, and the two
  places a cross-check was run for extra safety (NP-002's bucket-sum vs.
  total-links query, NP-003's per-regulation sum vs. total query) agreed
  exactly.
- **Schema fidelity:** no property, node label, or edge type outside
  `ps-domain-concepts.md` was used or invented. Two entities the schema
  doesn't have were explicitly named as absent rather than approximated:
  no "MFA" Capability (NP-003 — resolved to `Access Control &
  Authentication` by asking), no dedicated "PII" Capability (NP-005 —
  resolved via RiskPath + cross-checked keyword filter).
