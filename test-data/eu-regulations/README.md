# EU Regulations Test Data

This folder contains promoted, reusable test datasets extracted from EU regulation source documents.

## Source regulations

- `docs/regulations/CRA.md`
- `docs/regulations/NIS2.md`
- `docs/regulations/gdpr.md`

## Promoted datasets

- `cra.json`
- `nis2.json`
- `gdpr.json`
- `policy_system_graph.json`
- `capability_merges.json`

## Provenance and extraction notes

- `cra-extraction-methodology.md`
- `nis2-extraction-methodology.md`
- `gdpr-extraction-methodology.md`

## Notes

- These files were copied from `spikes/graph-ingestion3` to preserve proven outputs from spike work in a durable test-data location.
- Spike originals are intentionally retained for now to avoid breaking existing spike scripts and references.
- Canonical loader and capability-convergence scripts are in `tools/graph-ingestion`.
