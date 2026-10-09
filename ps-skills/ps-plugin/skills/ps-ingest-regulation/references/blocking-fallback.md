# Blocking fallback for older PS Service (ps-ingest-regulation)

Read on demand from `SKILL.md`'s Process when On Load finds `ingest_regulation` but neither `start_ingestion` nor `get_ingestion_status`. Every Guardrail in `SKILL.md` still applies.

2b. **Fallback (older PS Service only, see On Load).** Call
`ingest_regulation` with the same two inputs instead. This fallback is a
blocking call that can take minutes — do not report a failure just
because it is still in flight. Its result is either the same summary a
succeeded run's `result` holds or an `error:` string; go straight to
step 4/5 with it.
