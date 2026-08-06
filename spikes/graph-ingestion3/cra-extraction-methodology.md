# CRA Extraction Methodology

How `cra.json` was derived from `spikes/eu-regulations/CRA.md`. This documents the actual
determination criteria applied during extraction — the judgment calls, not just the target
schema (that's in `docs/artifacts/ps-domain-concepts.md`). Written after a first pass under-
extracted (18 requirements) by clustering distinct duties together; the criteria below are what
replaced that pass and produced the current 74/78 requirement/obligation count.

---

## Role

**Test applied:** an actor type the Regulation itself names and assigns duties to — not any
noun that appears in the text.

- Sourced from Article 3 (Definitions), not from the obligation articles themselves. `DEFINES`
  edge `source_ref` points at the defining clause (e.g. `Art. 3(13)` for Manufacturer), even
  though the *substantive* duties for that role live in later articles (Art. 13–14 for
  Manufacturer). The definition location and the duty location are different things; only the
  former goes on `DEFINES`.
- A role gets minted even without an explicit Art. 3 entry if the Regulation's operative text
  clearly creates a duty-bearing actor category — e.g. **Substantial modifier** (Art. 22(1))
  isn't in the Art. 3 glossary but is unambiguously a distinct actor subject to obligations, so
  it became a sixth Role with `defines_ref: "Art. 22(1)"`.
- Not minted: "economic operator" (Art. 3(12)). It's an umbrella term covering four roles
  already modeled separately (Manufacturer, Authorised representative, Importer, Distributor),
  not a fifth actor with its own duties. Where the Regulation imposes a duty on "economic
  operators" collectively (Art. 23), that duty was expanded into one Obligation per underlying
  Role rather than invented a node for the umbrella term.

## Requirement

**Test applied:** does this span of text impose a distinct "shall" duty that isn't just another
sentence restating a duty already captured elsewhere?

**Granularity — paragraph/sub-point, not article:**
Default unit is the numbered paragraph. Split *below* paragraph level when a single paragraph
visibly bundles unrelated duties — e.g. Art. 13(8) reads as one paragraph but contains three
independent duties (handle vulnerabilities during the support period; determine and document
the support period itself; maintain a coordinated vulnerability disclosure policy), so it became
three Requirements (`13.8a`, `13.8b`, `13.8c`). Conversely, *merge* adjacent paragraphs only when
they're mechanically one duty split across sentences by the source formatting — e.g. Art. 13(2)
("undertake a risk assessment") and 13(3) ("document and keep it updated") are one duty told in
two sentences, not two duties, so they became a single Requirement (`13.2`).

**Inclusion filter:**
- Included: any paragraph with an operative "shall."
- Excluded — permissive, not mandatory: Art. 13(10)-(11)'s "manufacturer *may* maintain public
  archives..." is an optional compliance path, not a duty. (13(11) was kept in modified form
  because it embeds a real conditional "shall": *if* the manufacturer exercises that option, it
  *shall* inform users of the risk — the conditional duty is what got extracted, not the option
  itself.)
- Excluded — institutional/procedural, not duty-imposing on an economic operator: Commission
  delegated/implementing act powers (Art. 13(24)-(25), 14(9)-(10)), committee procedure, CSIRT/
  ENISA/ADCO internal mechanics, submission-routing rules that specify *how* to notify rather
  than *whether* (Art. 14(7)), and pure definitions (Art. 14(5)'s definition of "severe"
  incident — used to interpret Art. 14(3)'s duty, not a duty itself).
- Excluded to avoid restating the same duty twice: Annex I, Part II, point (5) (the CVD-policy
  clause) is not a separate Requirement because Art. 13(8) already expresses that exact clause
  by direct cross-reference ("policies... including coordinated vulnerability disclosure
  policies, referred to in Part II, point (5), of Annex I"). One Requirement (`13.8c`) covers
  both locations; the merge is called out in the `_comment` field so it's traceable rather than
  silently dropped.
- Two provisions that look similar are **not** treated as duplicates if they operate at
  different levels of the same structure: Art. 13(1) ("comply with Part I of Annex I") is the
  general chapeau obligation; Annex I Part I points (a)–(m) are the thirteen specific technical
  requirements it invokes. Both were kept — the chapeau plus all thirteen specifics — because
  collapsing them would lose the technical substance (encryption, access control, logging,
  etc.) that the Capability layer needs to attach to.

**Identity/`source_ref`:** `{REG}_req_art_{article}.{paragraph}[letter]`, with the human-readable
citation (e.g. `Art. 13(8), fifth paragraph; Annex I, Part II, point (5)`) carrying any merge or
cross-reference detail that the ID alone can't.

## Obligation

**Test applied:** what is the canonical, generically-phrased duty this Requirement establishes,
and which single Role does it attach to?

- Text is a short imperative duty statement derived from (not quoted verbatim from) the
  Requirement text — e.g. Requirement text "Manufacturers shall ensure that a product... is
  designed, developed and produced in accordance with..." becomes Obligation text "Ensure Secure
  Product Design and Development."
- **Hard constraint, discovered the hard way:** Obligation is 1:1 with Role (`HAS` is exactly one
  inbound edge per Obligation per the domain model), and because Obligation identity is a hash of
  its text, *identical text for two different roles collides into the same node* — silently
  merging two distinct role-specific duties into one. This happened on the first full pass:
  Manufacturer (Art. 13(22)), Importer (Art. 19(7)) and Distributor (Art. 20(5)) all got the
  literal text "Cooperate with Market Surveillance Authority Requests," and Importer/Distributor
  also collided on "Notify Manufacturer Cessation of Operations to Authorities and Users." Fix
  applied throughout: whenever two roles carry what the Regulation phrases as the same duty,
  the Obligation text is made role-specific ("...as Manufacturer" / "...as Importer" /
  "...as Distributor") purely to keep the nodes distinct — the underlying duty is still the same
  in substance, which is exactly what the shared target `Capability` (e.g. `Regulatory
  Cooperation`) is for.
- **Fan-out case:** where one Requirement is satisfied by several role-specific Obligations at
  once (Art. 23's traceability duty applies identically to Manufacturer, Authorised
  representative, Importer and Distributor; Art. 21's deeming provision applies to both Importer
  and Distributor), one Requirement node gets multiple `SATISFIED_BY` edges to multiple
  Obligation nodes, one per Role — not one Obligation shared across roles.
- `obligation_type` (`technical` vs `organizational`): technical if the duty is about a property
  or mechanism the product itself must have (encryption, logging, access control); organizational
  if it's about a process the responsible party must run (documentation, reporting, verification,
  cooperation).
- `confidence`: not a measure of extraction certainty in the LLM-parsing sense (there's no
  ambiguity in what the text says) but of how directly the Requirement text maps to the
  Obligation's canonical phrasing. Near-verbatim, single-duty paragraphs scored 0.9–0.95;
  paragraphs requiring more paraphrase or judgment to compress into one duty statement (e.g.
  bundling "act with due care" from Art. 20(1), a fairly open-ended chapeau) scored lower
  (0.75–0.85).

## Capability

**Test applied:** what underlying capacity, independent of *who* holds the duty or *which*
regulation imposes it, would satisfy this Obligation? Mint a new Capability only if no existing
one already names that capacity.

- Capabilities were **not** created one-per-Obligation. Before minting a new one, every new
  Obligation was checked against the existing Capability list for a fit. This is what produces
  convergence: `Conformity Assessment & Certification` is `REQUIRES`'d by ten different
  Obligations across Manufacturer, Importer and Distributor, because "assess/verify/attest
  conformity" is the same capacity regardless of which role's process triggers it.
- Capabilities were split when Requirement text was specific enough to name a distinct technical
  capacity — this is why Annex I Part I's thirteen lettered points became thirteen separate
  Capabilities (`Access Control & Authentication`, `Data Encryption`, `Attack Surface
  Minimisation`, ...) rather than one generic "Product Security" bucket. A single vague
  Capability would have been easier to write but would have thrown away exactly the information
  a Policy/Standard/Control layer would need later to know *what* to govern.
- One Capability, `Security Logging`, deliberately **reuses the exact node id**
  (`cap_security_logging_c4d9e2`) from `policy_system_graph.json`'s worked example rather than
  minting a new one — same name, same underlying capacity (Annex I Part I point (l): "recording
  and monitoring relevant internal activity"), so it's the same canonical node if the two files
  are ever loaded into the same graph. This is the cross-regulation convergence the domain model
  describes, made concrete rather than asserted.
- Capabilities were **not** split just because two Obligations differ in role or article. Art.
  13(9) (retain issued security updates ≥10 years), Annex I Part I(c) (update delivery
  mechanism with opt-out), and Annex I Part II(7)-(8) (secure/timely update distribution and
  dissemination) are four different provisions but the same capacity — the ability to get
  security updates to users reliably — so all four `REQUIRES` the single `Security Update
  Delivery Mechanism` Capability instead of four near-duplicate ones.
