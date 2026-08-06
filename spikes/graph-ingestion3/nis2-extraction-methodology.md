# NIS2 Extraction Methodology

How `nis2.json` was derived from `spikes/eu-regulations/NIS2.md`, per the scope in `nis2-prompt.md`
(Art. 3(1)-(4), 20, 21, 23 only). Written after the fact, like `cra-extraction-methodology.md`, to
record the judgment calls actually applied — not just the target schema (`docs/artifacts/
ps-domain-concepts.md`). Backfilled once GDPR extraction raised the question of whether every
regulation should get one; CRA's is the model this follows.

---

## Role

**Test applied:** same as CRA — an actor type the Directive itself names and assigns duties to.

- Sourced from Art. 3(1)-(2): **Essential entity** (`DEFINES` `Art. 3(1)`) and **Important entity**
  (`DEFINES` `Art. 3(2)`). Both descriptions were pulled directly from the sub-point criteria in
  Art. 3(1)(a)-(g) and 3(2) rather than paraphrased down to something vaguer, since the thresholds
  (medium-sized enterprise ceilings, qualified trust service providers, TLD/DNS operators
  regardless of size, etc.) are what a future Authorization Model artifact would need.
- **Not minted: "Member State."** NIS2 is a Directive, so nearly every operative sentence is
  phrased "Member States shall ensure that essential and important entities [do X]" rather than
  CRA's direct "Manufacturers shall [do X]." Per the extraction prompt, the Member State
  transposition wrapper was stripped and the Requirement/Obligation pair was built from the
  substantive duty that lands on the entity — e.g. Art. 21(1)'s "Member States shall ensure that
  essential and important entities take appropriate and proportionate... measures" became a duty
  on Essential/Important entity directly, not a duty on Member States to legislate.
- **Not minted: "Trust service provider."** Art. 3(1)(b) folds qualified trust service providers
  into Essential entity regardless of size; Art. 23(4)'s first subparagraph gives them a shorter
  24-hour (not 72-hour) notification deadline. Rather than mint a third Role to carry that one
  variant deadline, it's recorded as a `_comment` on `NIS2-1.0_req_art_23.4b` and left unmodeled as
  a separate Requirement — trust service providers already inherit Essential entity's obligations,
  and the deadline variance is a parametric detail, not a distinct duty.

## Requirement

**Test applied:** same as CRA — does this span of text impose a distinct duty not already captured
elsewhere, once the Member-State wrapper is stripped.

**Granularity — paragraph/lettered-point, matching CRA's default:** Art. 21(2)'s ten lettered
risk-management measures ((a)-(j)) and Art. 23(4)'s lettered notification-timeline sub-duties
((a)-(e)) were each split into their own Requirement, the same way CRA's Annex I Part I points
were. Where a lettered point itself bundles more than one named measure in one sentence — e.g.
21(2)(i) "human resources security, access control policies and asset management" or 21(2)(j)
"multi-factor authentication... secured voice, video and text communications and secured emergency
communication systems" — the point was **kept as a single Requirement** rather than split further,
because the source text presents it as one undifferentiated clause with no internal
lettering/numbering to split on (unlike CRA's Art. 13(8), which read as one paragraph but had
three separable duties). The multi-capacity content of these bundled points surfaces downstream
instead, as fan-out on the `REQUIRES` edge (see Capability, below) — splitting the Requirement
text itself would have meant inventing sub-letters the Directive doesn't have.

**Inclusion filter:**
- Included: any paragraph/lettered point with an operative "shall" landing on the entity, once the
  Member State wrapper is stripped.
- Excluded — permissive, not mandatory: Art. 20(2)'s clause encouraging entities to "offer similar
  training to their employees" is a "shall encourage," not a duty on the entity itself; only the
  preceding clause (management body members are "required to follow training") was extracted.
- Excluded — institutional/procedural or liability-scoping, not duty-imposing on the entity: Art.
  20(1)'s second subparagraph ("without prejudice to national law as regards... liability of public
  servants") and the "can be held liable for infringements" clause within Art. 20(1) itself — the
  liability clause was left in the Requirement's quoted text (it's part of the same sentence) but
  did not spawn its own Obligation, the same treatment CRA gave penalty provisions bundled into
  duty-bearing paragraphs.
- Excluded per the prompt's scope, not re-litigated here: Annex I/II (Role-sourcing context only,
  same relationship CRA's Art. 3 had to CRA's duty articles), Art. 29-30 (voluntary information
  sharing), and all institutional articles outside 3/20/21/23.

**Identity/`source_ref`:** `NIS2-1.0_req_art_{article}.{paragraph}[letter]`, matching CRA's scheme
exactly (`{REG}_req_art_...`) so both regulations' Requirement ids are visibly comparable in shape
even though their content is regulation-scoped and can never collide.

## Obligation

**Test applied:** same as CRA — the canonical duty a Requirement establishes, attached to exactly
one Role.

- **Same hash-collision fix CRA needed, applied pre-emptively.** Because NIS2's duties are phrased
  identically for Essential entity and Important entity in every one of the 24 Requirements (Art.
  21(2)'s measures apply to both without differentiation), every Obligation text got a role-specific
  suffix ("...as Essential entity" / "...as Important entity") from the start, rather than being
  discovered as a collision partway through like CRA's Manufacturer/Importer/Distributor case. All
  24 Requirements produced exactly 2 Obligations each (48 total), one `HAS` edge per Role.
- `obligation_type`: same test as CRA (technical = a property/mechanism; organizational = a
  process). Technical: incident handling, the acquisition/development/maintenance security duty,
  cryptography/encryption policy, MFA/secured-communications deployment. Everything else —
  registration, governance, training, business continuity, supply chain, effectiveness assessment,
  HR/access/asset management, corrective action, and all six Art. 23 reporting duties — scored
  organizational, since NIS2's Art. 21/23 duties are phrased as policies, processes and reporting
  actions rather than technical properties the system itself must have (a higher organizational
  share than CRA, which had Annex I's lettered technical requirements to draw on).
- `confidence`: narrower band than CRA's (0.8-0.95 vs. CRA's 0.75-0.95). The one 0.8 score is Art.
  21.1's general chapeau duty ("appropriate and proportionate... measures to manage the risks..."),
  the same kind of open-ended chapeau CRA scored lower for (CRA's Art. 20(1) "act with due care").
  Everything sourced from a specific lettered point or a numeric deadline (Art. 23.4a's 24-hour
  early warning, 23.4d's one-month final report) scored 0.9-0.95, since there's no compression
  judgment call in restating "submit X within N hours" as a duty.

## Capability

**Test applied:** same as CRA — what underlying capacity would satisfy this Obligation, independent
of Role or Regulation; mint new only if nothing existing already names that capacity.

- **Direct id reuse from `cra.json`, not just later TF-IDF matching.** Eight of NIS2's 19
  Capabilities reuse CRA's exact node id rather than being minted fresh and left for
  `find_capability_duplicates.py` to catch later: `Cybersecurity Risk Assessment Process`,
  `Secure Development Lifecycle`, `Vulnerability Management`, `Coordinated Vulnerability Disclosure
  Policy`, `Data Encryption`, `Access Control & Authentication`, `Security Incident Reporting`, and
  `Vulnerability Reporting & User Communication`. This extends the single-capability precedent
  CRA's methodology set with `Security Logging` (reused from `policy_system_graph.json`) to a
  whole-extraction practice: before minting, the full CRA capability list was checked, per the
  `nis2-prompt.md` instruction, and reused wherever NIS2's Art. 21(2)/23 measure named the same
  capacity CRA's Annex I already had. `capability_merges.json` stays empty for these pairs because
  there was never a duplicate to merge — the convergence happened at extraction time, not as a
  post-hoc cleanup step.
- **Reused capability names run slightly narrower than how NIS2 uses them.** `Vulnerability
  Reporting & User Communication` was named for CRA's user-facing vulnerability disclosure duty,
  but in NIS2 it's reused for two broader duties — notifying service recipients of *any* significant
  incident (not just a vulnerability) and communicating cyber-threat remedies. The underlying
  capacity (operate a channel to tell users something security-relevant) is genuinely the same, so
  the reuse stands, but the node's `name`/`description` were not narrowed to match either
  regulation's phrasing specifically — a future GDPR extraction reusing this id should expect the
  same slight name/usage mismatch rather than treat the name as a precise scope boundary.
- **Multi-capability fan-out on bundled Requirements**, the flip side of the granularity call
  above: where a single Art. 21(2) lettered point names more than one distinct capacity in one
  clause, the Obligation keeps a single node but gets multiple `REQUIRES` edges instead of being
  artificially split into multiple Obligations. Three cases: 21(2)(e)'s Obligation requires *Secure
  Development Lifecycle*, *Vulnerability Management* **and** *Coordinated Vulnerability Disclosure
  Policy*; 21(2)(i)'s requires *Access Control & Authentication* **and** *Asset & Personnel Security
  Management*; 21(2)(j)'s requires *Access Control & Authentication* **and** *Secure
  Communications*. This is new relative to CRA, where `REQUIRES` was effectively 1:1 — CRA's
  Annex I lettered points were each already single-capacity, so the need never came up.
- **New capabilities were minted only where NIS2 names a capacity CRA has no analogue for**: entity
  registration/contact-detail upkeep with authorities (`Regulatory Registration & Information
  Management`), management-body-level governance and approval of risk measures (`Cybersecurity
  Governance & Oversight`, distinct from the general `Cybersecurity Training & Awareness` used for
  the training duty itself), a broad incident-handling capacity separate from CRA's
  product-specific `Vulnerability Management` (`Incident Handling`), business continuity/disaster
  recovery, supply chain security, control-effectiveness assessment, HR/asset security, secure
  communications infrastructure, and non-compliance remediation.
- **Not split further** to mirror CRA's Annex I granularity: NIS2's Art. 23 reporting cascade (Art.
  23.1a's initial notification, 23.4a's 24-hour early warning, 23.4b's 72-hour notification,
  23.4c's intermediate report, 23.4d's final report, 23.4e's progress/final report for ongoing
  incidents) all `REQUIRES` a single `Security Incident Reporting` Capability rather than one per
  report stage — six different deadlines and document types are the same underlying capacity
  (report incidents to authorities within mandated timeframes), the same "don't fragment a
  multi-stage duty into near-duplicate capabilities" call CRA's methodology made for `Security
  Update Delivery Mechanism` across four provisions.
