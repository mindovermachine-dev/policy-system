# GDPR Extraction Methodology

How `gdpr.json` was derived from `docs/regulations/gdpr.md`, per the scope in `gdpr-prompt.md`
(Art. 4 role-sourcing only; Chapter II Art. 5-11; Chapter III Art. 12-22 plus Art. 23 only if it
imposed a direct duty; Chapter IV Section 1 Art. 24-31; Section 2 Art. 32-34; Section 3 Art. 35-36;
Section 4 Art. 37-39; and Chapter V Art. 44-49). Written after the fact, following the precedent set
by `cra-extraction-methodology.md` and `nis2-extraction-methodology.md`, to record the judgment
calls actually applied.

Built with a generator script (not hand-authored JSON) given the scale: 179 Requirements, 213
Obligations, 5 Roles and 42 Capabilities (27 newly minted, 15 reused from `cra.json`/`nis2.json`).

---

## Role

**Test applied:** same as CRA/NIS2 — an actor type the Regulation itself names and assigns duties to.

- **Controller** (`DEFINES` `Art. 4(7)`) and **Processor** (`DEFINES` `Art. 4(8)`) are the two
  duty-bearing roles Art. 4 itself defines.
- **Joint controller** is minted as its own Role rather than folded into Controller: Art. 26(1)
  imposes a distinct arrangement duty (transparently determine respective responsibilities via an
  arrangement between the joint controllers) that is not simply "Controller, but two of them" — the
  same call CRA's methodology made minting Substantial modifier outside the Art. 3 glossary. `DEFINES`
  points at Art. 26(1), the clause that both defines and imposes the duty (Art. 4 has no separate
  "joint controller" definition point).
- **Representative** (`DEFINES` `Art. 4(17)`, the defining clause) has its substantive duties in Art.
  27, matching CRA's pattern of pointing `DEFINES` at the definition location rather than the duty
  location.
- **Data Protection Officer** is minted as its own Role, not folded into an Obligation on
  Controller/Processor: Art. 38-39 impose duties directly on the DPO (secrecy, cooperating with the
  supervisory authority, task performance) that are distinct from the Controller/Processor's own duty
  to designate one (Art. 37) and to support/protect that designee (Art. 38(1)-(3), (6)). Art. 4 has no
  DPO definition, so `DEFINES` points at Art. 37(1), the clause that first introduces the role — the
  same treatment CRA gave Substantial modifier (`defines_ref` outside Art. 3) and NIS2 gave Essential/
  Important entity's precise sourcing.
- **Not minted: Recipient, Third party** (Art. 4(9)-(10)). These are defined terms describing data
  flows the Regulation's other actors interact with, not actors the Regulation itself assigns duties
  to — the same distinction CRA drew for "economic operator" as an umbrella term rather than a
  fifth Role.
- **Not minted: Data subject.** Data subjects hold rights, not duties. Every Chapter III "the data
  subject shall have the right to X" was read for the duty it actually creates on Controller (or
  Processor, via Art. 28(3)(e)) to fulfil that right — e.g. Art. 16's "right to rectification" became
  the Controller Obligation "Rectify and Complete Inaccurate or Incomplete Personal Data Without
  Undue Delay." This is the same "extract the duty that actually lands on a duty-bearing actor"
  principle NIS2's methodology applied to Member-State-phrased duties that actually land on
  essential/important entities.

## Requirement

**Test applied:** same as CRA/NIS2 — does this span of text impose a distinct duty, once
rights-language and permissive/institutional wrapping are stripped down to the duty that actually
lands on Controller/Processor/Joint controller/Representative/DPO?

**Granularity — paragraph/lettered-point, extended further than CRA/NIS2 needed to go:** the prompt
was explicit that full lettered-point splitting applies broadly, not only to the four provisions it
named as the provisions where this "matters most" (Art. 5(1), 28(3), 32(1), 35(7)). Applied
consistently, this also produced full splits of Art. 13(1)/(2), 14(1)/(2), 15(1), 30(1)/(2), 36(3)
and 47(2) — each a "content checklist" for a single document/notice/record, structurally the same
shape as Art. 28(3)'s contract-clause list and Art. 35(7)'s DPIA-content list, which the prompt
explicitly required split. This is a materially larger multiplier than CRA or NIS2 needed (Art. 47(2)
alone contributes 14 lettered Requirements, each producing 2 Obligations).

**Disjunctive vs. cumulative lettered lists — the one place full splitting was *not* applied:**
lettered points that are alternative (disjunctive) conditions for a single duty — "at least one of
the following applies" / "shall take place only on one of the following conditions" — were kept as
one Requirement with the alternatives folded into the text as elaboration, not split into competing
Requirements. This is a distinct pattern from the "content checklist" case (where every lettered
point applies cumulatively) and covers: Art. 6(1)'s six lawful bases, Art. 9(2)'s ten special-category
exceptions, Art. 17(1)'s six erasure grounds, Art. 18(1)'s four restriction grounds, Art. 22(2)'s
three automated-decision exceptions, Art. 46(2)'s six no-authorisation safeguard mechanisms, and Art.
49(1)'s seven transfer derogations. Splitting these would have manufactured seven competing
"Requirements" for what is legally a single "satisfy one of the following" duty.

**Inclusion filter:**
- Included: any paragraph or lettered point with an operative "shall" landing on Controller,
  Processor, Joint controller, Representative or DPO.
- Excluded — permissive, not mandatory: Art. 7(4) (a factor for assessing consent validity, no
  "shall" on the controller), Art. 12(6)-(7) (controller *may* request ID confirmation / use icons),
  Art. 21(5) (data subject *may* object by automated means), Art. 24(3)/25(3)/28(5)/32(3)/35(8)
  (codes of conduct/certification *may* be used as an element of demonstrating compliance) — the
  same "may" exclusion CRA's methodology applied to Art. 13(10)-(11)'s public-archive option, applied
  here across every Chapter IV "adherence to a code of conduct may be used as an element" clause.
- Excluded — institutional/procedural or Member-State-legislative, not duty-imposing on an entity:
  Art. 6(2)-(3) (the legal basis for (c)/(e) processing must be laid down by Union/Member State law —
  a legislative duty, not an entity duty, the same exclusion NIS2 applied to transposition-phrased
  provisions), Art. 35(4)-(6) (supervisory authority list-publishing), Art. 36(2)/(4)-(5) (supervisory
  authority response duties; Member States shall consult during legislative preparation; Member
  State law may require prior authorisation), Art. 45 in its entirety (Commission adequacy-decision
  mechanics — the one clause touching Controller/Processor is permissive, not a duty), and all of
  Chapter IV Section 5 (Art. 40-43: codes of conduct and certification, voluntary compliance paths).
- **Art. 23 excluded in its entirety**, per the prompt's explicit instruction: it only grants Member
  States a legislative option to restrict the scope of Chapter III/Art. 5 rights and obligations; it
  never itself imposes a direct duty on Controller or Processor.
- Excluded — exemption/scope-limiting clauses paired with a duty already captured: Art. 9(4),
  13(4), 14(5), 17(3), 20(3)-(4), 27(2), 30(5), 34(3)-(4), 37(2)-(3)/(6), 38(4), 49(3)-(5). Each of
  these describes when a duty *doesn't* apply or clarifies its boundary rather than adding a new one,
  the same treatment CRA gave Art. 13(10)'s optional path and penalty-scoping clauses.
- Excluded to avoid restating the same duty twice: Art. 26(3) (data subjects may exercise rights
  against each joint controller irrespective of their arrangement) restates Chapter III duties already
  captured rather than adding a new one; Art. 27(3) (representative's required location) and Art.
  34(2) (breach-communication content, a direct cross-reference to Art. 33(3)(b)-(d)) are folded into
  the Requirement they elaborate, with the merge noted in `_comment`.

**Identity/`source_ref`:** `GDPR-1.0_req_art_{article}.{paragraph}[letter]`, matching CRA/NIS2's
scheme (`{REG}_req_art_...`).

## Obligation

**Test applied:** same as CRA/NIS2 — the canonical duty a Requirement establishes, attached to
exactly one Role.

- **Same role-suffix fix CRA/NIS2 needed, applied throughout Chapter IV/V.** Every provision
  addressed to "the controller or the processor" (Art. 27(1), 28(3) chapeau, 28(9), 30(3)-(4), 31,
  32(1)/(4), 37(1)/(5)/(7), 38(1)-(3)/(6), 44, 46(1)/(3), 47(1)-(2), 48, 49(6)) mints two Obligation
  nodes with role-specific suffixes ("...as Controller" / "...as Processor"), even where the
  underlying duty text is identical — the same fix CRA's methodology required for Manufacturer/
  Importer/Distributor collisions and NIS2's for Essential/Important entity collisions.
- **A second, distinct duplicate-text case, resolved differently: same Role, same text, two
  Requirements.** Art. 13(2) and Art. 14(2) — the "further information" lists for, respectively, data
  collected from and not collected from the data subject — genuinely restate five identical
  disclosure duties (storage period, existence of rights, consent-withdrawal right, complaint right,
  automated-decision-making information), differing only in which collection scenario triggers them.
  Minting separate Obligation nodes per Requirement here would have produced five pairs of nodes with
  literally identical `text`, an accidental-duplication smell distinct from CRA/NIS2's cross-role
  collision. Since the duty and the Role are both actually identical (unlike the cross-role case,
  where the Role differs and merging would wrongly collapse two entities' duties into one), these
  five pairs were **merged**: one Obligation node, two `SATISFIED_BY` edges (one from the Art. 13(2)
  Requirement, one from the matching Art. 14(2) Requirement). This is the mirror image of NIS2's
  one-Requirement-to-many-Obligations fan-out (Art. 23's simultaneous Essential/Important entity
  duties), applied here as many-Requirements-to-one-Obligation.
- `obligation_type`: same test as CRA/NIS2 (technical = a property/mechanism; organizational = a
  process). Technical: Art. 8(2)'s child-consent age verification, Art. 25(1)-(2)'s design/default
  measures, and Art. 32(1)(a)-(c)/(4)'s pseudonymisation, encryption, system-resilience and
  incident-recovery duties. Everything else — consent management, transparency and access-request
  handling, rectification/erasure/restriction/portability execution, DPIA, prior consultation, DPO
  designation and tasks, records of processing, breach notification, and international-transfer
  governance — scored organizational, since GDPR's Chapters II-V are overwhelmingly phrased as legal/
  procedural duties rather than product or system properties (a lower technical share than CRA, which
  had Annex I's lettered technical requirements to draw on, and closer to NIS2's Art. 21/23 mix).
- `confidence`: narrower band than CRA's (0.75-0.95), similar to NIS2's (0.8-0.95). Lower scores
  (0.75-0.8) went to open-ended chapeau duties requiring real compression judgment — Art. 24(1)'s
  "implement appropriate... measures to ensure and demonstrate" compliance, Art. 32(1)'s risk-based
  chapeau, Art. 35(9)'s "where appropriate" qualifier — the same kind of chapeau CRA and NIS2 both
  scored lower (CRA's Art. 20(1) "act with due care," NIS2's Art. 21(1) general chapeau). Provisions
  with a specific numeric deadline or an enumerated, near-verbatim content list (Art. 33(1)'s 72-hour
  breach notification, the Art. 13/14/15/30/36(3)/47(2) content-checklist points) scored 0.85-0.95.

## Capability

**Test applied:** same as CRA/NIS2 — what underlying capacity would satisfy this Obligation,
independent of Role or Regulation; mint new only if nothing existing already names that capacity.

- **15 of GDPR's 42 Capabilities reuse exact node ids from `cra.json`/`nis2.json`**, checked against
  the full capability list of both files before minting, per the prompt's convergence instruction:
  `Data Minimisation`, `Access Control & Authentication`, `Data Encryption`, `Data & Configuration
  Integrity Protection`, `Availability & Resilience`, `Cybersecurity Risk Management Program`,
  `Business Continuity & Disaster Recovery`, `Security Control Effectiveness Assessment`, `Asset &
  Personnel Security Management`, `Security Incident Reporting`, `Incident Handling`, `Vulnerability
  Reporting & User Communication`, `Regulatory Cooperation`, `Compliance Documentation Management`,
  and `Secure Data Removal & Portability`. `capability_merges.json` stays untouched for these —
  convergence happened at extraction time, the same practice NIS2's methodology established.
- **Where the prompt named the intended convergence but the closest match required judgment:**
  Art. 30's records of processing activities reuse `Compliance Documentation Management` (from CRA)
  rather than minting a GDPR-specific "records" capability — the underlying capacity (create, retain
  and make available a compliance record to an authority on request) is the same one CRA's Art. 31/
  32 technical-documentation duty names, even though CRA's description is phrased around "market
  surveillance authorities" rather than "supervisory authorities." The same reuse was extended to
  Art. 28(3)(h)'s audit-cooperation duty and Art. 33(5)/49(6)'s breach/transfer documentation duties.
  Art. 32(1)(b)'s "confidentiality, integrity, availability and resilience of processing systems"
  fans out across three reused Capabilities (`Access Control & Authentication`, `Data & Configuration
  Integrity Protection`, `Availability & Resilience`) rather than one bundled node, mirroring NIS2's
  multi-capability fan-out for Art. 21(2)(i)/(j).
- **27 new Capabilities minted** where GDPR names a capacity with no CRA/NIS2 analogue — almost all
  privacy-specific rather than security-specific, reflecting that GDPR's scoped chapters are mostly
  about lawful processing and data-subject rights rather than cybersecurity: `Consent Management`,
  `Data Subject Rights Fulfilment & Communication` and `Data Subject Rights Execution` (split the
  same way CRA split Annex I's technical points — "tell the data subject something" vs. "do the
  thing the data subject asked for" are different capacities), `Data Protection Impact Assessment`,
  `Data Protection Officer Management`, `International Data Transfer Governance`, `Binding Corporate
  Rules Governance`, and others detailed in the capability registry.
- **Not split further**, mirroring CRA/NIS2's "don't fragment a multi-stage duty" rule: all 28
  Art. 13/14/15 "information to provide" lettered Requirements `REQUIRES` a single `Data Subject
  Rights Fulfilment & Communication` Capability rather than one per disclosure item — 28 different
  content items are the same underlying capacity (transparently communicate processing information
  to data subjects), the same call CRA made for `Security Update Delivery Mechanism` and NIS2 made
  for `Security Incident Reporting`. Similarly, all 14 Art. 47(2) BCR-content Requirements `REQUIRES`
  a single `Binding Corporate Rules Governance` Capability, and Art. 29's instruction-only-processing
  duty reuses `Processor Contract Management` (minted for Art. 28(3)(a)) rather than a near-duplicate.
