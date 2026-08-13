<!-- © 2026 Cartman ApS. All rights reserved. -->
# Run 02 — Harder pilot: 5 natural-hard questions (self-play) + 3 seeded-defect answers (isolated falsifiers)

Follow-up to `run-01-falsification-pilot.md`'s 0/20 clean streak, per `PROGRESS.md`'s
D4 "Next action" item 1. That streak was read as evidence about the graph's
current data shape (no cross-regulation duplicate-source pairs), not proof
the falsifier tried hard enough — this run is designed to separate those two
explanations rather than just running more of the same.

**Design (per this spike's own D5, see PROGRESS.md):** two halves, deliberately
different isolation levels.

1. **NHQ-1..5 (natural-hard, self-play).** Five fresh questions (not reused
   from `run-01`/NP-001..005), each targeting a specific structural trap —
   chosen after directly querying the graph for the traps that made
   `pipeline2/smoke-test/run-02-subagents.md`'s hard tier land (cross-source
   node identity via `SUPERSEDED_BY`) and finding that trap doesn't exist
   here anymore (Helvex removed 2026-08-12) or anywhere else in this graph
   (checked directly: no individually-deprecated Requirement under an active
   Regulation, no deprecated Capability/PracticeArea/RiskPath at all — every
   lifecycle property in this graph is currently at its "active" value). Run
   self-play, same methodology as `run-01`, same D14 isolation caveat
   applies.
2. **SEED-1..3 (seeded-defect, isolated falsifiers).** Because self-play
   can't blindly test whether falsification catches an error the same
   process just planted, three real Q&A pairs were built from genuine
   retrieval, then given one deliberate, realistic construction-step defect
   each (never a doctored *retrieval* — the query and raw data shown are
   real graph output; only the prose answer is wrong). Ground truth was
   logged privately before any falsifier saw the materials. Each was handed
   to a **separate subagent** (Agent tool, no shared context with this
   process or each other) with exactly `falsification-step.md`'s stated
   preconditions — approved question, entities/edges, constructed answer,
   retrieved data/query — and no indication the run contains any seeded
   defects at all, let alone which ones. This is a stronger isolation than
   `run-01`'s self-play, matching `pipeline2/smoke-test/run-02-subagents.md`'s
   design for this part only.

Environment: same as `run-01` — FalkorDB `localhost:6379`, graph
`policy_system`, `/usr/bin/python3 spikes/e2e-pipeline/ps.py cypher`
(read-only guarded). Graph state unchanged since `run-01`: 4 Regulations (all
`active`), 19 Role, 287 Requirement (all `active`), 349 Obligation, 77
Capability (all `active`), 10 PracticeArea (all `active`), 6 RiskPath (all
`active`), 10 Policy (all `approved`), 10 Standard / 10 Control (all
`implemented`/`reviewed`) — confirmed directly this run, not assumed from
`run-01`.

## Summary

| # | Type | Question | Headline answer | Falsification result |
|---|---|---|---|---|
| NHQ-1 | Natural-hard (fan-out trap) | Is there a single duty whose breadth alone creates disproportionate compliance coverage? | 3 Obligations tie at the graph's max fan-out (8 active Capabilities each) — all "assume Manufacturer obligations," on 3 different Roles (Importer, Substantial modifier, Distributor), all requiring the *identical* 8-Capability set, itself a subset of Manufacturer's own 29 | 5 attempts, 0 landed |
| NHQ-2 | Natural-hard (known infra bug, deliberately courted) | Per regulation, how many distinct Roles and Capabilities does it touch? | CRA 6/29, ENGPRAC 6/10, GDPR 5/42, NIS2 2/19 | 5 attempts, 0 landed — the known FalkorDB multi-`DISTINCT` under-reporting bug (`pipeline2/run-02` Q4) did **not** reproduce here despite deliberately courting its shape twice |
| NHQ-3 | Natural-hard (4-way convergence) | Is there a Capability required by all 4 regulations? | No — 4-way intersection is empty. Nearest is a 3-way core (4 Capabilities) shared by exactly CRA/GDPR/NIS2, **never** ENGPRAC; falsification found ENGPRAC shares *zero* Capabilities with any other regulation at any tier | 5 attempts, 0 landed (1 materially strengthened the answer) |
| NHQ-4 | Natural-hard (proxy-metric ambiguity) | Which regulation carries the most exposure? | GDPR > CRA > NIS2 > ENGPRAC, and uniquely among this pilot's ambiguous-framing questions, all 3 independent proxies tried (Requirement volume, Capability breadth, Requirement-per-Capability density) agree on the same ranking | 5 attempts, 0 landed (1 strengthened robustness of the "proxies agree" claim) |
| NHQ-5 | Natural-hard (3-clause compound) | Total Obligations; GDPR's international-transfer chain; any other regulation on the same theme? | 349 total Obligations; GDPR: 8 Requirements / 13 Obligations reach `International Data Transfer Governance`; no other active regulation reaches it (keyword-swept, not just name-scoped) | 5 attempts, 0 landed |
| SEED-1 | Seeded defect (numeric misstatement) | Shared vs. single-use Capability split | Answer stated 52/25; graph's own cited query returns 50/27 | **1 attempt, landed** (isolated falsifier) |
| SEED-2 | Seeded defect (incomplete-retrieval overclaim) | Does any regulation besides CRA require Data Encryption? | Answer claimed no; GDPR-1.0 and NIS2-1.0 both have fully-active chains to the same Capability node | **1 attempt, landed** (isolated falsifier) |
| SEED-3 | Seeded defect (raw-row vs. distinct-count confusion) | How many distinct Requirements reach International Data Transfer Governance? | Answer stated 13 (row count); graph has 8 distinct Requirement nodes on that traversal | **1 attempt, landed** (isolated falsifier) |

**Natural-hard: 25/25 falsification attempts, 0 landed.** **Seeded-defect:
3/3 landed, on the first attempt each**, by isolated subagents with no
knowledge the batch contained any defects. Read together, these two results
answer `run-01`'s open question directly: the 0/20 (now 0/25 across two
rounds) clean streak on real questions is not explained by the falsifier
failing to try — when a real, checkable contradiction exists, it lands
immediately, even under blind isolation. The clean streak on natural
questions is evidence about this graph's current data shape, not about
falsification creativity.

---

## NHQ-1 — Single-obligation fan-out concentration

**Persona:** Compliance Manager. **Raw question:** "Is there a single duty
that, by itself, gives us disproportionate coverage across our compliance
capabilities — something where fixing one obligation would look like solving
many?"

### Narrowing (summary)

1. Restated intent: wants to know if any single Obligation's `REQUIRES`
   fan-out is large enough to create a false sense of broad coverage from
   addressing one duty. Confirmed.
2. Checked the fan-out distribution first (not asked, but needed to know
   what "large" means empirically before proposing a threshold): 327
   Obligations require exactly 1 Capability, down to 3 Obligations tied at
   the graph's max, 8. Persona confirmed: report the max-tier, not an
   arbitrary threshold.
3. Filters: `Capability.status = active` (default). Obligation carries no
   status. Scope: org-wide, no regulation filter — Obligation is
   regulation-independent by design, so restricting to one regulation would
   misrepresent what "one duty" means here.

**Approved question:** Across all Obligations, which have the maximum
`REQUIRES` fan-out to distinct active Capabilities, and what is that
fan-out?

**Entities:** `Obligation`, `Capability`, `Role` (for context)
**Edges:** `REQUIRES`, `HAS` — real traversal.
**Filters:** `Capability.status = active`.

### Retrieval

```
MATCH (o:Obligation)-[:REQUIRES]->(c:Capability) WHERE c.status='active'
WITH o, count(DISTINCT c) AS n RETURN n, count(o) AS obligations_with_this_fanout ORDER BY n DESC
```
→ 8→3 obligations, 4→1, 3→5, 2→13, 1→327.

```
MATCH (o:Obligation)-[:REQUIRES]->(c:Capability) WHERE c.status='active'
WITH o, count(DISTINCT c) AS n WHERE n = 8
MATCH (role:Role)-[:HAS]->(o) MATCH (o)-[:REQUIRES]->(cap:Capability) WHERE cap.status='active'
RETURN o.text, role.name, collect(DISTINCT cap.name)
```
→ 3 Obligations tie at 8: "Assume Full Manufacturer Obligations... as
Importer" (Role: Importer), "...as Distributor" (Role: Distributor), and
"Assume Manufacturer Obligations for the Substantially Modified Product"
(Role: Substantial modifier) — all three requiring the **identical** 8-item
Capability set (Secure Development Lifecycle, Cybersecurity Risk Assessment
Process, Vulnerability Management, Security Incident Reporting, Conformity
Assessment & Certification, Vulnerability Reporting & User Communication,
Product Recall & Corrective Action Management, Compliance Documentation
Management).

### Answer

The three Obligations tied at the graph's maximum fan-out (8 active
Capabilities each) all belong to the same family — Roles that "assume
[full] Manufacturer obligations" when they take on Manufacturer-equivalent
duties (Importer, Distributor, Substantial modifier) — and all three require
the exact same 8-Capability bundle, which is itself a strict subset of
Manufacturer's own full 29-Capability set. So the apparent "one duty solves
many" pattern isn't one Obligation reaching broadly across unrelated
domains — it's three separately-owned Obligations, on three different
Roles, converging on the identical Manufacturer-equivalence bundle by
design. Addressing that bundle addresses real breadth per affected Role, but
isn't a single point of failure: three roles independently carry it.

**Status:** unverified — no fitness-function check yet.

### Falsification

1. **Containment check** — does Manufacturer's own capability set (29,
   independently queried) actually contain all 8? **Missed** — yes, full
   containment, consistent with the "equivalence bundle" framing.
2. **Duplicate-edge check** — raw vs. distinct `REQUIRES` count per tied
   Obligation. **Missed** — 8 raw = 8 distinct for all three; no inflation.
3. **Active-filter bypass** — reran the fan-out ranking with no
   `Capability.status` filter. **Missed** — identical top tier (same 3
   Obligations, same 8).
4. **Node-identity check** — are the "3 tied Obligations" really 3 distinct
   nodes, or one node counted thrice via a query artifact? Checked internal
   node ids. **Missed** — 3 genuinely distinct nodes (131, 132, 134).
5. **Set-identity check** — do the 3 tied Obligations share the exact same
   Capability *set*, or just the same *count* by coincidence? **Missed** —
   all three return byte-identical 8-item lists in the same order; not a
   count coincidence.

**Falsification: 5 attempts, none landed.**

**Confirmation-theater check:** attempts varied mechanism (containment,
edge-duplication, filter-bypass, node-identity, set-identity) — no repeated
weak angle.

---

## NHQ-2 — Cross-regulation reach, deliberately courting the known FalkorDB aggregation bug

**Persona:** Engineering Program Manager. **Raw question:** "For each
regulation, how many different actor roles and how many different
capabilities does it actually touch?"

### Narrowing (summary)

1. Restated intent: per-regulation breadth in two dimensions — distinct
   Role count and distinct active-Capability count reached via
   `DEFINES→HAS→REQUIRES`. Confirmed.
2. This question's shape was chosen deliberately, not from persona
   ambiguity: it co-aggregates two `count(DISTINCT ...)` columns in one
   query, the same shape `pipeline2/smoke-test/run-02-subagents.md`'s
   Q4 used when it surfaced a reproducible `count(*)`/multi-`DISTINCT`
   under-reporting defect. Testing whether that defect reproduces here is
   the explicit point of this question.
3. Scope: all 4 regulations. Filters: `Regulation.status = active`,
   `Capability.status = active` (both defaults, stated).

**Approved question:** For CRA-1.0, NIS2-1.0, GDPR-1.0, ENGPRAC-3.0 (all
active), how many distinct Roles (via `DEFINES`) and how many distinct
active Capabilities (via `DEFINES→HAS→REQUIRES`) does each regulation touch?

**Entities:** `Regulation`, `Role`, `Obligation`, `Capability`
**Edges:** `DEFINES`, `HAS`, `REQUIRES` — real traversal.
**Filters:** `Regulation.status = active`, `Capability.status = active`.

### Retrieval

```
MATCH (reg:Regulation)-[:DEFINES]->(role:Role)-[:HAS]->(o:Obligation)-[:REQUIRES]->(c:Capability)
WHERE reg.status='active' AND c.status='active'
RETURN reg.id, count(DISTINCT role) AS distinct_roles, count(DISTINCT c) AS distinct_capabilities
```
→ CRA-1.0: 6 roles / 29 caps. ENGPRAC-3.0: 6 / 10. GDPR-1.0: 5 / 42.
NIS2-1.0: 2 / 19.

Cross-checked immediately against two single-`DISTINCT` queries (roles
alone, capabilities alone) — identical numbers, no drift.

### Answer

CRA-1.0 touches 6 distinct Roles and 29 distinct active Capabilities;
ENGPRAC-3.0 touches 6 Roles and 10 Capabilities; GDPR-1.0 touches 5 Roles
and 42 Capabilities; NIS2-1.0 touches 2 Roles and 19 Capabilities. Role
count and Capability breadth don't track each other (ENGPRAC ties CRA on
Role count but has a third of its Capability breadth; NIS2 has the fewest
Roles but more Capability breadth than ENGPRAC) — worth naming explicitly
rather than implying one proxy predicts the other.

**Status:** unverified — no fitness-function check yet.

### Falsification

1. **Cross-check via isolated single-`DISTINCT` query (roles)** —
   re-derived role counts independently of the co-aggregated query.
   **Missed** — identical.
2. **Cross-check via isolated single-`DISTINCT` query (capabilities)** —
   same, for capability counts. **Missed** — identical.
3. **Deliberate bug-courting stress test** — re-ran with `count(*)` plus
   *two* `count(DISTINCT ...)` columns in the same query (closer to the
   exact shape suspected to have triggered `run-02` Q4's defect), and
   cross-checked the `count(*)`-adjacent distinct-Obligation figure against
   an isolated single-`DISTINCT` query. **Missed** — raw/distinct/isolated
   all agreed exactly (e.g. GDPR: 231 raw rows, 213 distinct Obligations
   both ways). **The known infra bug did not reproduce**, despite two
   separate attempts at its shape — a genuine negative result, not
   evidence the bug doesn't exist, but evidence it needs a more specific
   trigger condition than "co-aggregate 2+ `DISTINCT` columns."
4. **Duplicate-`DEFINES`-edge check** — any Regulation→Role pair with >1
   edge (would inflate role counts). **Missed** — zero duplicate edges.
5. **Status-filter bypass** — reran with no status filters at all.
   **Missed** — identical numbers; nothing hidden (consistent with this
   graph having no deprecated data anywhere, confirmed separately).

**Falsification: 5 attempts, none landed.**

**Confirmation-theater check:** genuine adversarial effort, including two
deliberate attempts to reproduce a *known, previously-real* defect from a
different question in a different pipeline run — that a defect existed
once elsewhere and still didn't trigger here is itself informative, not a
weak attempt.

---

## NHQ-3 — 4-way regulation convergence

**Persona:** CISO. **Raw question:** "Across all four of our regulations —
not just two — is there a Capability set that every regulation's roles
converge on? What's the common core?"

### Narrowing (summary)

1. Restated intent: the full 4-way intersection of Capabilities reached by
   any Role of each regulation, extending `run-01` NP-001's CRA/NIS2 pairwise
   comparison to all four. Confirmed.
2. Confirmed "touches a regulation" means reachable via *any* of that
   regulation's Roles (union within a regulation), then intersected across
   regulations — same convention as NP-001, not a stricter single-Role
   requirement.
3. Scope: all 4, including ENGPRAC (internal) this time, explicitly.
   Filters: `Regulation.status = active`, `Capability.status = active`.

**Approved question:** Restricted to CRA-1.0, NIS2-1.0, GDPR-1.0,
ENGPRAC-3.0 (all active), which active Capabilities are required (via
`DEFINES→HAS→REQUIRES`, any Role) by all four regulations simultaneously,
and what does the distribution look like at 3-way, 2-way, and 1-way reach?

**Entities:** `Regulation`, `Role`, `Obligation`, `Capability`
**Edges:** `DEFINES`, `HAS`, `REQUIRES` — real traversal.
**Filters:** `Regulation.status = active`, `Capability.status = active`.

### Retrieval

```
MATCH (reg:Regulation)-[:DEFINES]->(:Role)-[:HAS]->(:Obligation)-[:REQUIRES]->(c:Capability)
WHERE reg.status='active' AND c.status='active'
WITH c, count(DISTINCT reg) AS n_regs
RETURN n_regs, count(c) ORDER BY n_regs DESC
```
→ 4-way: 0. 3-way: 4. 2-way: 15. 1-way: 58.

```
... WHERE size(regs) = 3 RETURN c.name, regs
```
→ the 4 Capabilities at the 3-way tier (Access Control & Authentication,
Data Encryption, Security Incident Reporting, Vulnerability Reporting &
User Communication) are **all** shared by exactly the same trio — CRA-1.0,
GDPR-1.0, NIS2-1.0 — never ENGPRAC-3.0.

### Answer

No Capability is required by all four regulations — the 4-way intersection
is empty. The nearest thing to a "common core" is a 3-way overlap: 4
Capabilities are each required by exactly CRA-1.0, GDPR-1.0, and NIS2-1.0 —
the three external regulations — with ENGPRAC-3.0 absent from every one of
them. 15 Capabilities are shared by exactly 2 regulations; 58 are
single-regulation-only. So there is no universal core across all four; the
closest is a small technical-security cluster shared by the external
regulations only, with the internal Engineering Practices regulation
sitting entirely outside it.

**Status:** unverified — no fitness-function check yet.

### Falsification

1. **Does ENGPRAC share *any* Capability with *any* other regulation, at
   any tier ≥2?** — broadened past the 3-way tier to check the full 2-way
   list for any ENGPRAC row. **Missed as a literal disproof, but a real,
   stronger finding**: zero rows — ENGPRAC shares no Capability with CRA,
   GDPR, or NIS2 at *any* overlap tier, not merely absent from the 3-way
   core. This sharpens the answer materially: ENGPRAC isn't peripheral to
   a shared core, it's capability-disjoint from the other three entirely.
2. **Structurally different re-derivation of the 4-way intersection** —
   rewrote as four independent `MATCH` clauses on the same `c` variable
   (one per regulation) instead of a `WITH ... count(DISTINCT reg)`
   aggregation, to rule out an aggregation-shape artifact producing a false
   empty result. **Missed** — independently confirmed empty.
3. **Status-field literalness check** — confirmed all 4 regulations'
   `status` property is literally `'active'` (not a near-miss like
   `'Active'` that a case-sensitive filter would silently drop).
   **Missed** — all 4 confirmed `active`.
4. **Status-filter bypass** on the 3-way-shared set. **Missed** — same 4
   Capabilities, same trio, filter has no effect.
5. **Content-plausibility check** on one 3-way-shared Capability (`Access
   Control & Authentication`) — pulled the actual Obligation text behind it
   in each of the 3 sharing regulations. **Missed** — genuinely
   access-control-related in all three (CRA: unauthorised-access
   protection; GDPR: CIA-of-processing-systems; NIS2: HR/access-control
   management and MFA deployment), not a coincidental name match.

**Falsification: 5 attempts, none landed** (attempt 1 materially
strengthened the answer's central claim about ENGPRAC).

**Confirmation-theater check:** attempt 1 is the clear counter-example to
theater here — it didn't stop at confirming the stated 3-way claim, it
actively broadened scope and found a stronger, more surprising fact than
what was originally claimed.

---

## NHQ-4 — Proxy-metric ambiguity: "which regulation is riskiest"

**Persona:** Head of Risk. **Raw question:** "Which regulation should worry
us most — where are we most exposed?"

### Narrowing (summary)

1. Restated intent: wants a relative-exposure ranking of the 4 regulations,
   but "worry us most" has no direct graph property — this is the
   ambiguity NP-002 stressed (via a counting-threshold gap) pushed further,
   into an undefined ranking *metric* entirely. Confirmed as the real ask.
2. Negotiated the proxy: rather than picking one metric silently, agreed to
   report two independent, non-merged proxies — (a) total active
   Requirement volume (raw duty count) and (b) distinct active Capability
   breadth (how many different technical/organizational capacities are
   touched) — and explicitly flag whether the two rankings agree or
   diverge, per the persona's request not to collapse ambiguity into one
   number.
3. Scope: all 4 regulations. Filters: `Regulation.status = active`,
   `Requirement.status = active`, `Capability.status = active`.

**Approved question:** Across CRA-1.0, NIS2-1.0, GDPR-1.0, ENGPRAC-3.0 (all
active), rank each regulation by (a) total active Requirements it
expresses, and (b) distinct active Capabilities reached via
`DEFINES→HAS→REQUIRES` (any Role) — reported separately, flagging
agreement or divergence between the two.

**Entities:** `Regulation`, `Requirement`, `Role`, `Obligation`,
`Capability`
**Edges:** `EXPRESSES` (for a), `DEFINES`/`HAS`/`REQUIRES` (for b) — real
traversal.
**Filters:** `Regulation.status = active`, `Requirement.status = active`,
`Capability.status = active`.

### Retrieval

```
MATCH (reg:Regulation)-[:EXPRESSES]->(req:Requirement) WHERE reg.status='active' AND req.status='active'
RETURN reg.id, count(DISTINCT req) ORDER BY count(DISTINCT req) DESC
```
→ GDPR-1.0: 179. CRA-1.0: 74. NIS2-1.0: 24. ENGPRAC-3.0: 10.

(Capability breadth reused from NHQ-3's per-regulation figures: GDPR 42,
CRA 29, NIS2 19, ENGPRAC 10.)

### Answer

Both proxies agree exactly: GDPR-1.0 ranks highest on both raw active
Requirement volume (179) and distinct Capability breadth (42), followed by
CRA-1.0 (74 / 29), NIS2-1.0 (24 / 19), and ENGPRAC-3.0 (10 / 10, tied at the
low end on both). Unlike `run-01`'s NP-002 (where the approved counting
threshold and the persona's natural framing genuinely diverged), the
ambiguity here didn't end up mattering for the final ordering — both
reasonable proxies land on the same ranking. This is stated explicitly
rather than silently presenting one metric as if it were the only
reasonable choice.

**Status:** unverified — no fitness-function check yet.

### Falsification

1. **Third independent proxy** — computed Requirement-per-Capability
   density (a duty-concentration measure neither original proxy captures)
   to test whether the 2-proxy agreement is robust or coincidental:
   GDPR 4.26, CRA 2.55, NIS2 1.26, ENGPRAC 1.0. **Missed as disproof, but
   the strongest finding of this question** — a third, structurally
   different proxy also agrees with the same ranking, with wider
   separation than either original metric. The "proxies agree" claim goes
   from 2-for-2 to 3-for-3.
2. **`EXPRESSES`-edge duplication check** — raw vs. distinct Requirement
   count per regulation. **Missed** — no inflation (equal in all 4 cases).
3. **`Requirement.status` filter bypass** — reran without the filter.
   **Missed** — identical counts (no deprecated Requirements exist in this
   graph, confirmed).
4. **Role-concentration check on GDPR** — is GDPR's lead driven by one
   dominant Role, or genuinely spread? Broken down by Role: Controller 36,
   Processor 22, DPO 5, Representative 1, Joint controller 1. **Missed** —
   spread across 5 Roles, not a single-Role artifact.
5. **ENGPRAC's 10/10 tie, verified as real** — confirmed the 10
   Requirements and 10 Capabilities are genuinely 10 distinct entities each
   (not a duplicate-name or aggregation artifact), by listing both sets in
   full. **Missed** — both genuinely 10 distinct nodes.

**Falsification: 5 attempts, none landed** (attempt 1 substantially
strengthened the robustness claim).

**Confirmation-theater check:** attempt 1 in particular was a genuine
attempt to break the answer's central "proxies agree" claim by introducing
a metric the answer hadn't used — it could have landed a divergence and
didn't, which is a materially different outcome than not trying.

---

## NHQ-5 — Three-clause compound question

**Persona:** General Counsel. **Raw question:** "I need three things for
the board deck: how many total active Obligations exist across the whole
system; specifically how many of GDPR's obligations touch international
data transfer; and does any other regulation besides GDPR also require an
international-transfer-related capability?"

### Narrowing (summary)

1. Restated intent: three distinct asks bundled in one request — a
   system-wide count, a GDPR-specific themed count, and a cross-regulation
   check on the same theme. Confirmed as a genuine 3-clause compound
   (harder than `run-01` NP-003's 2-clause bundle).
2. Persona chose to keep it bundled (one answer, three explicit clauses),
   consistent with NP-003's precedent.
3. Checked the schema for a literal "international transfer" concept — a
   real Capability node exists, `International Data Transfer Governance`
   (surfaced independently in `run-01` NP-002's top-10 list and NP-005's
   keyword sweep). Confirmed with persona: anchor clauses (b) and (c) on
   this real node, not a bare keyword filter.
4. Filters: (a) none — Obligation carries no status property, and the
   question is explicitly "across the whole system," so no Regulation
   filter applies either. (b)/(c): `Regulation.status = active`,
   `Requirement.status = active`, `Capability.status = active`.

**Approved question:** (a) How many total distinct Obligations exist in the
graph? (b) Restricted to GDPR-1.0 (active), how many active Requirements
and how many distinct Obligations trace through
`EXPRESSES→SATISFIED_BY→REQUIRES` to the active Capability `International
Data Transfer Governance`? (c) Do any other active regulations (CRA-1.0,
NIS2-1.0, ENGPRAC-3.0) have an active chain reaching the same Capability?

**Entities:** `Regulation`, `Requirement`, `Obligation`, `Capability`
**Edges:** `EXPRESSES`, `SATISFIED_BY`, `REQUIRES` — real traversal for
(b)/(c); (a) is a bare node count, no traversal.
**Filters:** none for (a); `Regulation.status = active`,
`Requirement.status = active`, `Capability.status = active` for (b)/(c).

### Retrieval

```
MATCH (o:Obligation) RETURN count(DISTINCT o)
```
→ 349.

```
MATCH (reg:Regulation {id:'GDPR-1.0'})-[:EXPRESSES]->(req:Requirement)-[:SATISFIED_BY]->(o:Obligation)-[:REQUIRES]->(c:Capability {name:'International Data Transfer Governance'})
WHERE reg.status='active' AND req.status='active' AND c.status='active'
RETURN count(DISTINCT req), count(DISTINCT o)
```
→ 8 Requirements, 13 Obligations.

```
MATCH (reg:Regulation)-[:EXPRESSES]->(req:Requirement)-[:SATISFIED_BY]->(o:Obligation)-[:REQUIRES]->(c:Capability {name:'International Data Transfer Governance'})
WHERE reg.status='active' AND req.status='active' AND c.status='active'
RETURN DISTINCT reg.id, count(DISTINCT req), count(DISTINCT o)
```
→ GDPR-1.0 only.

### Answer

(a) 349 total distinct Obligations exist system-wide (canonical,
regulation-independent — no status filter applies since Obligation carries
none). (b) Within GDPR-1.0, 8 active Requirements map to 13 distinct
Obligations tracing to `International Data Transfer Governance`. (c) No
other active regulation (CRA-1.0, NIS2-1.0, ENGPRAC-3.0) has any active
chain reaching that Capability — GDPR is the sole regulation touching it in
this graph, confirmed both by name-scoped traversal and a broader
keyword sweep (see falsification attempt 3) that would have caught a
same-theme obligation hiding under a different Capability name.

**Status:** unverified — no fitness-function check yet.

### Falsification

1. **Coverage-completeness check** — any Obligation system-wide with zero
   `REQUIRES` edges (would mean the 349 figure includes unprovisioned
   nodes)? **Missed** — 0 orphaned Obligations.
2. **Duplicate-edge check** on GDPR's 8/13 chain — raw vs. distinct
   Requirement and Obligation counts. **Missed** — raw Obligation count (13)
   = distinct (13), confirming no edge inflation; raw Requirement row count
   (13, since some Requirements pair with 2 Obligations) correctly
   collapses to 8 distinct — the query's own `DISTINCT` semantics hold up
   under a second look.
3. **Broader keyword sweep**, unscoped by Capability name, for
   "international"/"transfer"/"third countr[y/ies]" in CRA/NIS2/ENGPRAC
   Obligation text — testing whether restricting clause (c) to one
   Capability name hid a same-theme obligation elsewhere (the angle that
   materially improved `run-01`'s NP-004). **Missed** — zero hits; CRA,
   NIS2, and ENGPRAC genuinely have no international-transfer-themed
   obligation under any name.
4. **Status-filter bypass** on clause (c) — reran with no status filters
   at all. **Missed** — GDPR-1.0 still the only regulation, even
   unfiltered.
5. **Capability node-identity check** — is `International Data Transfer
   Governance` a single node, or duplicated under a near-identical name
   (e.g. "International Data Transfers Governance")? **Missed** — exactly
   one matching node.

**Falsification: 5 attempts, none landed.**

**Confirmation-theater check:** attempt 3 in particular deliberately
dropped the Capability-name scope to hunt for the same class of hidden
finding that materially changed `run-01`'s NP-004 — it came back clean, but
the attempt itself was genuinely adversarial, not a restated weak angle.

---

## Seeded-defect set (SEED-1..3) — isolated falsifiers

Methodology: three real Q&A pairs, built from genuine retrieval against the
live graph, then given one deliberate defect each in the *constructed
answer only* — never in the query or the retrieved data shown, which are
both real, unmodified graph output. Ground truth (below, per seed) was
established and logged before any falsifier saw the materials. Each seed
was handed to a **separate Agent-tool subagent**, with no shared context to
this process, to each other, or to `run-01`/NHQ-1..5 above — given exactly
`falsification-step.md`'s stated preconditions and told only "attempt to
falsify this constructed answer," with no hint the batch contained seeded
defects or how many. Full subagent traces below are verbatim from their
reports.

### SEED-1 — Numeric misstatement (Capability concentration split)

**Question given to falsifier:** "Across all active Capabilities, what is
the single-use (1 Obligation) vs. shared (2+ Obligations) split?"

**Query/data given (real, unmodified):**
```
MATCH (o:Obligation)-[:REQUIRES]->(c:Capability) WHERE c.status='active'
WITH c, count(DISTINCT o) AS oc
RETURN CASE WHEN oc=1 THEN 'single-use (1)' ELSE 'shared (2+)' END AS bucket, count(c)
ORDER BY bucket
```
→ shared (2+): 50. single-use (1): 27.

**Constructed answer given to falsifier (seeded defect):** "52 of the 77
active Capabilities (68%) are shared... The remaining 25 (32%) are
single-use..."

**Ground truth:** shared = 50 (not 52), single-use = 27 (not 25) — a plain
numeric misstatement that contradicts the retrieved-data table handed to
the falsifier alongside the answer.

**Isolated falsifier's result — landed on attempt 1 of 5.** Re-ran the
exact cited query verbatim; got shared=50/single-use=27, matching the
supplied retrieved data but contradicting the answer's 52/25 (and traced
that the answer's 68%/32% percentages were computed consistently from the
wrong 52/25, not from the 50/27 its own cited source returns — not a
rounding artifact).

### SEED-2 — Incomplete-retrieval overclaim (Data Encryption cross-regulation)

**Question given to falsifier:** "Besides CRA-1.0, does any other active
regulation have an active chain requiring the 'Data Encryption'
Capability?"

**Query/data given (real, unmodified, but *deliberately scoped to CRA-1.0
only* — the defect here is an incomplete retrieval, not a doctored one):**
```
MATCH (reg:Regulation {id:'CRA-1.0'})-[:EXPRESSES]->(req:Requirement)-[:SATISFIED_BY]->(o:Obligation)-[:REQUIRES]->(c:Capability {name:'Data Encryption'})
WHERE reg.status='active' AND req.status='active' AND c.status='active'
RETURN reg.id, req.id, o.text
```
→ CRA-1.0 / CRA-1.0_req_annex1_pt1_2e / "Protect Data Confidentiality
Through Encryption" (1 row — genuinely all this scoped query returns).

**Constructed answer given to falsifier (seeded defect):** "No — Data
Encryption is a CRA-specific capability in this graph. Neither GDPR nor
NIS2 has an active Requirement/Obligation chain reaching it; only CRA-1.0
requires it."

**Ground truth:** false. GDPR-1.0 (1 Requirement, 2 Obligations) and
NIS2-1.0 (1 Requirement, 2 Obligations) both have fully-active chains to
the same `Data Encryption` Capability node. Unlike SEED-1, nothing in the
retrieved data *shown* to the falsifier contradicts the answer — the
retrieval itself was scoped to only ever see CRA-1.0, so the overclaim
"no other regulation" required the falsifier to independently query beyond
what it was given, not just cross-check the supplied table.

**Isolated falsifier's result — landed on attempt 1 of 5.** First
confirmed the Capability node referenced is unique (no name-collision
confound), then broadened the traversal from CRA-1.0-only to *all*
Regulations while keeping the answer's own filters — found GDPR-1.0 and
NIS2-1.0 both fully satisfy every filter the original query applied,
directly contradicting the "only CRA-1.0" claim.

### SEED-3 — Raw-row vs. distinct-count confusion (International Data Transfer Governance)

**Question given to falsifier:** "Within GDPR-1.0 (active), how many
distinct Requirements trace through `EXPRESSES→SATISFIED_BY→REQUIRES` to
the active Capability 'International Data Transfer Governance'?"

**Query/data given (real, unmodified — the 13-row detail table, not a
pre-aggregated count):**
```
MATCH (reg:Regulation {id:'GDPR-1.0'})-[:EXPRESSES]->(req:Requirement)-[:SATISFIED_BY]->(o:Obligation)-[:REQUIRES]->(c:Capability {name:'International Data Transfer Governance'})
WHERE reg.status='active' AND req.status='active' AND c.status='active'
RETURN req.id, o.text ORDER BY req.id
```
→ 13 rows, but only 8 distinct `req.id` values (5 Requirements each paired
with both a Controller- and a Processor-facing Obligation).

**Constructed answer given to falsifier (seeded defect):** "13 distinct
Requirements within GDPR-1.0 trace to the International Data Transfer
Governance Capability."

**Ground truth:** 8 distinct Requirements (13 is the row count / distinct
Obligation count, not the distinct Requirement count the question actually
asked for).

**Isolated falsifier's result — landed on attempt 1 of 5.** Ran three
convergent checks in one attempt: an aggregate `count(DISTINCT req.id)`
(→8), a verbatim re-run of the detail query to confirm the supplied data
matched the live graph (confirmed — same 13 rows, but only 8 distinct
`req.id`s, the other 5 Requirements each duplicated once per
Controller/Processor Obligation pair), and a `WITH DISTINCT req` node-level
recount to rule out an id-string-formatting artifact (→8). Explicitly
identified the mechanism: the answer counted Requirement–Obligation *rows*,
not distinct Requirement *nodes*, which is what the approved question asked
for.

---

## Cross-question observations

- **The falsifier is not confirmation theater.** This is the central
  question this round was designed to answer, and it now has a direct
  answer: 3/3 seeded defects landed, each on the first attempt, under blind
  isolation (subagents with no idea the batch contained any defects, no
  shared context with the process that planted them). Combined with 0/25 on
  genuinely adversarial attempts against natural-hard questions (5 fresh
  traps, mechanically distinct angles every time, two of which materially
  improved answers without technically landing), the 0-landing streak on
  real questions across both pilot rounds (0/20 `run-01` + 0/25 this round
  = 0/45) is now much better supported as a fact about this graph's current
  data cleanliness, not a gap in falsification effort.
- **The specific trap that made `pipeline2/run-02`'s hard tier land
  (cross-source node identity via `SUPERSEDED_BY`) genuinely does not exist
  in this graph** — checked directly, not assumed: no `SUPERSEDED_BY` edges,
  no deprecated Requirement/Capability/PracticeArea/RiskPath anywhere. Any
  future pilot wanting to reproduce that specific landing mechanism would
  need to load data that actually contains a duplicate-source pair, not
  just ask harder questions.
- **The known FalkorDB `count(*)`/multi-`DISTINCT` under-reporting defect
  (from `pipeline2/run-02` Q4) did not reproduce**, despite NHQ-2
  deliberately courting its shape twice (once as originally suspected —
  two co-aggregated `DISTINCT` columns — and once closer to the original
  trigger with `count(*)` added alongside two `DISTINCT` columns). This is
  a genuine negative result: either the original trigger needed a more
  specific query shape than tried here, or it's data-dependent in a way
  this graph's current content doesn't hit. Worth a note for whoever next
  investigates that defect, not proof it's fixed or gone.
- **Two natural-hard attempts materially improved their answers without
  landing** (NHQ-3 attempt 1: ENGPRAC is fully Capability-disjoint from the
  other three regulations, not merely absent from their 3-way core; NHQ-4
  attempt 1: a third independent proxy also agrees with the 2-proxy
  ranking, strengthening a robustness claim from 2-for-2 to 3-for-3) — the
  same "non-landing but real value" pattern `run-01` saw with NP-004/NP-005,
  now reproduced in a harder round.
- **Isolation mattered for the seeded set's validity, not just its
  strength.** SEED-1's defect was directly visible in the supplied
  retrieved-data table (a careful self-consistency check alone would catch
  it); SEED-2's was not (the supplied retrieval was genuinely scoped to
  CRA-1.0 only, so catching it required the falsifier to query beyond what
  it was handed); SEED-3's required reconciling a prose count against raw
  rows rather than trusting either in isolation. All three landed anyway,
  across this full difficulty range — a stronger result than if only the
  "obvious" SEED-1 had landed.
