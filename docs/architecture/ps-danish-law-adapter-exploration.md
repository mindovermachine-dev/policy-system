<!-- © 2026 Cartman ApS. All rights reserved. -->
# Policy System — Danish Law Adapter Exploration (Retsinformation / ELI)

**Status:** Exploration record, 2026-10-24. **Not canonical.** Once the sub-issues of the umbrella
issue ship, `ps-domain-concepts.md`, `ps-solution-architecture.md` and
`ps-service-container-architecture.md` are the source of truth and this document is history.
Each sub-issue carries a check that this document is kept in sync (see section 10).

Decisions are tagged **[CONFIRMED]** (stated by the user in the exploration conversation) or
**[PROPOSED]** (recommended by the assistant, not yet explicitly confirmed). Facts are tagged
**[VERIFIED]** (read from an official page or a live response on 2026-10-24) or
**[UNVERIFIED]** (secondhand, inferred, or single-source). Section 8 lists what is unverified.

---

## 1. Intent and Objective

Add an Ingestion Adapter for **Danish legislation**, sourced from Retsinformation (the Danish
state legal information system) and its ELI (European Legislation Identifier) interface.

**Why:** Compliance officers face Danish national law as well as EU law, and look for Roles,
Requirements and Obligations in it.

**Not a goal:** French or German adapters. Those jurisdictions were researched only to verify
that the conceptual model is not shaped by Danish specifics (section 4).

---

## 2. Decisions

| ID | Decision | Status |
|---|---|---|
| D-1 | Danish national law is ingested as a source of Roles, Requirements and Obligations in its own right, not only as a link target of EU directives. | CONFIRMED |
| D-2 | The link from a Danish act to an EU directive is **user-supplied first**. Where the source metadata names a directive, the system **proposes** the link as a pending review in the near-miss family; the Compliance Officer confirms, rejects, or establishes a link manually (including "none"). Nothing is linked silently. | CONFIRMED |
| D-3 | The instrument is **written first as unlinked**; the `TRANSPOSES` edge is attached on confirmation. | CONFIRMED |
| D-4 | National law with no directive link needs a **new instrument type** (`national_law`). | CONFIRMED |
| D-4a | `national_transposition` is folded into `national_law`: the edge, not the type, says "this transposes a directive". `TRANSPOSES` becomes optional on `national_law`. Identity form `{SHORT}-{JURISDICTION}-{VERSION}` is unchanged. Rationale: write-first (D-3) means the type cannot change when a link is confirmed later. Reopens the #40 enum decision; #46's "exactly one `TRANSPOSES`" criterion no longer applies. | CONFIRMED |
| D-9 | Old issues #41 and #46 are **lifted into new issues and closed** (after the new issues exist and the user approves each closure). Non-Danish-specific scope is carried explicitly (section 9). | CONFIRMED |
| D-5 | Unit of ingestion: the **original act when no consolidated law (LBK) exists, and LBKs**. Each new LBK supersedes the previous one one-to-one via `SUPERSEDED_BY`. Amending acts are **not extracted**; an instrument with unconsolidated amendments is flagged "amendment pending". A visible flag is sufficient for the Compliance Officer. | CONFIRMED |
| D-6 | When the user names an act that is historic (consolidated by a later LBK), the adapter **stops and asks** which to ingest, offering the current LBK as the likely target. Nothing is ingested until answered. | CONFIRMED |
| D-7 | PS also surfaces **new Danish laws relevant to what is tracked**, as a candidate list the Compliance Officer decides on. Never auto-ingest. Relevance signals are the assistant's proposal (section 6.5). | CONFIRMED (intent) / PROPOSED (signals) |
| D-8 | **Executive orders (bekendtgørelser, BEK) are in the first slice.** The delegation relation is modelled generically (not Danish-specific) so that EU delegated/implementing acts and German/French equivalents fit the same edge. | CONFIRMED |
| S | Synthesis of the three proposals (section 5): source-neutral `proposed_relations` on the adapter result, new `BASED_ON` edge, review flow generalised from near-miss. | PROPOSED |

---

## 3. Source Facts (Retsinformation)

All **[VERIFIED]** unless marked.

### 3.1 Licence and terms

Source: `retsinformation.dk/api` (Civilstyrelsen).

- Free, non-exclusive, unlimited right to use the open data, commercial use included; copying,
  distribution, modification and combination with other material are permitted.
- Laws and regulations are not subject to copyright (ophavsretslovens § 9).
- Conditions: the data must not be used so as to suggest Civilstyrelsen endorses the user or its
  products; the user is responsible for its own setup, with no support; use must not load the
  interface disproportionately; Civilstyrelsen may change the extraction interface.

### 3.2 Document access

- Canonical ELI: `/eli/lta/{year}/{number}` (`lta` = Lovtidende A). Other `pubMedia` values:
  `retsinfo` (non-promulgated decisions), `fob` (Ombudsman), `ft` (Folketing, by accession number),
  and `mt` (seen in metadata links; not documented in the template page).
- Alias `/eli/accn/{accession}`; citation aliases such as `/eli/regel/lbk/2015/03/18/261`.
- Format suffix: `/XML`, `/PDF`, `/RAWHTML`; default HTML. Language suffix: `dan` only.
- Metadata suffix: `.json` (JSON-LD), `.ttl`, `.rdf` on any canonical or non-partial alias URI.
- Reverse EU lookup: `/eli/eu/{CELEX}` redirects to `retsinformation.dk/documents?ce={CELEX}`.
- Channels: ELI sitemap `/eli/sitemap.xml` (`loc` + `lastmod`, refreshed at least monthly); ELI Atom
  feed `/eli/eli-update-feed.atom` (at least 60 days of history, daily).
- Several pages are single-page-application shells: a plain HTTP fetch returns only a title.
  Content pages and `.json` metadata respond normally; the technical documentation requires
  JavaScript rendering.

### 3.3 Harvest API

Source: official OpenAPI document at `api.retsinformation.dk/swagger/v1/swagger.json`.

- `GET /v1/Documents?date=` returns JSON: `documentId`, `accessionsnummer`, `reasonForChange`
  (`Unknown`, `DocumentMetadataChanged`, `DocumentContentChanged`,
  `DocumentMetadataChangedAndDocumentContentChanged`, `NewDocument`, `RemovedDocument`),
  `changeDate`, `documentType` (`shortName`, `id`), `href` (ELI accn URL), `images`.
- Open 03:00–23:45 Danish time; limited to one call per 10 seconds; `date` must be within the
  last 10 days; HTTP 400 outside hours or on an invalid date; 429 on rate limit.
- Intended for nightly harvesting; the client must monitor and keep itself current.

### 3.4 ELI metadata (JSON-LD) properties observed

Observed on `lta/2018/502`, `lta/2024/289`, `lta/2025/434`, `lta/2014/881`, `lta/2000/530`:
`title`, `title_alternative` (popular name), `title_short`, `type_document` (for example `LOVH`,
`BEKH`), `number`, `date_document`, `date_publication`, `date_no_longer_in_force`, `in_force`,
`id_local` (accession number), `responsibility_of` (ministry), `published_in`, `changes`,
`changed_by`, `consolidates`, `consolidated_by`, `based_on`, `basis_for`, `transposes`,
`countersigned_by`, and format/embodiment links.

- `transposes` on the NIS2 act (`lta/2025/434`) holds
  `http://data.europa.eu/eli/dir/2022/2555/oj`, convertible to CELEX `32022L2555`.
- `lta/2018/502` (Data Protection Act) has `consolidated_by lta/2024/289` and `in_force` =
  not-in-force; it is historic. It has no `transposes`, because it supplements a regulation.
- `lta/2014/881` and `lta/2000/530` (executive orders) have `based_on lta/2018/502`; the law's
  `basis_for` lists its dependent documents (inverse).

### 3.5 LexDania 2.1 XML

Observed from `lta/2018/502/xml` (one document, truncated at 100 000 of about 690 000 characters).

- Root `<Dokument>` (no namespace; `SchemaLocation` attribute). Children `Meta`, `TitelGruppe`,
  `DokumentIndhold`.
- `Meta`: `DocumentType`, `Rank`, `AccessionNumber`, `DocumentId`, `Number`, `Year`, `DiesSigni`,
  `DiesEdicti`, `DateOfHistoricMark`, `Status` (observed `Historic`), `Ministry`, amendment
  `Change` entries with `Ref_Accn`/`Ref_Af`/`Ref_Text`. Some dates are indirect via `REFid`.
- Body hierarchy: `Bog > Afsnit > Kapitel > ParagrafGruppe > Paragraf (§) > Stk > Index`, with
  text in `Linea > Char`, and `Nota formaNo="EU"` footnotes.
- No dedicated transposition field in the XML; the EU reference is a footnote and § 1 text. The
  structured link is in the ELI metadata (3.4).

---

## 4. Delegated-Legislation Research (shape verification)

The relation "delegated instrument depends on an enabling instrument" exists in every jurisdiction
examined but is exposed very differently. Purpose: verify that the PS edge is not Danish-specific.

| Jurisdiction | How the link is exposed | Granularity | Status |
|---|---|---|---|
| Denmark | ELI `based_on` on the order; `basis_for` on the law | Instrument | VERIFIED |
| EU | ELI RDFa on the DORA RTS (2024/1773) shows `based_on` = TFEU only. Cellar CDM `resource_legal_based_on_resource_legal` links it to CELEX 32022R2554 (DORA). The DORA page does not list `basis_for` its RTS. | Instrument | VERIFIED (one delegated act) |
| France | No structured link found in sources read. The *baromètre de l'application des lois* dataset (LexImpact; Open Licence 2.0; laws since 2002) lists per law each application measure: article, base law amended, DILA observation, published decree id, planned date, state (applied / not applicable / pending). | Article | VERIFIED (dataset page); link semantics in LEGI UNVERIFIED |
| Germany | Text only: the enabling clause (Eingangsformel) of BSI-KritisV states issuance under "§ 10 Absatz 1" of the BSI-Gesetz; no hyperlink to the parent. | Section | VERIFIED (one regulation, HTML) |

ELI ontology definitions (OWL, paraphrased): `based_on` — a work empowered by another (constitution,
treaty, enabling act); `basis_for` — inverse, primary law typically underpinning secondary law;
`transposes` — binding link from a national measure to the directive; `implements` — deprecated in
favour of `applies`, an informative link with no legal value; `changes` — amends, replaces or
repeals; `consolidates` — a consolidated text taking another item into account.
The ontology declares no class for delegated legislation.

Implications:

1. The relation is general and a standard vocabulary exists (`based_on` / `basis_for`).
2. Link provenance differs in kind: structured (DK, Cellar), derived from a separate dataset (FR),
   text-only (DE). A proposal needs to record its origin and evidence.
3. Granularity differs: instrument-level (DK, EU) versus article/section-level (FR, DE). An optional
   enabling-provision reference on the edge covers both.
4. ELI `based_on` also targets treaties and constitutions; the PS edge applies only when the
   enabling instrument is a node in the graph.
5. France additionally tracks expected-but-missing delegated instruments (pending decrees). Noted;
   out of scope.

No source was found on Akoma Ntoso or MetaLex modelling of enabling-act relations.

---

## 5. The 3P Analysis

### Proposals

- **P1 — Narrow Danish slice.** `national_law` replaces `national_transposition`; new edge
  `BASED_ON`; Danish adapter reads XML and ELI JSON-LD; proposal logic lives inside the Danish
  adapter; link proposals go to a new pending-review kind.
- **P2 — Source-neutral relation proposals.** As P1, but the adapter result gains optional
  `proposed_relations`; the core resolves and queues them; any adapter can use the same door.
- **P3 — User-supplied links only.** As P1, but no proposal queue; the user passes the link on the
  ingest call; discovery is a separate read-only listing.

### Critique

- F-1 (P1): proposal logic duplicated per adapter; breaks "ingestion core stays source-agnostic".
- F-2 (P1): review kind is shaped by Danish field names.
- F-3 (P2): changes the adapter contract for every adapter (empty list is the default).
- F-4 (P2): a proposal can target an instrument not yet in the graph; needs an explicit state.
- F-5 (P3): drops the Compliance Officer proposal flow that D-2 requires.
- F-6 (all): the Domain Mapper currently mints `TRANSPOSES` ("minted by the domain mapper from
  legislation, never submitted"); confirmed source links must not conflict with it.

### Synthesis (S) — PROPOSED

P2's source-neutral shape with P3's discipline that nothing is written until a human confirms or
the user supplied it.

- **Types and edges.** `national_law` (D-4a). `TRANSPOSES` optional on it. New generic edge
  `BASED_ON` (delegated → enabling), many-to-one, target any instrument type, optional
  `enabling_provision`; name follows the ELI vocabulary.
- **Adapter contract.** `FetchedRegulatoryInstrumentStructure` gains optional `proposed_relations`:
  `kind` (`transposes` | `based_on`), target identifier (CELEX or ELI), `evidence` (structured field
  or text span), `origin` (`structured` | `text` | `user`). `origin=user` is written directly.
- **Review flow.** Second kind in the near-miss family; outcomes confirm, reject, set manually;
  state "target not ingested" while the target is absent.
- **Danish adapter, first slice.** Original acts without an LBK, LBKs, executive orders (D-5, D-6,
  D-8).
- **Discovery.** Candidate list from `transposes`, `/eli/eu/{CELEX}` and `basis_for` signals;
  nothing auto-ingested (D-7).
- **Change detection.** Per-tracked-act metadata polling for `consolidated_by` and `changed_by`,
  in the existing sweep model; independent of the harvest window.

### Critique of S

- F-7: the Cellar adapter does not emit `proposed_relations`. Option: emit `based_on` from the CDM
  relation in the same slice, as a second source testing the contract. Not decided.
- F-8: the Change Monitor rejects re-ingestion of `national_transposition` today. It needs a guard
  for the new type, and a decision whether Danish acts are in its scope in slice 1 or covered by a
  separate poller.
- F-9: Danish text through the Domain Mapper; cross-language Capability convergence. Spike.
- F-10: Danish `{SHORT}` rule. `title_alternative` ("NIS 2-loven") is the natural candidate; not
  every act has one.

---

## 6. Design Detail

### 6.1 Repository facts that constrain the design — VERIFIED

- `InstrumentType = Literal["regulation", "directive", "national_transposition"]`
  (`ingestion/models.py`); `domain_schema/definition.py` mirrors it as an enum.
- `national_transposition` has no producing adapter. The Change Monitor rejects its re-ingestion
  (`change_monitor/errors.py`, `trigger.py`), so folding it affects a type literal, the schema
  definition, a few guards and the docs, with no data migration known.
- `SUPERSEDED_BY` is `0..1 : 0..1` (`ps-domain-concepts.md`); this is why amending acts are not
  superseded into an LBK (D-5).
- `TRANSPOSES` is `0..* : 1`, source `national_transposition`, target `directive`.
- `RegulatoryInstrumentMetadata.celex` is optional, so Danish acts can leave it null.
- Scope rule today (`ps-domain-concepts.md`): every `regulation` is in scope, plus transpositions
  whose jurisdiction is in the company's set. Typing a Danish act `regulation` would put it in
  scope for every company, which is wrong; D-4 avoids this.

### 6.2 Scope rule after the change — PROPOSED

In scope for a company operating in jurisdictions J: every `regulation`, plus every national
instrument (`national_law`) whose `jurisdiction ∈ J`. The "national `source_ref` is operative"
rule applies only where a `TRANSPOSES` edge exists; unlinked national law converges with other
instruments at the Capability level as usual.

### 6.3 Identifier and identity — PROPOSED

- Adapter identifier: ELI path, for example `lta/2018/502`.
- Instrument id: `{SHORT}-DK-{VERSION}` (see F-10).
- `celex`: null for Danish acts unless the act is linked and the directive CELEX is known; the
  CELEX field is not the Danish identifier.

### 6.4 Historic-act handling (D-6)

Metadata `consolidated_by` and `in_force` identify a historic act. The adapter reports the act,
the consolidating LBK and its status, offers the LBK, and ingests nothing until the user answers.

### 6.5 Discovery signals (D-7) — PROPOSED

1. `transposes` names a CELEX that PS tracks (precise).
2. `/eli/eu/{CELEX}` lists Danish documents tied to a tracked EU instrument (includes executive
   orders implementing regulations).
3. `basis_for` on a tracked Danish act lists dependent documents (inferred to be mostly executive
   orders; semantics **[UNVERIFIED]**).
4. Ministry or title keywords: rejected as too noisy.

Output is a candidate list showing the matched signal; same family as the near-miss and
link-proposal reviews.

---

## 7. Rejected or Deferred

- Parsing the directive link from footnote text as the primary source: the structured `transposes`
  field exists; footnote parsing is a fallback only for acts that lack it.
- Extracting Obligations from amending acts (D-5): patch text lacks context and duplicates the LBK.
- Writing after Compliance Officer resolution (hold-then-write): rejected in favour of D-3.
- Harvest API as the primary change signal: its 10-day window and hours make per-act metadata
  polling more robust; the harvest feed remains a discovery source.
- Pending-decree tracking à la France: out of scope.

---

## 8. Unverified / Open

| ID | Item |
|---|---|
| U-1 | How consistently `transposes` and `based_on` are populated across the Danish corpus (checked on five documents). |
| U-2 | LexDania schema: no public XSD found. Element semantics inferred from one document. Build against a sample set; unknown elements must fail loudly. |
| U-3 | `basis_for` semantics (signal 3). |
| U-4 | Size and composition of the ELI sitemap (about 20 000 URLs is third-party). It also holds non-legislation `pubMedia` types. |
| U-5 | How consistently Cellar populates `resource_legal_based_on_resource_legal` (one delegated act checked). |
| U-6 | France: LEGI link semantics; Légifrance API relation fields and quotas. |
| U-7 | Germany: XML element for the enabling clause; federal portal API (trial service, incomplete dataset; licence statement not found). |
| U-8 | Danish-language extraction quality in the Domain Mapper; cross-language Capability convergence (F-9). |
| U-9 | Whether the Domain Mapper keeps proposing text-derived `TRANSPOSES` for sources without metadata (F-6). |
| U-10 | F-7, F-8, F-10 above. |
| U-11 | `/eli/eu/{CELEX}` redirects to a JavaScript search page (`documents?ce=`). No machine-readable form of the reverse lookup is verified. Discovery may have to rely on incremental feeds (harvest API, Atom feed) plus a one-off sitemap crawl, evaluating the `transposes` / `based_on` metadata of each new or changed document. |
| U-12 | Where the entry-into-force date for `effective_date` comes from: the ELI JSON-LD for Danish documents showed `date_document` and `date_publication` but no entry-into-force property; the XML `StartDate` is indirect via `REFid`. |

---

## 9. Relation to Existing Issues (#40, #41, #46)

Read 2026-10-24 from the issue bodies. This exploration overlaps #41 and #46 and, through D-4a,
reopens a decision made under #40.

**#46 — national transposition ingestion, `TRANSPOSES` edge, jurisdiction-scoped queries (open).**
Its first deliverable is a *blocking* `design-exploration` on how national transposition text is
acquired ("a new national-source ingestion adapter vs the PDF pipeline vs another mechanism").
This exploration answers that for **Denmark**: a national-source adapter on Retsinformation/ELI.
Germany and France were researched here only for shape (section 4), so #46's decision remains open
for them.

| Topic | #46 | This exploration | Tension |
|---|---|---|---|
| Instrument type | `national_transposition` kept | D-4: new `national_law`; D-4a (PROPOSED): fold `national_transposition` into it | D-4a reopens the #40 enum decision |
| Edge cardinality | node has **exactly one** outbound `TRANSPOSES` (AC) | `TRANSPOSES` optional (D-3: written unlinked first) | #46 AC conflicts |
| Who writes the edge | new domain-edge writer; Domain Mapper reused unchanged | structured source metadata → Compliance Officer-confirmed proposal (D-2) | different mechanism |
| Scope | transposition only | also national law with no directive (D-1, D-4) and executive orders (D-8) | this is a superset |
| Query resolution | in-scope set = all `regulation` + transpositions with `jurisdiction ∈ J` | same, widened to all `national_law` (6.2) | compatible; #46's query patterns are reusable as written |
| Company Merge | jurisdiction-blind (#40 D-5) | no change proposed | none |
| Proof | `NIS2-DE` end to end | Danish NIS2 act (`lta/2025/434`, `transposes` → CELEX 32022L2555) is a natural DK analogue | none |

**#41 — directive-aware amendment monitoring (open).** Its open questions ("per-jurisdiction
source adapters", "whether the national sources offer machine-readable change feeds at all") are
answered for Denmark in section 3: yes — per-act ELI metadata (`consolidated_by`, `changed_by`),
the harvest API and the Atom feed. Overlap:

- #41 scope 1 (per-transposition polling) ≈ this plan's change-detection sub-issue.
- #41 scope 2 (work-queue of "transposed or amended, not yet ingested", kept in the Change
  Monitor's state, never as a hollow graph node) ≈ D-7's candidate list for the `transposes`
  signal. Compatible: both keep the backlog outside the graph.
- #41 scope 3 (re-transposition after a Directive revision) is not touched here.
- F-8 (Change Monitor guard on `national_transposition`) lands inside #41's territory.

**#40** (resolved; instrument_type, `TRANSPOSES`, `national_transposition`): D-4a is the only item
here that changes its outcome. If D-4a is rejected, `national_law` stays additive and #46's AC stand.

**Disposition (D-9).** #41 and #46 are lifted and closed:

| Old scope | Carried by |
|---|---|
| #46 acquisition decision | answered for Denmark here; Germany and France carried by the "other jurisdictions" deferred issue |
| #46 type, identity, `TRANSPOSES` writer | domain-model sub-issue |
| #46 jurisdiction-scoped resolution, framework-completeness, gap-analysis queries | queries sub-issue |
| #46 `NIS2-DE` / `NIS2-FR` proof | Danish NIS2 proof in the Danish adapter sub-issue; DE/FR in the deferred issue |
| #41 per-transposition polling, "not yet ingested" backlog | change-detection and discovery sub-issue |
| #41 re-transposition after Directive revision | separate deferred issue |
| #46 note on #42 (Worked Example 4 reading) | Related links on the queries sub-issue |

---

## 10. Plan and Doc-Sync Rule

Work is tracked as umbrella #374 with sub-issues:

1. #375 Spike: link coverage and Danish-language extraction (U-1, U-8, short-name rule); runs first, may invalidate later issues.
2. #376 Domain model: `national_law`, `BASED_ON`, scope rule.
3. #377 Adapter contract and review kind: `proposed_relations`, link-proposal review.
4. #378 Danish adapter.
5. #379 Change detection and discovery.
6. #380 Jurisdiction-scoped resolution and gap queries.
7. Deferred, not started by the umbrella: #381 (Germany and France acquisition), #382 (re-transposition after a Directive revision).

#41 and #46 were closed on 2026-10-24 with comments naming the replacing issues (D-9).

**Sync rule.** Every sub-issue must, in the same pull request as its code, (a) update the solution
and container architecture docs it affects, and (b) update this document: mark the decisions and
open items it resolves, correct anything that proved wrong, and record any new fact. A sub-issue is
not done until its pull request does both. The umbrella closes only after a final walk of
section 8 and the result recorded here.
