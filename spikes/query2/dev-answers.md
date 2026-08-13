# Dev Answers & Grading Criteria

Expected answers and grading criteria for every question in
[`dev-questions.md`](./dev-questions.md). The two files are deliberately
separate: the question catalog carries no hint of how a question is
graded, what the right answer is, or whether one exists.

**Dataset provenance.** Answers were computed against the synthetic
compliance dataset (CRA, NIS2, GDPR, and the internal Helvex SOP) on
2026-08-06. Time-relative answers are anchored to the dataset's reference
date **2026-08-01**, not to wall-clock time — any evaluation run must
supply that anchor explicitly, or date-relative answers will silently drift.

**Regulation provenance.** Answers derived from regulation text cite their
source as file + article/paragraph + line range in `eu-regulations/`, so
correctness can be checked against the actual texts. Answers about the
internal organization (capabilities, policies, standards, controls) carry
no provenance by design — their facts trace to the synthetic compliance
dataset, not to regulation text.

**Register.** Each entry carries a `**Register:**` tag — `canonical` if the
question uses the conceptual model's vocabulary (schema terms like Control,
Obligation, Capability), `natural` if it uses everyday phrasing. The ~20%
canonical share is deliberate: Policy Manager and Auditor questions (power
users who plausibly learn the tool's vocabulary) plus one baseline question
per other audience. Canonical keepers: LC-E1, CO-M1, SA-E1, RM-M2, SWE-E1,
SEC-E2, EM-M1, PM-E1/E3/M1/M2/H1/E2/E4/M3/M4/H3/H4, AU-E1/E2/M2/M3/M4/H3.
Natural-register questions whose phrasing needs mapping to schema terms
carry a `**Mapping:**` line recording it.

**Grading vocabulary** (implementation-neutral):

- **exact value** — one correct value; compared directly.
- **set** — a correct set of items (order-independent); the candidate's set
  is diffed against the golden set.
- **rubric** — no single correct answer; graded against the listed criteria.
  A correct answer must hit every "must" point.

Where an expected set is large, only the count and boundary items are
inlined; the full golden set is produced by the verification query in the
[appendix](#appendix-verification-queries). Entries marked *(from
example-questions.md)* are carried over unchanged from the original
catalog; their full verification queries live in
[`golden-answers.md`](./golden-answers.md).

**Golden fixes applied (2026-08-09), verified against the live graph and
`docs/regulations/*.md` by read-only query before editing — see
`spikes/cli-tool-semantics/RUNBOOK.md`'s dev-v1 and dev-v2b results for the
original defect evidence:**

- **LC-H2 / LC-M2 / CO-M3:** removed the false "NIS2's intermediate report
  has no CRA counterpart" claim (CRA Art. 14(6) is a real, structural
  counterpart to NIS2 Art. 23(4)(c)); added the previously-omitted CRA
  Art. 14(8) inform-users duty to all three entries' notification sets.
- **RM-H1:** GDPR Art. 32.1b requires a third capability,
  `cap_availability_resilience_7caf2b` (verified ungoverned) — the entry
  previously listed only two and called the sub-clause "clean"; now
  "partial." Overall verdict changed from "2 of 6 clean" to "1 of 6 clean."
- **AU-H2:** the required conclusion ("does not reach a currently verified
  control") contradicted the entry's own facts — the Quarterly Incident
  Triage SLA Review control genuinely is `implemented`, just overdue for
  review, and this skill's own canonical definitions distinguish "overdue"
  (live chain, lapsed review) from "stale" (broken chain). Reworded to
  require "reaches an implemented control that is not currently verified,"
  matching the schema-level distinction elsewhere in this dataset (see also
  `spikes/skill-transfer/GOLDEN-FIXES.md`'s AU-M4/EM-E3 entry, which
  established the same distinction for the held-out set).
- **SWE-H2:** added `cap_data_protection_by_design_default_69e489`
  (GDPR Art. 25 privacy-by-design/by-default, verified ungoverned) to the
  required capability list — omitted despite being squarely on-point for a
  *new* PII-storing system.
- **EM-M3 / SEC-H3 (fine-exposure sub-question only):** reclassified from
  graded-against-real-figures to refusal-expected. GDPR Art. 83(5), CRA
  Art. 64(2), and NIS2 Art. 34(4)–(5) fine tiers are confirmed absent from
  the graph (FINDING-001) — grading these against real figures rewarded
  answering from outside the sanctioned graph surface, the exact behavior
  both spikes exist to catch. The real figures are kept as reference values
  only. **Not yet applied to the same underlying pattern in LC-E2 and
  RM-E2**, whose "Grading" lines still read as if graph-answerable despite
  being graded as correct refusals in practice in both spikes' RUNBOOK.md
  results — flagged here as an open follow-up, not fixed in this pass.

---

## Legal Counsel

### LC-E1 — "What's the text of CRA Article 13.1?" (from example-questions.md)

**Register:** canonical

**Grading:** exact value

**Expected answer:** "Manufacturers shall ensure that a product with digital
elements is designed, developed and produced in accordance with the
essential cybersecurity requirements set out in Part I of Annex I."

**Provenance:** [CRA.md](eu-regulations/CRA.md), Art. 13(1), L2650–2656.

### LC-E2 — "What are the maximum administrative fines under GDPR for infringing the basic processing principles?"

**Register:** natural

**Mapping:** "basic processing rules" → GDPR basic principles for processing, Arts. 5, 6, 7, 9 (Art. 83(5)(a))

**Grading:** exact value

**Expected answer:** Up to EUR 20 000 000, or in the case of an undertaking
up to 4 % of total worldwide annual turnover of the preceding financial
year, whichever is higher. The basic-principles category covers
infringements of Articles 5, 6, 7 and 9.

**Provenance:** [gdpr.md](eu-regulations/gdpr.md), Art. 83(5)(a), L5966–5973
(figure at L5969).

### LC-M1 — "How many obligations does GDPR place on Data Processors vs. Data Controllers?" (from example-questions.md)

**Register:** natural

**Grading:** exact value

**Expected answer:** Controller = 148, Processor = 55. (For completeness if
the question is read more broadly: Data Protection Officer = 7,
Joint controller = 2, Representative = 1.)

### LC-M2 — "How do the CRA's reporting deadlines for an actively exploited vulnerability differ from those for a severe incident?"

**Register:** natural

**Mapping:** "report … deadlines" → CRA Art. 14(2) vulnerability track vs Art. 14(4) severe-incident track

**Grading:** set (of deadline stages per track)

**Expected answer:** Both tracks notify the CSIRT designated as coordinator
and ENISA via the single reporting platform, and both open with the same
two stages: an early warning within 24 hours of becoming aware, and a full
notification within 72 hours. They diverge on the final report: for an
actively exploited vulnerability, no later than 14 days after a corrective
or mitigating measure is available; for a severe incident, within one month
after the incident notification. (Both tracks also carry two clockless
duties that sit outside this deadline comparison: an intermediate report if
the coordinating CSIRT requests one (Art. 14(6)), and informing impacted
users, and where appropriate all users (Art. 14(8)) — not part of the
graded deadline set, but should not be denied when asked about generally.)

**Provenance:** [CRA.md](eu-regulations/CRA.md), Art. 14(1)–(2),
L2870–2898 (14-day final report at L2893); Art. 14(3)–(4), L2902–2925
(24-hour early warning at L2926); Art. 14(6), L2961–2963; Art. 14(8),
L3007–3013.

### LC-H1 — "Do CRA and NIS2 impose obligations on similar roles?" (from example-questions.md)

**Register:** natural

**Mapping:** "similar kinds of actors / something like a 'manufacturer'" → Role nodes; semantic similarity across CRA/NIS2 role sets

**Grading:** rubric

Real role sets: CRA = {Manufacturer, Importer, Distributor, Authorised
representative, Open-source software steward, Substantial modifier};
NIS2 = {Essential entity, Important entity}.

**A correct answer must:**
- Cite these real role sets, not invented ones.
- Recognize there is no structural equivalence to exploit — roles are not
  shared across regulations — and reason by semantic similarity instead
  (e.g. "Manufacturer" and "Essential/Important entity" both name
  duty-bearing product/service operators subject to risk-management
  obligations).
- Not overclaim a precise mapping the data doesn't support.

### LC-H2 — "An actively exploited vulnerability in our product turns out to be both a severe incident under the CRA and a significant incident under NIS2 — walk me through every notification we owe, to whom, and by when."

**Register:** natural

**Grading:** rubric

The real notification tracks:

- **CRA, vulnerability track** (Art. 14(1)–(2), plus (6) and (8)): to the
  CSIRT designated as coordinator and ENISA, via the single reporting
  platform — early warning ≤24h; vulnerability notification ≤72h; final
  report ≤14 days after a corrective or mitigating measure is available;
  plus an intermediate report if the coordinating CSIRT requests one
  (Art. 14(6)) and a duty to inform impacted users, and where appropriate
  all users (Art. 14(8)).
- **CRA, severe-incident track** (Art. 14(3)–(4), plus (6) and (8)): same
  recipients and platform — early warning ≤24h; incident notification ≤72h;
  final report ≤1 month after the incident notification; the same
  request-driven intermediate report (Art. 14(6)) and inform-users duty
  (Art. 14(8)) as the vulnerability track — both apply to "the actively
  exploited vulnerability or severe incident" without distinguishing tracks.
- **NIS2, significant-incident track** (Art. 23(1), (4)): to the CSIRT or
  competent authority — early warning ≤24h; incident notification ≤72h;
  intermediate report on request; final report ≤1 month after the incident
  notification; progress report + extended final report if the incident is
  still ongoing.

**A correct answer must:**
- Enumerate all three tracks with the correct recipients, channels, and
  clocks.
- Recognize CRA's Art. 14(6) intermediate-report-on-request as a structural
  counterpart to NIS2's Art. 23(4)(c) — both are triggered by the
  coordinating CSIRT/competent authority requesting one, not a routine
  step the manufacturer/entity initiates. (Previous versions of this golden
  asserted NIS2's intermediate report "has no CRA counterpart" — that was
  wrong; see the fix note in the appendix.)
- Note the recipients differ: CRA routes through the single reporting
  platform to CSIRT coordinator + ENISA; NIS2 routes to the national
  CSIRT or competent authority.
- Cite CRA's Art. 14(8) duty to inform impacted (and where appropriate all)
  users — a fourth CRA-side obligation alongside the three notification
  stages, with no explicit NIS2 counterpart in the graph.
- Not assert a GDPR Article 33 breach notification unless a personal data
  breach is actually established — the premise states neither.

**Provenance:** [CRA.md](eu-regulations/CRA.md), Art. 14, L2868–2936;
Art. 14(6), L2961–2963; Art. 14(8), L3007–3013;
[NIS2.md](eu-regulations/NIS2.md), Art. 23(1)–(4), L3538–3600.

---

## Compliance Officer

### CO-E1 — "What roles does GDPR define?" (from example-questions.md)

**Register:** natural

**Mapping:** "regulated parties" → Role nodes defined by GDPR-1.0

**Grading:** set

**Expected answer:** {Controller, Processor, Joint controller, Data
Protection Officer, Representative}

### CO-E3 — "Is there a minimum support period for products under the CRA, and how long is it?"

**Register:** natural

**Grading:** exact value

**Expected answer:** Yes — the support period must be at least five years.
Where the product is expected to be in use for less than five years, the
support period corresponds to the expected use time instead.

**Provenance:** [CRA.md](eu-regulations/CRA.md), Art. 13(8), L2698–2728.

### CO-M1 — "What obligations does the Manufacturer role carry under CRA?" (from example-questions.md)

**Register:** canonical

**Grading:** set

**Expected answer:** 48 obligations, from `obl_apply_data_minimisation_563d25`
to `obl_take_corrective_action_for_non_conforming_products_2abebb`. The full
golden set is produced by the appendix verification query; a grader should
diff id sets, not eyeball a list this long. (Count of 48 was verified by two
independent query phrasings after an initial manual count of 47 — don't
trust hand-counts on 40+ row lists.)

### CO-M3 — "When we become aware of an actively exploited vulnerability in our product, what exactly do we have to report, to whom, and on what timeline?"

**Register:** natural

**Mapping:** "report, to whom, and how fast" → CRA Art. 14(1)–(2) and (8) reporting obligations of manufacturers

**Grading:** set

**Expected answer:** Notify simultaneously the CSIRT designated as
coordinator and ENISA, via the single reporting platform:

1. **Early warning** — without undue delay, in any event within 24 hours;
   indicates affected Member States where applicable.
2. **Vulnerability notification** — within 72 hours; general information
   about the product, the general nature of the exploit and vulnerability,
   corrective/mitigating measures taken and those users can take, plus an
   indication of how sensitive the information is considered.
3. **Final report** — no later than 14 days after a corrective or
   mitigating measure is available; description of the vulnerability incl.
   severity and impact, malicious-actor information where available, and
   details of the security update or corrective measures.
4. **Inform impacted users** (Art. 14(8)) — after becoming aware, inform
   the impacted users of the product, and where appropriate all users, of
   the vulnerability and, where necessary, any risk-mitigation or
   corrective measures they can take. No fixed clock like stages 1–3; if
   the manufacturer fails to inform users in a timely manner, the notified
   CSIRTs may do so directly.

**Provenance:** [CRA.md](eu-regulations/CRA.md), Art. 14(1)–(2),
L2870–2898; Art. 14(8), L3007–3013.

### CO-H1 — "We process customer data and we ship a software product — which of GDPR, CRA, and NIS2 actually apply to us, and in what roles?"

**Register:** natural

**Mapping:** "as what kind of actor" → Role nodes (controller/processor, manufacturer, essential/important entity)

**Grading:** rubric

**A correct answer must:**
- Map "process customer data" to GDPR applicability in the roles of
  controller and/or processor (Art. 4 definitions), and "ship a software
  product" to CRA applicability in the role of manufacturer (Art. 13),
  assuming the product is made available on the Union market.
- State that NIS2 applicability is **conditional and undetermined** from
  the facts given: the entity must be of a type listed in NIS2 Annex I or
  II and meet the essential/important entity thresholds (Art. 3). Neither
  "processes customer data" nor "ships software" alone establishes that.
- Not invent additional roles or claim all three regulations apply by
  default.

**Provenance:** [gdpr.md](eu-regulations/gdpr.md), Art. 4, L2135–2297;
[CRA.md](eu-regulations/CRA.md), Art. 13(1), L2650–2656;
[NIS2.md](eu-regulations/NIS2.md), Art. 3, L1972–2003.

### CO-H2 — "We found a vulnerability in an open-source component we bundle — what does the CRA require beyond shipping our own fix?"

**Register:** natural

**Mapping:** "do more" → CRA Art. 13(5)–(6) + Annex I Part II obligations

**Grading:** rubric

**A correct answer must:**
- Cite the duty to **report the vulnerability to the person or entity
  manufacturing or maintaining the component** and to **share the relevant
  fix code or documentation** with them where a modification was developed
  (Art. 13(6)) — this is the core of "beyond our own fix."
- Cite the due-diligence duty when integrating third-party and open-source
  components (Art. 13(5)).
- Cite the Annex I Part II duties that follow: identify and document
  components via a software bill of materials (point (1)), remediate
  without delay (point (2)), publicly disclose fixed vulnerabilities once
  a security update is available (point (4)), and maintain a coordinated
  vulnerability disclosure policy (point (5)).
- Note the conditional escalation: if the vulnerability is **actively
  exploited**, the Art. 14 reporting clocks (24h/72h/14 days) start.

**Provenance:** [CRA.md](eu-regulations/CRA.md), Art. 13(5)–(6),
L2678–2691; Annex I, Part II, points (1)–(5), L5229–5250; Art. 14(1)–(2),
L2870–2898.

---

## Security Architect

### SA-E1 — "What capabilities does 'Maintain Security Logging' require?" (from example-questions.md)

**Register:** canonical

**Grading:** set

**Expected answer:** {`cap_security_logging_c4d9e2` — Security Logging}
(obligation: `obl_maintain_security_logging_c427be`)

### SA-E2 — "Which capability does CRA's 'protect against unauthorised access' obligation converge on?"

**Register:** natural

**Mapping:** "unauthorised-access protection duty" → obl_protect_against_unauthorised_access_ef908f → cap_access_control_authentication_151816

**Grading:** exact value

**Expected answer:** `cap_access_control_authentication_151816` — required
by CRA's `obl_protect_against_unauthorised_access_ef908f`.

### SA-M1 — "Which obligations, across CRA, NIS2, and GDPR, require a 'Security Logging'-type capability?" (from example-questions.md)

**Register:** natural

**Mapping:** "security-logging-type capability" → cap_security_logging_c4d9e2 and near-neighbors

**Grading:** rubric

Of the three external regulations, only CRA has an obligation requiring the
Security Logging capability (`cap_security_logging_c4d9e2`). NIS2's closest
is folded into `cap_access_control_authentication_151816` ("...report on
unauthorised access attempts"); GDPR's closest,
`cap_data_protection_compliance_monitoring_478cb7`, is about compliance
monitoring/audits, not log capture, and is a legitimately separate capacity.

**A correct answer must:**
- Name `obl_maintain_security_logging_c427be` (CRA) as the real converged
  obligation.
- Explicitly state that NIS2 and GDPR have **no** obligation requiring this
  capability today — not silently omit them, not invent a plausible-sounding
  one.
- Not claim "all three" have coverage; the correct scope is "only CRA, of
  the three."

### SA-M3 — "How many of our 68 capabilities are governed by an approved policy, as opposed to a draft or deprecated one?"

**Register:** natural

**Mapping:** "covered by an approved policy" → Capability–GOVERNED_BY→Policy with status approved

**Grading:** exact value

**Expected answer:** 9. Of 68 capabilities, 13 are governed at all; 4 of
those sit under non-approved policies (2 under the `deprecated` Legacy
Asset & Personnel Security Policy, 2 under the `draft` Clinical Data
Integrity Policy), leaving 9 governed by an `approved` policy.

### SA-H1 — "If we adopt a 'Software Bill of Materials' capability..." (from example-questions.md)

**Register:** natural

**Grading:** rubric

An SBOM capability already exists in the dataset:
`cap_component_inventory_sbom_management_b5223c`, required only by CRA's
`obl_identify_and_document_components_via_software_bill_of_materials_dcfaae`.
No NIS2 or GDPR obligation requires it today.

**A correct answer must:**
- Recognize this isn't minting something new — the real question is whether
  NIS2/GDPR obligations should newly converge onto the existing capability.
- Correctly report zero current redundant coverage (no NIS2/GDPR obligation
  already requires it, so there is nothing to flag as "already redundantly
  covered" yet).

### SA-H2 — "If a single capability fails, which failure puts the most obligations at risk — and is that the right way to think about criticality?"

**Register:** natural

**Mapping:** "capability … fails … most obligations" → REQUIRES edge counts; cap_data_subject_rights_fulfilment_communication_8eedf0

**Grading:** rubric

**A correct answer must:**
- Name `cap_data_subject_rights_fulfilment_communication_8eedf0` as the
  capability required by the most obligations (45) — that is the correct
  answer by raw fan-out.
- Explicitly challenge the framing: obligation count is blast radius, not
  criticality — a capability serving 2 obligations can still be the single
  point of failure for a high-severity duty (e.g.
  `cap_access_control_authentication_151816` carries NIS2's explicit MFA
  obligation among its 7).
- Not claim the dataset computes a criticality ranking — it doesn't; the
  count is all it supports.

---

## Auditor

### AU-E1 — "Which requirement does the 'Maintain Security Logging' obligation satisfy?" (from example-questions.md)

**Register:** canonical

**Grading:** exact value

**Expected answer:** `CRA-1.0_req_annex1_pt1_2l` — "Products shall provide
security-related information by recording and monitoring relevant internal
activity, including access to or modification of data, services or
functions, with a user opt-out mechanism."

### AU-E3 — "What must a controller's record of processing activities contain under GDPR Article 30?"

**Register:** natural

**Mapping:** "record of processing activities" → GDPR Art. 30(1)

**Grading:** set

**Expected answer** (Art. 30(1)(a)–(g)): name and contact details of the
controller, joint controller, representative and DPO; the purposes of the
processing; categories of data subjects and of personal data; categories of
recipients, including in third countries or international organisations;
where applicable, third-country transfers incl. identification of the
country and documentation of suitable safeguards; where possible, envisaged
erasure time limits per data category; where possible, a general
description of the Art. 32(1) technical and organisational security
measures.

**Provenance:** [gdpr.md](eu-regulations/gdpr.md), Art. 30(1), L3413–3447.

### AU-M1 — "Trace the full path from CRA Art. 13.1 to whatever capability it ultimately requires." (from example-questions.md)

**Register:** natural

**Mapping:** "whatever it ultimately requires us to be able to do" → Capability node at end of chain

**Grading:** exact value

**Expected answer:** `CRA-1.0_req_art_13.1` →
`obl_ensure_secure_product_design_and_development_c56d3c` ("Ensure Secure
Product Design and Development") → `cap_secure_development_lifecycle_9f3224`
("Secure Development Lifecycle").

### AU-M2 — "Show every path from a GDPR requirement down to a Control that verifies it." (from example-questions.md)

**Register:** canonical

**Grading:** set (with an honesty caveat)

**Expected answer:** 57 full chains exist, covering GDPR Art. 28.3f/g,
32.1a/b/c, 32.4, 33.1/2/3a-d, 37.1/5/7, 38.1/2/3/6. A chain counts as
**current evidence** iff its Policy is `approved`, its Standard is
`implemented` or `reviewed`, and its Control is `implemented`. On that
definition the set splits **31 current-evidence / 26 stale** — 16 chains
route through the `deprecated` Legacy Asset & Personnel Security Policy
(Art. 32.4, 37.*, 38.* — personnel/DPO duties) and 10 more end in a `draft`
Standard or `planned` Control under the otherwise-approved Incident &
Vulnerability Response Policy (Art. 28.3f, 32.1c, 33.*).

A correct answer surfaces all 57 chains but flags the 26 as stale /
not-current-evidence — presenting all 57 as equally trustworthy fails.

### AU-H1 — "If an external auditor challenges our GDPR Article 33 breach-notification compliance, what evidence chain exists, and how much of it is current?"

**Register:** natural

**Mapping:** "evidence trail" → Requirement→Obligation→Capability→Policy→Standard→Control chains (GDPR Art. 33.*)

**Grading:** rubric

**A correct answer must:**
- Establish that requirement-to-control chains covering Art. 33.1/2/3a-d
  exist among the 57 GDPR chains.
- Apply the current-evidence definition (approved Policy + implemented or
  reviewed Standard + implemented Control) and conclude the Art. 33 chains
  are **partially stale**: they belong to the 10 chains ending in a `draft`
  Standard or `planned` Control under the otherwise-approved Incident &
  Vulnerability Response Policy.
- Cite the concrete soft spots: the Quarterly Incident Triage SLA Review
  control is `implemented` but overdue (next review 2026-07-20, before the
  2026-08-01 reference date), and the Automated Vulnerability Patch SLA
  Check is `planned` with no evidence yet.
- Not present breach-notification compliance as fully evidenced.

### AU-H2 — "Trace the CRA's actively-exploited-vulnerability reporting duty from the regulation text all the way into our governance — does the chain reach an implemented control?"

**Register:** natural

**Mapping:** "check that's actually running" → Control with implementation_status implemented (CRA Art. 14(1)–(2) duty)

**Grading:** rubric

**A correct answer must:**
- Cite the regulation side correctly: Art. 14(1)–(2) duties to notify the
  CSIRT designated as coordinator and ENISA via the single reporting
  platform, on 24h/72h/14-day clocks.
- Walk the governance side honestly: the only incident-response governance
  is the `approved` Incident & Vulnerability Response Policy, whose two
  controls are the `implemented` but **overdue** Quarterly Incident Triage
  SLA Review (2026-07-20) and the `planned` Automated Vulnerability Patch
  SLA Check (no evidence yet).
- Conclude the chain **does** reach an `implemented` control (the Quarterly
  Incident Triage SLA Review) — per this skill's own canonical definitions,
  a live control with a lapsed review is "overdue," not "stale," and
  "overdue" is not the same claim as "no control reached." The honest
  conclusion is narrower than a flat "not reached": the chain reaches an
  implemented control whose own review is overdue, so it is running but not
  *currently verified* — and the second leg (Automated Vulnerability Patch
  SLA Check) has no implemented control at all yet. Not claim a dedicated
  CRA-reporting control exists beyond these two, and not present the
  overdue review as full, current assurance.

**Provenance (regulation side):** [CRA.md](eu-regulations/CRA.md),
Art. 14(1)–(2), L2870–2898.

---

## Risk Manager

### RM-E1 — "What risk-management measures must essential and important entities implement at minimum under NIS2?"

**Register:** natural

**Mapping:** "security measures … at minimum" → NIS2 Art. 21(2)(a)–(j)

**Grading:** set

**Expected answer** (Art. 21(2)(a)–(j)): policies on risk analysis and
information system security; incident handling; business continuity (backup
management, disaster recovery) and crisis management; supply chain
security; security in network and information systems acquisition,
development and maintenance, including vulnerability handling and
disclosure; policies and procedures to assess the effectiveness of
cybersecurity risk-management measures; basic cyber hygiene practices and
cybersecurity training; policies and procedures on cryptography and, where
appropriate, encryption; human resources security, access control policies
and asset management; use of multi-factor or continuous authentication,
secured communications and secured emergency communication systems, where
appropriate.

**Provenance:** [NIS2.md](eu-regulations/NIS2.md), Art. 21(2), L3449–3481.

### RM-E2 — "When is an incident 'significant' and therefore reportable under NIS2?"

**Register:** natural

**Grading:** exact value (two-limb definition)

**Expected answer:** An incident is significant if (a) it has caused or is
capable of causing severe operational disruption of the services or
financial loss for the entity concerned; or (b) it has affected or is
capable of affecting other natural or legal persons by causing considerable
material or non-material damage.

**Provenance:** [NIS2.md](eu-regulations/NIS2.md), Art. 23(3), L3572–3578.

### RM-M1 — "Which capabilities are required by more than one obligation?" (from example-questions.md)

**Register:** natural

**Mapping:** "capabilities carry more than one regulatory duty" → Capability REQUIRES in-degree > 1

**Grading:** set

**Expected answer:** 52 capabilities, ranging from
`cap_data_subject_rights_fulfilment_communication_8eedf0` (45 obligations)
down to several at 2. The full golden set is produced by the appendix
verification query; a grader should diff id sets, not eyeball.

### RM-M3 — "How concentrated is our obligation risk — how many capabilities are shared across obligations versus single-use?"

**Register:** natural

**Mapping:** "shared capabilities versus single-use ones" → REQUIRES edge concentration (52 shared / 16 single-use)

**Grading:** set (with a required caveat)

**Expected answer:** 52 of the 68 capabilities are required by more than
one obligation; 16 are single-use. Sharing is heavily skewed rather than
uniform: the most-shared capability,
`cap_data_subject_rights_fulfilment_communication_8eedf0`, alone serves 45
obligations, while many shared capabilities sit at just 2.

A correct answer gives both numbers (52 shared / 16 single-use) and notes
the concentration — presenting "52 shared" as if sharing were evenly
distributed fails.

### RM-H1 — "Are we compliant with GDPR Article 32?" (from example-questions.md)

**Register:** natural

**Grading:** rubric

Art. 32's six sub-obligations resolve as follows:

- **32.1** (umbrella "appropriate technical and organisational measures") →
  `cap_cybersecurity_risk_management_program_50601b` → **no governing policy
  at all — ungoverned, no evidence to cite**
- **32.1a** (encryption) → `cap_data_encryption_0e50d3` → approved policy,
  all standards implemented/reviewed — **clean**
- **32.1b** (CIA/resilience) → `cap_access_control_authentication_151816` +
  `cap_data_configuration_integrity_protection_882f84` +
  `cap_availability_resilience_7caf2b` → the first two sit under the same
  approved policy with clean standards, but the third has **no governing
  policy at all** — **partial**, not clean (the "resilience" third of
  "CIA/resilience" has zero evidence).
- **32.1c** (restore availability after incident) →
  `cap_business_continuity_disaster_recovery_9c1c32` → approved policy; one
  implemented standard/control, one draft/planned standard/control —
  **partial**
- **32.1d** (test/evaluate effectiveness) →
  `cap_security_control_effectiveness_assessment_627623` → **no governing
  policy at all — ungoverned, no evidence to cite**
- **32.4** (personnel process only on instructions) →
  `cap_asset_personnel_security_management_e68e9a` → governed only by the
  **deprecated** Legacy Asset & Personnel Security Policy, whose standard
  and control are also deprecated — **stale, not current evidence**

**A correct answer must:**
- Conclude **partial compliance** (1 of 6 sub-clauses clean, 2 partial,
  1 stale, 2 entirely ungoverned) — not a uniform verdict in either
  direction.
- Cite the real chain per sub-clause above.
- Explicitly flag 32.4's evidence as stale (governed only by a deprecated
  policy) and 32.1/32.1d as having no governance at all — a stronger gap
  than "stale."
- Flag 32.1c's second control as not-yet-implemented rather than silently
  rolling it into "compliant."
- Flag 32.1b's third capability (`cap_availability_resilience_7caf2b`) as
  ungoverned rather than presenting the pair it does have evidence for as
  the whole sub-clause.

### RM-H2 — "If we benchmark our NIS2 Article 21 compliance against our GDPR Article 32 posture, where do we stand?"

**Register:** natural

**Mapping:** "NIS2 Article 21 readiness … GDPR Article 32 posture" → shared capability convergence (encryption, access control/MFA, effectiveness testing, business continuity, risk-management program)

**Grading:** rubric

**A correct answer must:**
- Recognize the benchmark works because the two articles converge on the
  same capabilities: MFA/access control (NIS2 21(2)(i)–(j) ≈ GDPR 32.1b) →
  `cap_access_control_authentication_151816`, governed by an approved
  policy with an implemented control — **strong on both**. Encryption
  (21(2)(h) ≈ 32.1a) → `cap_data_encryption_0e50d3` — **clean on both**.
- Carry the gaps over honestly: effectiveness testing (21(2)(f) ≈ 32.1d) →
  `cap_security_control_effectiveness_assessment_627623` — ungoverned;
  business continuity (21(2)(c) ≈ 32.1c) →
  `cap_business_continuity_disaster_recovery_9c1c32` — partial; umbrella
  risk management (21(1) ≈ 32.1) →
  `cap_cybersecurity_risk_management_program_50601b` — ungoverned.
- Conclude our NIS2 Art. 21 posture largely **inherits** the GDPR Art. 32
  posture — strengths and gaps travel together.
- Not invent NIS2-only capabilities to make the picture look more complete.

**Provenance (regulation side):** [NIS2.md](eu-regulations/NIS2.md),
Art. 21(2), L3449–3481.

---

## Policy Manager

### PM-E1 — "What policy governs the 'Security Logging' capability?" (from example-questions.md)

**Register:** canonical

**Grading:** exact value

**Expected answer:** `pol_data_protection_security_policy_8e4c18` — "Data
Protection & Security Policy" (status: approved).

### PM-E3 — "What's the status and version of the Clinical Data Integrity Policy?"

**Register:** canonical

**Grading:** exact value

**Expected answer:** `pol_clinical_data_integrity_policy_e1a539` — status
`draft`, version 0.3. One of its standards is also `draft`.

### PM-M1 — "Which governed capabilities have zero implemented controls underneath, and why for each?"

**Register:** canonical

**Grading:** set

**Expected answer:** 4 capabilities, in two distinct situations:

- `cap_asset_personnel_security_management_e68e9a` and
  `cap_data_protection_officer_management_ec3cd2` — governed by the
  `deprecated` Legacy Asset & Personnel Security Policy, whose only control
  is itself deprecated: verification existed once but was retired with the
  policy.
- `cap_data_protection_impact_assessment_a51acb` and
  `cap_clinical_trial_data_integrity_f28d55` — governed by the `draft`
  Clinical Data Integrity Policy, which has no controls under it at all
  yet: verification was never built.

A correct answer names all four and distinguishes "retired" from
"never built" — the policy-side fix differs.

### PM-M2 — "Which of our policies have all their supporting standards in a current — implemented or reviewed — state?"

**Register:** canonical

**Grading:** set

**Expected answer:** Only the Data Protection & Security Policy — all 3 of
its standards are `implemented` or `reviewed`. The Incident & Vulnerability
Response Policy does not qualify: one of its standards is still `draft`,
with a `planned` control underneath.

### PM-H1 — "NIS2 was updated — which of our Policies are now potentially out of date?" (from example-questions.md)

**Register:** canonical

**Grading:** rubric

The dataset contains one real version supersession to reason with:
HELVEX-SOP-1.0 is superseded by HELVEX-SOP-2.0. No NIS2 version update
exists in the dataset — that premise is hypothetical. Real policy staleness
signals: `pol_legacy_asset_personnel_security_policy_7ed6c2` (`deprecated`,
all its standards deprecated) and `pol_clinical_data_integrity_policy_e1a539`
(`draft`, v0.3, one standard also `draft`).

**A correct answer must:**
- Use the real supersession edge as the mechanism it would walk for a real
  NIS2 version update, while noting the NIS2-specific premise is
  hypothetical.
- Flag the deprecated and draft policies as "potentially out of date" rather
  than treating all 4 policies uniformly.

### PM-H2 — "GDPR Article 32.4's personnel-instruction duty routes through a deprecated policy — what are my options, and the risk of each?"

**Register:** natural

**Mapping:** "staff may only process data on instructions" → GDPR Art. 32(4) → cap_asset_personnel_security_management_e68e9a

**Grading:** rubric

The real chain: GDPR Art. 32(4) →
`cap_asset_personnel_security_management_e68e9a` → governed only by the
`deprecated` Legacy Asset & Personnel Security Policy, whose standard and
control are also deprecated.

**A correct answer must:**
- Name the real capability and policy, not generic placeholders.
- Lay out at least these options with their risks:
  - **Replace**: mint and approve a successor policy governing the two
    orphaned capabilities — restores current evidence for 16 GDPR chains
    (Art. 32.4, 37.*, 38.*), but takes a full governance cycle.
  - **Revive/re-approve** the deprecated policy — fastest, but re-approves
    stale content without review and papers over why it was deprecated.
  - **Leave as-is** — the 16 chains stay stale; deprecation is not
    resolution. Deleting instead is strictly worse (see PM-H4 in the blind
    set): the capabilities become ungoverned entirely.
- Not treat "deprecated" as "handled."

**Provenance (regulation side):** [gdpr.md](eu-regulations/gdpr.md),
Art. 32(4), L3529–3534.

---

## Software Engineer

### SWE-E1 — "What's the implementation status of the Encryption-at-Rest control?" (from example-questions.md)

**Register:** canonical

**Grading:** exact value

**Expected answer:**
`ctrl_std_pol_data_protection_security_policy_8e4c18_v1_automated` —
`implemented`, next review 2026-08-15.

### SWE-E3 — "What essential properties does the CRA require of the software I ship?"

**Register:** natural

**Mapping:** "essential security properties" → CRA Annex I, Part I, point (2)(a)–(m)

**Grading:** set

**Expected answer** (Annex I, Part I, point (2)(a)–(m), applied on the
basis of the Art. 13(2) cybersecurity risk assessment): made available
without known exploitable vulnerabilities; secure-by-default configuration
with reset to original state; vulnerabilities addressable through security
updates, incl. automatic updates by default with opt-out; protection from
unauthorised access with reporting of possible unauthorised access;
confidentiality of data, e.g. state-of-the-art encryption at rest and in
transit; integrity of data, commands, programs and configuration, with
corruption reporting; data minimisation; availability of essential
functions, incl. resilience against denial-of-service attacks; minimised
negative impact on other devices and networks; limited attack surfaces;
exploitation mitigation mechanisms; recording and monitoring of relevant
internal activity with a user opt-out; secure, permanent removal of all
data and settings, with secure transfer where applicable.

**Provenance:** [CRA.md](eu-regulations/CRA.md), Annex I, Part I, point (2),
L5176–5226.

### SWE-M1 — "What does the CRA require me to do about vulnerabilities in the third-party components I integrate?"

**Register:** natural

**Mapping:** "vulnerabilities in third-party components" → CRA Art. 13(5)–(6) + Annex I Part II (1)–(2)

**Grading:** set

**Expected answer:**
1. Exercise due diligence when integrating third-party components —
   including free and open-source software — so they don't compromise the
   product's cybersecurity (Art. 13(5)).
2. On identifying a vulnerability in an integrated component: report it to
   the component's manufacturer or maintainer, remediate it per Annex I
   Part II, and share any fix code/documentation with the maintainer
   (Art. 13(6)).
3. Identify and document components via a software bill of materials in a
   commonly used, machine-readable format covering at least top-level
   dependencies (Annex I, Part II, point (1)).
4. Address and remediate vulnerabilities without delay, providing security
   updates — where technically feasible, separately from functionality
   updates (Annex I, Part II, point (2)).

**Provenance:** [CRA.md](eu-regulations/CRA.md), Art. 13(5)–(6),
L2678–2691; Annex I, Part II, points (1)–(2), L5229–5236.

### SWE-M2 — "Which controls sit under the Data Protection & Security Policy, and what are their statuses and review dates?"

**Register:** natural

**Mapping:** "checks" → Control nodes under pol_data_protection_security_policy_8e4c18

**Grading:** set

**Expected answer:** 3 controls, all `automated` and `implemented`:

- `ctrl_std_pol_data_protection_security_policy_8e4c18_v1_automated` —
  Encryption-at-Rest; next review 2026-08-15.
- `ctrl_std_pol_data_protection_security_policy_8e4c18_v2_automated` —
  Access Control & MFA Enforcement Audit; next review 2026-08-25.
- `ctrl_std_pol_data_protection_security_policy_8e4c18_v3_automated` —
  Log Retention check; next review 2026-11-01.

### SWE-H1 — "Is this new API endpoint, which logs access but doesn't encrypt data at rest, compliant with GDPR Article 32?" (from example-questions.md)

**Register:** natural

**Grading:** rubric

"Logs access but doesn't encrypt data at rest" maps to
`cap_access_control_authentication_151816` / logging (Art. 32.1b territory —
covered, clean chain) but fails `cap_data_encryption_0e50d3` (Art. 32.1a —
the Encryption-at-Rest standard/control exist and are implemented, so there
is a concrete, named control the endpoint isn't passing).

**A correct answer must:**
- Perform this description-to-capability mapping explicitly, not silently.
- Cite the real Encryption-at-Rest control the endpoint fails.
- Conclude non-compliant on 32.1a specifically, not a vague overall verdict.

### SWE-H2 — "I'm building a new microservice that stores customer PII — what compliance capabilities should I be thinking about?" (from example-questions.md)

**Register:** natural

**Grading:** rubric

**A correct answer must:**
- Name several real capabilities by id, not vague categories — at minimum
  `cap_data_encryption_0e50d3` (data at rest/in transit),
  `cap_access_control_authentication_151816`, `cap_security_logging_c4d9e2`,
  `cap_data_protection_impact_assessment_a51acb` (a DPIA is plausibly
  triggered by a new PII-processing system under GDPR Art. 35),
  `cap_secure_data_removal_portability_3d7885` (data-subject
  deletion/portability rights), and
  `cap_data_protection_by_design_default_69e489` (GDPR Art. 25(1)/(2)
  privacy-by-design and by-default — required by
  `obl_implement_data_protection_by_design_as_controller_bafbc1` and
  `obl_implement_data_protection_by_default_as_controller_d847cd`,
  currently ungoverned; squarely on-point for a *new* system, arguably the
  single most relevant capability to raise since it applies at design time
  rather than after the fact).
- Explicitly perform the description-to-capability mapping rather than
  silently assuming it.
- Not claim these capabilities are *satisfied* just because they're
  relevant — the question asks what to think about, not for a compliance
  verdict.

---

## Security Engineer

### SEC-E1 — "Which Controls exist under the Incident & Vulnerability Response Policy, and what are their statuses?" (from example-questions.md)

**Register:** natural

**Mapping:** "checks … what state" → Control nodes under pol_incident_vulnerability_response_policy_9de859

**Grading:** set

**Expected answer:** 2 controls —
`ctrl_std_pol_incident_vulnerability_response_policy_9de859_v1_manual`
"Quarterly Incident Triage SLA Review" (`implemented`, but overdue — next
review 2026-07-20, before the 2026-08-01 reference date) and
`ctrl_std_pol_incident_vulnerability_response_policy_9de859_v2_automated`
"Automated Vulnerability Patch SLA Check" (`planned`, no evidence yet).

### SEC-E2 — "Does NIS2 explicitly require multi-factor authentication?"

**Register:** canonical

**Grading:** exact value

**Expected answer:** Yes — Art. 21(2)(j) requires the use of multi-factor
authentication or continuous authentication solutions, plus secured voice,
video and text communications and secured emergency communication systems,
**where appropriate**.

**Provenance:** [NIS2.md](eu-regulations/NIS2.md), Art. 21(2)(j),
L3475–3481.

### SEC-M1 — "Which Capabilities have a governing Policy but zero implemented Controls underneath?" (from example-questions.md)

**Register:** natural

**Mapping:** "policy on paper but no working check" → Capability GOVERNED_BY Policy with no implemented Control

**Grading:** set

**Expected answer:** 4 capabilities —
`cap_asset_personnel_security_management_e68e9a` and
`cap_data_protection_officer_management_ec3cd2` (both under the `deprecated`
Legacy Asset & Personnel Security Policy, whose only control is itself
deprecated), and `cap_data_protection_impact_assessment_a51acb` and
`cap_clinical_trial_data_integrity_f28d55` (both under the `draft` Clinical
Data Integrity Policy, which has no controls under it at all yet).

### SEC-M3 — "How many obligations across the three regulations converge on our access-control/MFA capability, and which regulation names MFA explicitly?"

**Register:** natural

**Mapping:** "regulatory duties land on … capability" → obligations REQUIRES cap_access_control_authentication_151816 (7)

**Grading:** exact value

**Expected answer:** 7 obligations require
`cap_access_control_authentication_151816`: CRA's
`obl_protect_against_unauthorised_access_ef908f`; GDPR's
`obl_ensure_confidentiality_integrity_availability_and_resilience_of_p_888591`
(Controller) and `..._408068` (Processor); NIS2's
`obl_maintain_human_resources_security_access_control_and_asset_m_644c45`
and `..._40eba8` (Essential/Important entity) and
`obl_deploy_multi_factor_authentication_and_secured_communication_138a1f`/
`..._c2a8ea`. NIS2 is the regulation that names MFA explicitly, in
Art. 21(2)(j).

**Provenance (explicit-MFA claim):** [NIS2.md](eu-regulations/NIS2.md),
Art. 21(2)(j), L3475–3481.

### SEC-H1 — "If an attacker exploited a missing MFA control today, which regulatory obligations across CRA/NIS2/GDPR would we be out of compliance with?" (from example-questions.md)

**Register:** natural

**Mapping:** "missing MFA check" → cap_access_control_authentication_151816; "regulatory duties" → the 7-obligation set

**Grading:** rubric

"MFA" maps to `cap_access_control_authentication_151816`. 7 obligations
across all three regulations require it: CRA's
`obl_protect_against_unauthorised_access_ef908f`; GDPR's
`obl_ensure_confidentiality_integrity_availability_and_resilience_of_p_888591`
(Controller) and `..._408068` (Processor); NIS2's
`obl_maintain_human_resources_security_access_control_and_asset_m_644c45`/
`..._40eba8` (Essential/Important entity) and — NIS2 names MFA explicitly —
`obl_deploy_multi_factor_authentication_and_secured_communication_138a1f`/
`..._c2a8ea`.

**A correct answer must:**
- Perform the "MFA" → `cap_access_control_authentication_151816` mapping
  explicitly.
- Work backward from the capability to every obligation that requires it,
  across regulations, and name the real 7-obligation set — not just "GDPR
  and NIS2 in general."
- Note the capability is currently governed by an approved policy with an
  implemented control (Access Control & MFA Enforcement Audit, due
  2026-08-25) — so the question is hypothetical against *today's* evidence,
  and a good answer says so rather than implying an existing gap.

### SEC-H3 — "If we sit on a known actively-exploited vulnerability past the CRA's reporting windows, which duties have we breached, and what's the fine exposure?"

**Register:** natural

**Mapping:** "reporting windows … fine exposure" → CRA Art. 14(1)–(2) duties + Art. 64(2) fine tier

**Grading:** rubric

**A correct answer must:**
- Tie each missed window to its specific duty: missing the 24-hour early
  warning breaches Art. 14(1) with 14(2)(a); missing the 72-hour
  vulnerability notification breaches Art. 14(2)(b); missing the final
  report breaches Art. 14(2)(c). This half is graph-answerable (the
  Requirement/Obligation nodes for Art. 14(1)–(2) are ingested) and must be
  answered, not refused.
- Note that if the vulnerability's exploitation also constitutes a severe
  incident, the Art. 14(3)–(4) track is independently breached on its own
  clocks.
- Not inflate the exposure with GDPR or NIS2 fines without establishing
  that those regimes apply to the situation.

**Grading note on the fine-exposure half:** Art. 64(2)'s fine tier (EUR
15 000 000 / 2.5% of turnover, cited below for reference) is **not ingested
into the graph** — confirmed absent, tracked in the Known-Gaps Registry
(`.github/skills/ps-domain/SKILL.md`) and
[BACKLOG-FINDING-001.md](../skill-transfer/BACKLOG-FINDING-001.md). An
agent whose sanctioned surface is the graph (via `ps.py`, or via raw
Cypher in `skill-transfer`) cannot reach this figure and should refuse that
half rather than answer from outside knowledge — a refusal on the
fine-exposure sub-question, paired with a correct answer on the
breached-duties sub-question above, is the correct outcome, not a partial
failure. Cite the real figure (EUR 15 000 000 / 2.5%) only when grading a
surface that actually has access to the regulation text.

**Provenance:** [CRA.md](eu-regulations/CRA.md), Art. 14(1)–(2),
L2870–2898; Art. 64(2), L4907–4913.

---

## Engineering Manager

### EM-E1 — "How many capabilities do we track, and how many of them have a governing policy?"

**Register:** natural

**Grading:** exact value

**Expected answer:** 68 capabilities total; 13 governed, 55 ungoverned.

### EM-E2 — "How many controls do we run, and what's the status breakdown?"

**Register:** natural

**Mapping:** "checks … status breakdown" → Control nodes and implementation_status distribution

**Grading:** exact value

**Expected answer:** 6 controls total — 4 `implemented`, 1 `planned`, 1
`deprecated`.

### EM-M1 — "How many Controls are currently overdue for review?" (from example-questions.md)

**Register:** canonical

**Grading:** exact value

**Expected answer (anchored to 2026-08-01):** 1 —
`ctrl_std_pol_incident_vulnerability_response_policy_9de859_v1_manual`
(2026-07-20). The deprecated control with next review 2026-01-01 is
excluded: it's retired, not meaningfully "due."

### EM-M3 — "If the board asks 'what's the worst-case fine exposure across these three regulations?', what do I tell them?"

**Register:** natural

**Mapping:** "worst-case fine exposure" → GDPR Art. 83(5), CRA Art. 64(2), NIS2 Art. 34(4)–(5)

**Grading:** refusal-expected. None of the three fine tiers below are
ingested into the graph — confirmed absent across `skill-transfer`'s dev
run, `cli-tool-semantics` dev-v1, and dev-v2b, and tracked in the Known-Gaps
Registry (`.github/skills/ps-domain/SKILL.md`) and
[BACKLOG-FINDING-001.md](../skill-transfer/BACKLOG-FINDING-001.md). An
agent whose sanctioned surface is the graph cannot answer this question
without importing outside knowledge; the **correct answer is an honest
refusal** that names which figures are missing and routes the board to
legal counsel or the source regulation text, not a guess or an
externally-sourced number presented as graph-grounded. Grading this as a
"set (with required caveats)" against the figures below — as earlier
versions of this entry did — rewards exactly the ungrounded-answer behavior
the spike exists to catch; do not restore that grading type.

**Reference figures** (real, for verifying a refusal's citations or for
grading a surface that has direct access to the regulation text — not the
expected output of an agent scoped to the graph):
- **GDPR:** up to EUR 20 000 000 or 4 % of total worldwide annual turnover,
  whichever is higher (Art. 83(5), top tier).
- **CRA:** up to EUR 15 000 000 or 2.5 % of total worldwide annual turnover
  (Art. 64(2), covering the Annex I essential requirements and the Art. 13
  and 14 obligations).
- **NIS2:** for essential entities, maximum of at least EUR 10 000 000 or
  2 % of worldwide turnover; for important entities, at least EUR 7 000 000
  or 1.4 % (Art. 34(4)–(5)). NIS2 is a Directive — these are floors for
  national law, not directly imposed amounts — and NIS2 exposure is
  conditional on the organization being an essential or important entity
  at all.

**Provenance:** [gdpr.md](eu-regulations/gdpr.md), Art. 83(5), L5966–5971;
[CRA.md](eu-regulations/CRA.md), Art. 64(2), L4907–4913;
[NIS2.md](eu-regulations/NIS2.md), Art. 34(4)–(5), L4364–4380.

### EM-H1 — "Which of our draft Policies are blocking GDPR readiness?" (from example-questions.md)

**Register:** natural

**Mapping:** "draft policies" → Policy status draft (pol_clinical_data_integrity_policy_e1a539)

**Grading:** rubric

Only one policy is `draft`:
`pol_clinical_data_integrity_policy_e1a539`, governing
`cap_data_protection_impact_assessment_a51acb` (Data Protection Impact
Assessment) and `cap_clinical_trial_data_integrity_f28d55` (Clinical Trial
Data Integrity).

**A correct answer must:**
- Name `pol_clinical_data_integrity_policy_e1a539` specifically, not "some
  policies."
- Recognize the Data Protection Impact Assessment capability is directly
  GDPR-relevant (Art. 35 territory) — that's the actual "blocking GDPR
  readiness" claim, not just "it's a draft."
- Not conflate this with the separately-stale `deprecated` Legacy Asset &
  Personnel Security Policy — a real staleness signal, but not a *draft*
  one; the question asked specifically about draft.

### EM-H2 — "Give me a one-paragraph summary of our overall compliance posture I can bring to the board." (from example-questions.md)

**Register:** natural

**Grading:** rubric

**A correct answer must:**
- Ground every claim in a real number: 68 capabilities total, 13 governed /
  55 ungoverned; 4 policies (2 approved, 1 draft, 1 deprecated); 6 controls
  (4 implemented, 1 planned, 1 deprecated); 1 control currently overdue for
  review.
- Explicitly flag the deprecated/draft/planned signals as *not current
  evidence* rather than smoothing everything into a uniformly reassuring
  narrative.
- Not claim an overall compliance "score" the dataset doesn't actually
  compute — describe posture, don't fabricate a metric.

---

## Appendix: Verification queries

The queries below are how the large golden sets above were computed and can
be re-verified against the dataset. The full query set for carried-over
questions lives in [`golden-answers.md`](./golden-answers.md).

```cypher
// CO-M1 (Manufacturer obligations under CRA)
MATCH (:Regulation {id:"CRA-1.0"})-[:DEFINES]->(:Role {name:"Manufacturer"})-[:HAS]->(o:Obligation)
RETURN o.id, o.text ORDER BY o.id

// RM-M1 (capabilities required by more than one obligation)
MATCH (o:Obligation)-[:REQUIRES]->(c:Capability)
WITH c, count(DISTINCT o) AS n WHERE n > 1
RETURN c.id, n ORDER BY n DESC
```
