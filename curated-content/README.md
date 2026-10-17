# curated-content

Curated, pre-ingested Policy System instruments — restorable into a deployed PS
Service without an LLM provider or a live extraction run.

## Layout

```text
curated-content/
  catalog.json                  # aggregate listing consumed by the `get-catalog-listing` tool and the `ps-get-catalog-listing` skill
  {INSTRUMENT_ID}/
    manifest.json                # schema_version, checksums, and identity fields
    baseline.json                 # the instrument's baseline graph, serialized
    native.json                   # the instrument's native structural graph, serialized
```

Each `{INSTRUMENT_ID}/` directory is produced once, by a project maintainer, via
`tools/curated-export/export_instrument.py` run against an already-ingested source —
see
[`ps-service-container-architecture.md`'s Export section](../docs/architecture/ps-service-container-architecture.md#export)
for the mechanism, and
[`ps-service-container-architecture.md`'s Restore section](../docs/architecture/ps-service-container-architecture.md#restore)
for how the `restore_instrument` tool (the `ps-restore-instrument` skill) loads one back into a target
deployment. The export CLI needs more than FalkorDB and an LLM provider: it writes the
embedding backfill through the Graph Write Gateway, so the PS state Postgres (the graph
mutation log and the audit trail) must be configured and reachable, and each run records one
system audit row before any embedding is computed. Resolve every near-miss review of the instrument first (`near_misses_list`,
`near_misses_resolve`): the export refuses (exit 1, nothing written) while an unresolved
review touches one of its Capabilities, because review decisions live in `policy_system`,
outside the exported graphs. `catalog.json` is always regenerated wholesale by that same tooling —
never hand-edited.

**Licensing:** see [`LICENSING.md`](./LICENSING.md) for the one-time confirmation that
content curated here is compatible with public redistribution, covering both external
(EU regulation) and internal (project-authored) sources.

**Current state:** two external EU instruments are curated here — CRA and GDPR (see
`catalog.json` for the CELEX ids) — and the internal Engineering Practices baseline
(`ENGPRAC-1.0`). Populating a new instrument is a one-time maintainer
action, separate from restoring an already-curated instrument (see the
user guide's
[Load curated content](../docs/artifacts/user-guide.md#load-curated-content)
for what an operator sees once it is populated).
