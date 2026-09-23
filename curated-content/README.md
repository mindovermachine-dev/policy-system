# curated-content

Curated, pre-ingested Policy System instruments — restorable into a deployed PS
Service without an LLM provider or a live extraction run (issue #66).

## Layout

```text
curated-content/
  catalog.json                  # aggregate listing consumed by `ps-cli get catalog`
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
for how `ps-cli restore instrument <instrument_id>` loads one back into a target
deployment. `catalog.json` is always regenerated wholesale by that same tooling —
never hand-edited.

**Licensing:** see [`LICENSING.md`](./LICENSING.md) for the one-time confirmation that
content curated here is compatible with public redistribution, covering both external
(EU regulation) and internal (project-authored) sources.

**Current state:** nine external EU instruments are curated here — CRA, GDPR, NIS2,
RRF, the Data Act, the Web Accessibility Directive, DSA, DORA, and the AI Act (see
`catalog.json` for the full list with CELEX ids). The internal Engineering Practices
baseline is not yet curated. Populating a new instrument is a one-time maintainer
action, tracked separately from restoring an already-curated instrument (see the
installation guide's
[Evaluator installation, step 8](../docs/artifacts/installation-guide.md#8-load-regulations-into-the-graph)
for what an operator sees once it is populated).
