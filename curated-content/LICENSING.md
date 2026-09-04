# Curated content — licensing confirmation

This is the one-time licensing confirmation TASK.md's deliverable requires before the
first curated artifact is committed under this folder (issue #66, D16). It is a
documented finding, not a runtime check — nothing in `ps_service`/`ps-cli` reads this
file, and it is not re-verified per export.

## External sources (EU regulation text — CRA, GDPR, NIS2, ...)

Every external instrument curated here is sourced via the Cellar/ELI adapter
(`ps-service/src/ps_service/ingestion/adapters/cellar_eli/`) — Cellar is the EU
Publications Office's official legal-content repository, and its content is published
on EUR-Lex under the EU institutions' own reuse-of-documents policy:

- EUR-Lex Legal Notice:
  <https://eur-lex.europa.eu/content/legal-notice/legal-notice.html>
- Commission Decision 2011/833/EU of 12 December 2011 on the reuse of Commission
  documents (OJ L 330, 14.12.2011, p. 39), repealing Decision 2006/291/EC/Euratom.

**Finding.** That policy permits free reuse of EU legal-act text — including
redistribution, for both commercial and non-commercial purposes — subject to
acknowledging the source, not distorting the original meaning or wording, and not
holding the EU institutions liable for how the reused content is subsequently used,
unless a specific document states an exception. None of the instruments this project
has ingested to date (CRA, `32024R2847`; GDPR, `32016R0679`; NIS2, `32022L2555`) carry
such an exception. Verbatim EU legal-act text exported into this folder's
`{instrument_id}/` artifacts (baseline and native structural graphs alike) is
therefore compatible with public redistribution as part of this open-source
repository. **No additional per-instrument license file is required** — this
confirmation covers every external instrument curated under this mechanism, present
and future, unless a specific future instrument is later found to carry its own
reuse exception (in which case that instrument's own curation should record the
exception here before export, not silently proceed).

Attribute reused EU regulation text as originating from the European Union via
EUR-Lex, citing the instrument's CELEX identifier (e.g. `32024R2847`) alongside any
excerpt — the same identifier already carried on every ingested node's provenance
fields.

## Internal sources (e.g. Engineering Practices)

Content curated under `source_type: internal` (e.g. the `ENGPRAC-*` instrument family)
is wholly project-authored: Role/Requirement/Obligation/Capability/Policy/Standard/
Control text minted from this project's own Business SoPs, never third-party
regulation text. No third-party rights question applies — this repository's own
license governs internal-source curated content like any other file in the
repository.

## Scope note

This confirmation covers the _content_ curated under `curated-content/` (regulation
text and internally-authored governance text). It says nothing about the export
artifact _format_ itself (`manifest.json`/`baseline.json`/`native.json` — see
[`ps-service-container-architecture.md`'s Export section](../docs/architecture/ps-service-container-architecture.md#export)),
which carries no licensing question of its own.
