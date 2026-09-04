# curated-content

Curated, pre-ingested Policy System instruments — restorable into a deployed PS
Service without an LLM provider or a live extraction run (issue #66).

## Layout

```text
curated-content/
  catalog.json                  # aggregate listing consumed by `ps-cli catalog list`
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
for how `ps-cli catalog restore <instrument_id>` loads one back into a target
deployment. `catalog.json` is always regenerated wholesale by that same tooling —
never hand-edited.

**Licensing:** see [`LICENSING.md`](./LICENSING.md) for the one-time confirmation that
content curated here is compatible with public redistribution, covering both external
(EU regulation) and internal (project-authored) sources.

**Current state:** this folder ships as an empty scaffold — no `{INSTRUMENT_ID}/`
directories or `catalog.json` are committed yet. Populating it with real curated
instruments (CRA, GDPR, NIS2, and the internal Engineering Practices baseline) is a
one-time maintainer action, tracked separately from restoring an already-curated
instrument (see the user guide's
[Local Test, step 5](../docs/artifacts/user-guide.md#5-load-regulations-into-the-graph)
for what an operator sees once it is populated).
