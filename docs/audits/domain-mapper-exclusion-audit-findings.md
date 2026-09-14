# Domain Mapper Exclusion Audit — Findings Report (Issue #27)

## Methodology

Two tiers, per regulation, both pure functions over the real adapter's real
`tuple[ExtractionUnit, ...]` (no LLM/IO in the sampler itself) -- see
`select_sample_units` in `tools/domain-mapper/exclusion_audit.py`.

**Tier A -- first 30 units in document order.** Hypothesis, sanity-checked by the live
runs below (CHANGES.md row 6): EU regulations conventionally open with "Subject matter
and scope" and "Definitions" before substantive duties begin, so scope/applicability
exclusions cluster structurally at the start of the document. A document-order prefix
therefore deliberately front-loads exactly the text most likely to produce genuine
scope/applicability zero-candidate units, while also covering a real cross-section of
ordinary substantive articles for baseline comparison.

**Tier B -- up to 10 additional units for CRA (Slices 2-3), tightened to up to 8 for
GDPR/NIS2 in this Slice 4 run (per the orchestrator's corrected Slice 4 budget, below),
keyword-targeted.** Rationale: conditional-permissive constructions ("may be X where/
if/unless <conditions>") are scattered throughout substantive articles, not
front-loaded like scope text -- a document-order prefix alone would likely under-sample
this category. A deterministic, pure regex scan
(`\bmay\b.{0,80}?\b(where|if|unless)\b`, capped at 80 chars of lookahead) selects
further units -- in document order, first match wins, stopping once the tier cap is
reached or the regulation is exhausted (whichever comes first: GDPR's real run hit its
8-match cap; NIS2's real run found only 2 real matches in the rest of the document and
stopped there, short of the cap) -- from the rest of the regulation (excluding anything
Tier A already selected).

**Live-run budget (CHANGES.md row 1, tightened further for this Slice 4 run per the
orchestrator's corrected accounting):** the true cumulative extraction-call count going
into Slice 4 was 42 for CRA (37 persisted in `records/cra.json` + 5 extra real calls
from a prior slice's accidental test re-run, not reflected in the persisted cache
count). Against a 120-call ceiling and a 38-new-call-per-regulation hard cap, this run
actually spent 38 new extraction calls for GDPR (30 Tier A + 8 Tier B, hitting the cap)
and 32 for NIS2 (30 Tier A + only 2 real Tier B regex matches found, short of the cap)
-- 70 new calls total, for a true cumulative total of 112, an 8-call safety margin
against the 120 ceiling, never exceeded. Classification-proposal calls (one per flagged
unit) are separately budgeted, outside this ceiling (CONTEXT.md decision 5).

Still nowhere near the full corpus for any of the three regulations. Every row below is
an AI-proposed, unreviewed classification -- see the Recommendation section's heading.


## Summary (AC-BI-008)

| Regulation | Units processed | Zero-candidate (flagged) | Scope/applicability | Conditional-permissive | Miss | Unclassified | Error |
|---|---|---|---|---|---|---|---|
| CRA | 37 | 18 | 11 | 5 | 2 | 0 | 0 |
| GDPR | 38 | 16 | 11 | 5 | 0 | 0 | 0 |
| NIS2 | 32 | 19 | 12 | 7 | 0 | 0 | 0 |

## Per-unit detail *(AI-proposed, unreviewed)*

### CRA

| Regulation | Citation | Article | Paragraph | Candidates | Status | Flagged | Classification *(AI-proposed, unreviewed)* | Rationale |
|---|---|---|---|---|---|---|---|---|
| CRA | Art. 1 | 1 | 1 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | Article 1 is a scope/objective provision that lays down what the Regulation covers and the related areas of rules and obligations, rather than imposing a standalone operative duty on a regulated actor. |
| CRA | Art. 2(1) | 2 | 1 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | The provision only states the Regulation’s scope by describing which products it applies to, rather than imposing any operative duty on an actor. |
| CRA | Art. 2(2) | 2 | 2 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | Article 2(2) is a scope clause because it states that the Regulation does not apply to certain products already covered by listed Union legal acts. |
| CRA | Art. 2(3) | 2 | 3 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | The sentence states the Regulation’s non-applicability to a defined class of products, which is scope/applicability text rather than an operative duty. |
| CRA | Art. 2(4) | 2 | 4 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | The text is a scope carve-out stating that the Regulation does not apply to equipment within the scope of another Directive, rather than imposing a duty on an actor. |
| CRA | Art. 2(5) | 2 | 5 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | The provision states that the Regulation’s application to certain products “may be limited or excluded” where specified conditions are met, which is a conditional-permissive formulation rather than an operative duty. |
| CRA | Art. 2(6) | 2 | 6 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | Article 2(6) states a scope exclusion by specifying that the Regulation does not apply to certain spare parts, which describes the Regulation’s applicability rather than imposing a duty. |
| CRA | Art. 2(7) | 2 | 7 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | The provision states the Regulation’s non-applicability to certain products, which is a scope/applicability limitation rather than an operative duty. |
| CRA | Art. 2(8) | 2 | 8 | 1 | ok | no | - | - |
| CRA | Art. 3 | 3 | 1 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | Article 3 is entirely a definitions clause beginning “For the purposes of this Regulation,” so it sets the scope and meaning of terms rather than imposing any operative duty. |
| CRA | Art. 4(1) | 4 | 1 | 1 | ok | no | - | - |
| CRA | Art. 4(2) | 4 | 2 | 1 | ok | no | - | - |
| CRA | Art. 4(3) | 4 | 3 | 1 | ok | no | - | - |
| CRA | Art. 4(4) | 4 | 4 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | The text only states that paragraph 3 does not apply to certain safety components covered by other Union harmonisation legislation, which is a scope/applicability limitation rather than an operative duty. |
| CRA | Art. 5(1) | 5 | 1 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | The provision is a permissive exception clause: it says this Regulation shall not prevent Member States from imposing additional cybersecurity requirements, but only where the stated conditions are met. |
| CRA | Art. 5(2) | 5 | 2 | 1 | ok | no | - | - |
| CRA | Art. 6 | 6 | 1 | 1 | ok | no | - | - |
| CRA | Art. 7(1) | 7 | 1 | 2 | ok | no | - | - |
| CRA | Art. 7(2) | 7 | 2 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | This provision defines which categories of products with digital elements fall into classes I and II by listing criteria, so it is scope/classification text rather than an operative duty. |
| CRA | Art. 7(3) | 7 | 3 | 1 | ok | no | - | - |
| CRA | Art. 7(4) | 7 | 4 | 0 | ok | yes | miss *(AI-proposed)* | The text imposes operative duties on the Commission to adopt an implementing act by 11 December 2025 and to do so under the examination procedure, so it is not scope/applicability or a conditional-permissive clause. |
| CRA | Art. 8(1) | 8 | 1 | 4 | ok | no | - | - |
| CRA | Art. 8(2) | 8 | 2 | 3 | ok | no | - | - |
| CRA | Art. 9(1) | 9 | 1 | 4 | ok | no | - | - |
| CRA | Art. 9(2) | 9 | 2 | 1 | ok | no | - | - |
| CRA | Art. 10 | 10 | 1 | 2 | ok | no | - | - |
| CRA | Art. 11 | 11 | 1 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | The provision only specifies the scope of applicability of other chapters of Regulation (EU) 2023/988 to products with digital elements under certain conditions, rather than imposing a standalone duty on an actor. |
| CRA | Art. 12(1) | 12 | 1 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | The provision states that the products “shall be deemed to comply” with specified cybersecurity requirements where conditions (a)–(c) are met, which is a conditional permissive/deeming construction rather than a direct operative duty. |
| CRA | Art. 12(2) | 12 | 2 | 0 | ok | yes | miss *(AI-proposed)* | This text does not merely describe scope or a conditional permission; it states operative rules that the relevant conformity assessment procedure “shall apply” and that certain notified bodies “shall also be competent,” so it should have been extracted as a requirement. |
| CRA | Art. 12(3) | 12 | 3 | 1 | ok | no | - | - |
| CRA | Art. 13(4) | 13 | 4 | 2 | ok | no | - | - |
| CRA | Art. 17(1) | 17 | 1 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | The provision is framed in permissive terms (“ENISA may submit” and “may consider”) and any action is conditioned on relevance and availability, so it is a conditional-permissive construction rather than a duty. |
| CRA | Art. 17(2) | 17 | 2 | 1 | ok | no | - | - |
| CRA | Art. 27(7) | 27 | 7 | 1 | ok | no | - | - |
| CRA | Art. 33(2) | 33 | 2 | 5 | ok | no | - | - |
| CRA | Art. 43(5) | 43 | 5 | 1 | ok | no | - | - |
| CRA | Art. 63(4) | 63 | 4 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | The provision uses the permissive “may exchange” construction, and that permission is expressly conditioned on necessity and confidentiality arrangements, so it is a conditional-permissive text rather than an operative duty. |

### GDPR

| Regulation | Citation | Article | Paragraph | Candidates | Status | Flagged | Classification *(AI-proposed, unreviewed)* | Rationale |
|---|---|---|---|---|---|---|---|---|
| GDPR | Art. 1(1) | 1 | 1 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | The provision is a scope/article-of-object clause stating what the Regulation lays down rules about, not a duty imposed on an actor. |
| GDPR | Art. 1(2) | 1 | 2 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | This sentence states the Regulation’s purpose and scope of protection (“This Regulation protects…”), not an operative duty imposed on a real-world actor. |
| GDPR | Art. 1(3) | 1 | 3 | 1 | ok | no | - | - |
| GDPR | Art. 2(1) | 2 | 1 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | The sentence states the Regulation’s scope by describing what processing it applies to, rather than imposing a duty on an actor. |
| GDPR | Art. 2(2) | 2 | 2 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | This provision is an applicability carve-out stating that the Regulation does not apply to specified categories of personal-data processing, so it describes the Regulation’s scope rather than imposing a duty. |
| GDPR | Art. 2(3) | 2 | 3 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | This provision identifies the regulation that applies to the processing of personal data by Union institutions, bodies, offices and agencies and describes adaptation of other legal acts, which is scope/applicability text rather than an operative duty on a regulated actor. |
| GDPR | Art. 2(4) | 2 | 4 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | The provision only states that this Regulation does not affect the application of Directive 2000/31/EC and its liability rules, which is a scope/interaction clause rather than an operative duty on a regulated actor. |
| GDPR | Art. 3(1) | 3 | 1 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | This provision only states the Regulation’s scope of application by saying it applies to certain processing of personal data in the context of an establishment in the Union, and it does not impose an operative duty on an actor. |
| GDPR | Art. 3(2) | 3 | 2 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | The provision defines the Regulation’s territorial/applicability scope by stating when it applies to processing of personal data, rather than imposing a substantive duty on an actor. |
| GDPR | Art. 3(3) | 3 | 3 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | The sentence only defines the Regulation’s applicability—specifying the processing activities and controller location it applies to—and does not impose a duty on an actor. |
| GDPR | Art. 4 | 4 | 1 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | Article 4 is a definitions clause beginning “For the purposes of this Regulation,” and it defines terms like “personal data,” “controller,” and “processor” rather than imposing any operative duty. |
| GDPR | Art. 5(1) | 5 | 1 | 6 | ok | no | - | - |
| GDPR | Art. 5(2) | 5 | 2 | 1 | ok | no | - | - |
| GDPR | Art. 6(1) | 6 | 1 | 2 | ok | no | - | - |
| GDPR | Art. 6(2) | 6 | 2 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | The provision uses the permissive form “Member States may maintain or introduce” and only allows such action under the stated conditions, rather than imposing a mandatory duty. |
| GDPR | Art. 6(3) | 6 | 3 | 3 | ok | no | - | - |
| GDPR | Art. 6(4) | 6 | 4 | 1 | ok | no | - | - |
| GDPR | Art. 7(1) | 7 | 1 | 1 | ok | no | - | - |
| GDPR | Art. 7(2) | 7 | 2 | 2 | ok | no | - | - |
| GDPR | Art. 7(3) | 7 | 3 | 3 | ok | no | - | - |
| GDPR | Art. 7(4) | 7 | 4 | 1 | ok | no | - | - |
| GDPR | Art. 8(1) | 8 | 1 | 1 | ok | no | - | - |
| GDPR | Art. 8(2) | 8 | 2 | 1 | ok | no | - | - |
| GDPR | Art. 8(3) | 8 | 3 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | This provision limits the reach of paragraph 1 by stating it shall not affect Member States’ general contract law, so it is scope/applicability text rather than a standalone operative duty. |
| GDPR | Art. 9(1) | 9 | 1 | 1 | ok | no | - | - |
| GDPR | Art. 9(2) | 9 | 2 | 1 | ok | no | - | - |
| GDPR | Art. 9(3) | 9 | 3 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | The provision uses permissive language (“may be processed”) conditioned on specific circumstances, so it is a conditional-permissive construction rather than an operative duty. |
| GDPR | Art. 9(4) | 9 | 4 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | The provision is permissive, stating that Member States “may maintain or introduce” further conditions if they choose, rather than imposing a mandatory duty. |
| GDPR | Art. 10 | 10 | 1 | 2 | ok | no | - | - |
| GDPR | Art. 11(1) | 11 | 1 | 1 | ok | no | - | - |
| GDPR | Art. 12(3) | 12 | 3 | 3 | ok | no | - | - |
| GDPR | Art. 15(3) | 15 | 3 | 2 | ok | no | - | - |
| GDPR | Art. 37(4) | 37 | 4 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | The provision is framed as a permission to designate a data protection officer (“may”) that becomes mandatory only in the specified case where Union or Member State law requires it, so it is a conditional-permissive construction rather than a standalone operative duty. |
| GDPR | Art. 41(2) | 41 | 2 | 4 | ok | no | - | - |
| GDPR | Art. 45(1) | 45 | 1 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | The provision uses a conditional-permissive form—personal data transfer "may take place where" the Commission has found adequate protection—and then states the consequence that no specific authorisation is required, rather than imposing a standalone operative duty. |
| GDPR | Art. 48 | 48 | 1 | 1 | ok | no | - | - |
| GDPR | Art. 49(1) | 49 | 1 | 4 | ok | no | - | - |
| GDPR | Art. 54(1) | 54 | 1 | 6 | ok | no | - | - |

### NIS2

| Regulation | Citation | Article | Paragraph | Candidates | Status | Flagged | Classification *(AI-proposed, unreviewed)* | Rationale |
|---|---|---|---|---|---|---|---|---|
| NIS2 | Art. 1(1) | 1 | 1 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | Article 1(1) only states the Directive’s overall purpose and scope—setting out measures to achieve a common level of cybersecurity across the Union—rather than imposing a duty on an actor. |
| NIS2 | Art. 1(2) | 1 | 2 | 4 | ok | no | - | - |
| NIS2 | Art. 2(1) | 2 | 1 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | The provision only defines the Directive’s scope by stating which entities it applies to and excluding Article 3(4) of the Recommendation for this Directive, rather than imposing an operative duty on a regulated actor. |
| NIS2 | Art. 2(2) | 2 | 2 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | The provision states that the Directive applies to certain entities and lists the conditions identifying those entities, which is scope/applicability text rather than an operative duty. |
| NIS2 | Art. 2(3) | 2 | 3 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | The sentence only states the Directive’s scope of application by specifying which entities it applies to, rather than imposing any operative duty on an actor. |
| NIS2 | Art. 2(4) | 2 | 4 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | The sentence defines the Directive’s applicability by stating it applies to entities providing domain name registration services, which is scope text rather than an operative duty. |
| NIS2 | Art. 2(5) | 2 | 5 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | The provision uses the permissive construction “Member States may provide” and is conditional on Member States choosing to extend the Directive’s application to listed entities, so it is not a standalone operative duty. |
| NIS2 | Art. 2(6) | 2 | 6 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | Article 2(6) is a scope/carve-out clause stating the Directive is without prejudice to Member States’ responsibility for national security and essential State functions, rather than imposing an operative duty. |
| NIS2 | Art. 2(7) | 2 | 7 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | Article 2(7) is a scope/exclusion clause stating that the Directive does not apply to certain public administration entities and activities, rather than imposing a duty. |
| NIS2 | Art. 2(8) | 2 | 8 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | The paragraph uses permissive conditional wording (“Member States may exempt…”, “may decide also to exempt…”) rather than imposing a duty, so it fits the conditional-permissive exclusion. |
| NIS2 | Art. 2(9) | 2 | 9 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | The sentence is a conditional exception clause—paragraphs 7 and 8 do not apply if the entity acts as a trust service provider—rather than an operative duty. |
| NIS2 | Art. 2(10) | 2 | 10 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | The sentence expressly states that the Directive "does not apply to entities" meeting a specified scope condition, so it is a scope/applicability clause rather than an operative duty. |
| NIS2 | Art. 2(11) | 2 | 11 | 1 | ok | no | - | - |
| NIS2 | Art. 2(12) | 2 | 12 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | This sentence is a scope/applicability clause stating that the Directive applies without prejudice to listed EU acts, and it does not impose any operative duty on an actor. |
| NIS2 | Art. 2(13) | 2 | 13 | 3 | ok | no | - | - |
| NIS2 | Art. 2(14) | 2 | 14 | 5 | ok | no | - | - |
| NIS2 | Art. 3(1) | 3 | 1 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | This provision defines, for the purposes of the Directive, which entities are to be considered “essential entities,” so it is scope/definition text rather than an operative duty. |
| NIS2 | Art. 3(2) | 3 | 2 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | This provision is definitional/classificatory for the Directive’s own scope, stating which entities are to be considered “important entities,” rather than imposing an operative duty on a regulated actor. |
| NIS2 | Art. 3(3) | 3 | 3 | 2 | ok | no | - | - |
| NIS2 | Art. 3(4) | 3 | 4 | 1 | ok | no | - | - |
| NIS2 | Art. 3(5) | 3 | 5 | 1 | ok | no | - | - |
| NIS2 | Art. 3(6) | 3 | 6 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | The provision is permissive and conditional, using "may notify" only "upon request of the Commission" and before a date, so it does not impose a mandatory duty. |
| NIS2 | Art. 4(1) | 4 | 1 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | The provision is a conditional non-applicability clause: where certain sector-specific Union legal acts meet specified conditions, the Directive’s provisions “shall not apply” to those entities. |
| NIS2 | Art. 4(2) | 4 | 2 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | This provision is a conditional equivalence clause stating that the requirements in paragraph 1 are considered equivalent where specified conditions in points (a) or (b) are met, rather than imposing a direct operative duty. |
| NIS2 | Art. 4(3) | 4 | 3 | 3 | ok | no | - | - |
| NIS2 | Art. 5 | 5 | 1 | 0 | ok | yes | correct_exclusion_conditional_permissive *(AI-proposed)* | The provision is a permissive carve-out: it says the Directive shall not prevent Member States from adopting or maintaining higher cybersecurity provisions, provided a condition is met, rather than imposing a standalone duty. |
| NIS2 | Art. 6 | 6 | 1 | 0 | ok | yes | correct_exclusion_scope_applicability *(AI-proposed)* | Article 6 consists entirely of definitions introduced by “For the purposes of this Directive” and “means,” so it is scope/definition text rather than an operative duty. |
| NIS2 | Art. 7(1) | 7 | 1 | 9 | ok | no | - | - |
| NIS2 | Art. 7(2) | 7 | 2 | 10 | ok | no | - | - |
| NIS2 | Art. 7(3) | 7 | 3 | 1 | ok | no | - | - |
| NIS2 | Art. 9(5) | 9 | 5 | 2 | ok | no | - | - |
| NIS2 | Art. 17 | 17 | 1 | 1 | ok | no | - | - |

## Recommendation (AC-BI-009)

**Provisional recommendation — pending human review of the AI-proposed classifications above (see user decision 2, issue #27 context)**

Open a follow-up issue to narrow the prompt: the AI-proposed classifications observed 2 miss(es) across the audited sample.
