<!-- © 2026 Cartman ApS. All rights reserved. -->

# Data Model, Persistence and State Migrations Audit - Findings Report (Issue #256, umbrella #246)

## Scope examined

| Area                    | Paths                                                                                                                                                                                                                                          |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Service persistence     | `ps-service/src/ps_service/persistence`, `state_migrations.py`, the five component `migrations/` directories (`audit`, `authz`, `graph_gateway`, `ingestion_runs`, `runtime_config`) and `passkey_signing/migrations`                          |
| Graph                   | `ps-service/src/ps_service/graph_gateway`, `domain_schema`, `restore`, `graph_cleanup`, `export`, and every other package that opens a graph handle                                                                                             |
| Provisioning            | `charts/policy-system/files/ps-postgres-init.sh`, `charts/policy-system/templates/ps-postgres-init-configmap.yaml`                                                                                                                              |
| Backup and restore      | `scripts/verify-backup-restore.sh`, `tools/backup-restore-verify`                                                                                                                                                                               |
| Canonical documentation | `docs/artifacts/ps-domain-concepts.md` (read only, not edited)                                                                                                                                                                                  |

Out of scope: feature work and any change to persistent data, migrations or public contracts (filed as issues instead).

## Environment and limitations

- A local kind cluster (`kind-policy-system`) was already running. Only read-only SELECT and catalog queries were run against it, each command with an explicit `--context kind-policy-system`. A second (AKS) context exists and was never used.
- No secrets, role password hashes (`pg_authid`) or connection strings were queried or recorded. Roles were read from `pg_roles` (names and login flag only).
- The live FalkorDB was empty (`GRAPH.LIST` returned nothing, `DBSIZE` 0). The graph schema (labels, relationship types, indexes, properties) was therefore derived statically from code and compared with the domain document; no live graph label, relationship or index extraction was possible. This is a stated limitation of the audit.
- `scripts/verify-backup-restore.sh` subcommands `backup`, `restore`, `seed` and `e2e` were not run (they write to or wipe the developer cluster). `counts` and `verify-provision` use the active kubectl context rather than an explicit one and were not run either. The script was audited statically and its unit tests were run.
- Tests marked `postgres_live` need a reachable database and were not run.

## Method and commands

Run from the repository root; `S` is any scratch directory outside the repository. Python sources use Python 3.14 syntax: run helper scripts with `uv run --frozen python -B` from `ps-service/`.

1. Live Postgres schema (read only). Save the SQL below as `$S/q.sql`, then for each database:

   ```text
   K=(kubectl --context kind-policy-system)
   for d in ps_state ps_signing; do
     ${K[@]} exec -i deploy/policy-system-ps-postgres -- sh -c "psql -U \"\$POSTGRES_USER\" -d $d -At" < $S/q.sql
   done
   ```

   `q.sql` selects table names (`pg_tables`), index names and definitions (`pg_indexes`), columns (`information_schema.columns`), constraints (`pg_constraint`), user triggers (`pg_trigger` where not internal), table owners, and the applied migration rows (`ps_schema_migrations` in `ps_state`, `schema_migrations` in `ps_signing`).

2. Graph labels and relationship types from code: a script printing `ALLOWED_NODE_LABELS` and `ALLOWED_RELATIONSHIP_TYPES` from `ps_service.graph_gateway.label_allow_list`.
3. Property coverage: a script iterating `DOMAIN_SCHEMA.nodes` and checking each property name appears in `docs/artifacts/ps-domain-concepts.md`.
4. Graph writers and readers: `grep -rnE "\.query\(|GRAPH\.QUERY" ps-service/src --include='*.py' | grep -v graph_gateway/`, then `grep -rnE "select_graph|FalkorDB\(|execute_command" ps-service/src --include='*.py'` outside `graph_gateway/`, and a search for mutating Cypher keywords outside `graph_gateway/`.
5. Lookup keys: `grep -rnoE "\([a-z_]*:[A-Za-z]+ *\{[a-z_]+:" ps-service/src --include='*.py'` outside `graph_gateway/`; Postgres `WHERE`/`ORDER BY` review of each store module.
6. Column references: for each live column, `grep -rlw <column> ps-service/src --include='*.py'`.
7. Migration tests, one file per command (`uv run pytest <file>` from `ps-service/`): `tests/test_state_migrations.py`, `tests/persistence/test_migration_runner.py`, `tests/persistence/test_privileged_migration_runner.py`, `tests/passkey_signing/test_migration_runner.py`, `tests/audit/test_migrations.py`, `tests/ingestion_runs/test_migrations.py`, `tests/graph_gateway/test_migrations.py`.
8. Verifier: `bash -n scripts/verify-backup-restore.sh`, `shellcheck scripts/verify-backup-restore.sh`, and `uv run pytest` on `tests/scripts/test_verify_backup_restore.py`, `..._e2e.py`, `..._restore.py`, `tests/tools/test_graph_digests.py`, `tests/tools/test_seed_tools.py`.

Earlier audits (`dead-code-audit.md`, `architecture-audit.md`, `dependencies-audit.md`, `repo-hygiene-audit.md`) were checked for persistence overlap: the graph-gateway symbols reachable only from tests are issue #263, and the edge-identity helper copies are issue #274; neither is re-filed here.

## Severity scale

- High: data loss on restore, an unlogged write path, or an exploitable weakness.
- Medium: a persistence guarantee or acceptance criterion is not evidenced, or a hot path lacks protection.
- Low: hygiene, redundancy or documentation drift with no behavioural effect.

## S1 Graph model

Derived label set (20): `ANNEX ARTICLE CHAPTER Capability Control MergedObligation Obligation PARAGRAPH PendingReview Policy PracticeArea RECITAL RegulatoryInstrument ReingestProgress Requirement RiskPath Role SECTION Standard TITLE`. Derived relationship types (15): `COVERS DEFINES EXPRESSES GOVERNED_BY HAS IMPLEMENTED_BY MERGED_INTO MITIGATED_BY OWNS REQUIRES SATISFIED_BY SUPERSEDED_BY SUPPORTED_BY TRANSPOSES VERIFIED_BY`. Source: `graph_gateway/label_allow_list.py:22-34`, built from `DOMAIN_SCHEMA` plus the named sets in `domain_schema/vocabulary_exceptions.py` (`SYSTEM_MINTED_EDGE_TYPES` line 20, `OPERATIONAL_LABELS` line 28). Live label counts: not available (empty graph).

| Group                                                                                                                                                                          | Names                                                                                                           | Written by                                                                                | Domain document                                                                                          | Status                                          |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| Domain nodes                                                                                                                                                                   | `RegulatoryInstrument Role Requirement Obligation Capability PracticeArea RiskPath Policy Standard Control`     | ingestion, domain mapper, company merge, policy lifecycle, internal seed, all via gateway | all documented; every schema property name appears in the document (script 3)                           | mapped                                          |
| Native structural labels                                                                                                                                                       | `TITLE CHAPTER SECTION ARTICLE PARAGRAPH ANNEX RECITAL`                                                         | ingestion graph writer (Cellar/ELI native graph)                                          | native graph vocabulary, not domain concepts; `CHAPTER` and `RECITAL` are not named in the document     | mapped as native vocabulary                     |
| Operational labels                                                                                                                                                             | `MergedObligation`, `PendingReview`, `ReingestProgress`                                                         | graph cleanup, company merge, change monitor                                              | `MergedObligation` named once (line 368); `PendingReview` and `ReingestProgress` not named               | unmapped by design (declared operational)       |
| Replay sentinel                                                                                                                                                                | `GraphReplayState` (not allow-listed on purpose)                                                                | gateway replay (`graph_gateway/replay_state.py`)                                          | not named                                                                                                | unmapped by design (declared bookkeeping label) |
| Relationship types                                                                                                                                                             | 15 types above                                                                                                  | every type except `TRANSPOSES` has a producer in source                                   | all 15 appear in the edge catalog (`TRANSPOSES` line 183)                                                | mapped; `TRANSPOSES` has no producer yet        |

Restore allow-lists (`restore/schema_allowlist.py:29-90`) are independent hand-written lists by design and are pinned to the intake vocabulary by `tests/restore/test_schema_allowlist.py`.

## S2 Postgres schema, indexes and hot paths

Live `ps_state`: 10 tables (`graph_log`: `applied_markers checkpoints entries groups payloads`; `public`: `access_role_assignments audit_events ingestion_runs ps_schema_migrations runtime_config`), 21 indexes, 3 user triggers (`applied_marker_guard`, `entries_next_position`, `groups_match_entries`), `graph_log` tables owned by the NOLOGIN role `ps_state_graph_owner`, public tables owned by `ps_state`. Live `ps_signing`: 3 tables (`pending_approvals schema_migrations signing_credentials`), 8 indexes, owner `ps_signing`. Every live table and index is created by a migration file; applied rows equal the files (`ps_state`: 7 rows, audit 2, authz 1, graph_gateway 2, ingestion_runs 1, runtime_config 1; `ps_signing`: 3 rows). No drift in either direction.

Table to code and domain mapping:

| Table(s)                                                                   | Module                                              | Domain concept                                       |
| -------------------------------------------------------------------------- | --------------------------------------------------- | ---------------------------------------------------- |
| `audit_events`                                                             | `audit/store.py`                                    | audit trail (non-domain concern)                     |
| `access_role_assignments`                                                  | `authz/store.py`                                    | AccessRole (authorization, non-domain concern)       |
| `runtime_config`                                                           | `runtime_config/store.py`                           | catalog-source override (non-domain concern)         |
| `ingestion_runs`                                                           | `ingestion_runs/store.py`                           | ingestion run record (non-domain concern)            |
| `graph_log.*` (5 tables)                                                   | `graph_gateway/store.py`, `gateway_log.py`          | Graph Write Gateway and Mutation Log (document `:688-735`) |
| `pending_approvals`, `signing_credentials`                                 | `passkey_signing/store.py`, `signing_credential_store.py` | passkey signing (non-domain concern)           |
| `ps_schema_migrations`, `schema_migrations`                                | `persistence/migration_runner.py`, `passkey_signing/migration_runner.py` | migration tracking                     |

Column check (script 6): of the live columns only `checkpoints.recorded_at` and `groups.committed_at` are never referenced in source; both are `DEFAULT now()` bookkeeping timestamps. The payload hash and `byte_length` columns are referenced once, in the log store.

Hot-path lookup keys:

| Lookup                                                                                       | Index / constraint                                                                      | Result                                  |
| -------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | --------------------------------------- |
| audit query by resource, actor, time range, `details` keys                                   | `audit_events_resource_idx`, `_actor_idx`, `_occurred_at_idx`, three `details->>` expression indexes | covered |
| authz by principal; count by role                                                            | primary key prefix; `access_role_assignments_role_idx`                                  | covered                                 |
| runtime config by key; ingestion run by `run_id`                                             | primary keys                                                                            | covered                                 |
| log by `(graph, position)`, by `group_id`, by `audit_event_id`, by `swap_set_id`             | `entries_pkey`, `entries_group_idx`, `groups_audit_event_idx`, `groups_swap_set_idx`    | covered; `groups` by `graph` scans a small table |
| pending approvals by `id`, by `code_hash`; credentials by actor, `credential_id`             | primary key, unique `code_hash`, `signing_credentials_actor_rp_idx`, unique `credential_id` | covered                            |
| graph lookups by `id` (about 30 literal sites)                                               | `CREATE INDEX FOR (n:L) ON (n.id)` per written label (`graph_gateway/index_manager.py:47`, `cypher.py:148`, `applier.py:114`) | covered |
| graph lookups by `celex` and `status`                                                        | none                                                                                    | gap, see P-S2-03                        |
| uniqueness of graph `id`                                                                     | no `CREATE CONSTRAINT` anywhere in `ps-service/src`                                     | recorded, see P-S2-03                   |

## S3 State migrations

- Sources: `ORDINARY_STATE_MIGRATION_SOURCES` (`state_migrations.py:27-32`: audit, authz, runtime_config, ingestion_runs), `GRAPH_GATEWAY_MIGRATION_SOURCE` (`:35`, privileged runner, admin credential), `passkey_signing` deliberately absent (own database and tracker `schema_migrations`). Coverage is pinned by `tests/test_state_migrations.py`.
- Ordering: filename order, `sorted(directory.glob("*.sql"))` (`persistence/migration_runner.py:115`; `passkey_signing/migration_runner.py:44`); components apply in list order (audit first because the log links to it).
- Idempotency: each file is applied once and recorded with its bookkeeping row in one transaction under an advisory lock; re-running applies nothing. Files themselves are plain `CREATE` (correct because tracked).
- 10 migration files: audit 2, authz 1, graph_gateway 2, ingestion_runs 1, passkey_signing 3, runtime_config 1.

| Component         | Empty-database test                                  | Second run is a no-op                          | N-1 to N upgrade test | Note                                     |
| ----------------- | ---------------------------------------------------- | ---------------------------------------------- | --------------------- | ---------------------------------------- |
| audit             | `persistence/test_migration_runner.py` (live)        | `test_second_run_applies_nothing` (live)       | none                  | static checks in `audit/test_migrations.py` |
| authz             | none                                                 | none                                           | none                  | no migration test file                   |
| runtime_config    | none                                                 | none                                           | none                  | store test only                          |
| ingestion_runs    | `ingestion_runs/test_migrations.py` (live)           | `test_migration_applies_once_...` (live)       | none                  |                                          |
| graph_gateway     | `graph_gateway/test_migrations.py` (static); provisioning live test | provisioning live test            | none                  | privileged runner tests static           |
| passkey_signing   | `passkey_signing/test_migration_runner.py` (live)    | `test_applying_migrations_twice_is_idempotent` (live) | none           |                                          |

Targeted runs (no live database): `test_state_migrations.py` 4 passed, `persistence/test_migration_runner.py` 4 passed (live ones deselected), `persistence/test_privileged_migration_runner.py` 6 passed, `audit/test_migrations.py` 4 passed, `ingestion_runs/test_migrations.py` 3 passed, `graph_gateway/test_migrations.py` 9 passed, `passkey_signing/test_migration_runner.py` no tests collected (all marked `postgres_live`).

## S4 Subsystems that own state, and the backup and restore verifier

Write-path sweep (script 4). Every `.query(` call outside `graph_gateway/` was classified by reading its Cypher and its handle: the modules `ingestion`, `change_monitor`, `domain_mapper`, `company_merge`, `policy_lifecycle`, `graph_cleanup`, `export`, `restore`, `query_engine`, `mcp_interface` and `api` obtain graphs only through `select_graph(ReadOnlyDatabase, ...)`, i.e. `GRAPH.RO_QUERY`, which the server refuses to run if it would write (`graph_gateway/read_only.py`). No mutating Cypher keyword occurs outside `graph_gateway/` except the read-only validator regex in `query_engine/cypher_query.py:42` and relationship-type constants. All graph writes are therefore log-first through the gateway, hence rebuildable from Postgres on restore. No High finding.

Store inventory (AC-BI-005):

| Store                                              | In `pg_basebackup` | Compared by the verifier                                 | Restore path                                       |
| -------------------------------------------------- | ------------------ | -------------------------------------------------------- | -------------------------------------------------- |
| Postgres `ps_state` (10 tables)                    | yes (whole cluster) | 3 tables by count; every logged graph by digest (`verify-backup-restore.sh:136-146`, `:454`) | data directory extracted, provisioning job re-run |
| Postgres `ps_signing` (3 tables)                   | yes                | no                                                       | same                                               |
| FalkorDB graphs                                    | not backed up (derived) | digest per logged graph                              | volume emptied, graphs replayed from the log       |
| authentik Postgres (separate deployment and PVC)   | no                 | no                                                       | none in the script                                 |
| ps-service `emptyDir` (trust bundle)               | n/a                | n/a                                                      | n/a                                                |

Verifier static review: `bash -n` passes, `shellcheck` reports nothing; `test_verify_backup_restore.py` 12, `..._e2e.py` 12, `..._restore.py` 29, `test_graph_digests.py` 8, `test_seed_tools.py` 3 tests all pass. The verifier derives the graph list from `graph_log.entries`, so every logged graph is digested. The init script creates roles and databases only on an empty data directory (`ps-postgres-init.sh:33` onward); roles live in the data directory and so survive a restore.

## Findings

| id       | location                                                                                            | severity | evidence                                                                                                                                                                                    | disposition                                                                                          |
| -------- | --------------------------------------------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| P-S0-01  | live FalkorDB                                                                                       | Low      | `GRAPH.LIST` empty and `DBSIZE` 0, so no live label, relationship, property or index extraction; schema derived statically.                                                                | no action: limitation recorded; the first full verifier run (issue #283) covers a seeded graph       |
| P-S1-01  | `vocabulary_exceptions.py:28`; `docs/artifacts/ps-domain-concepts.md`                               | Low      | `PendingReview` and `ReingestProgress` (operational) and `GraphReplayState` (sentinel) are not named in the canonical document; declared non-domain labels. The document is not edited here. | issue #287                                                                                           |
| P-S1-02  | `domain_schema/definition.py:455`, `domain_schema/intake.py:88`                                      | Low      | `TRANSPOSES` is allow-listed and documented but no source file writes it; the producer is the open national-transposition feature.                                                        | issue #46 (existing feature issue)                                                                   |
| P-S1-03  | `restore/schema_allowlist.py:29-90` vs `label_allow_list.py:22-34`                                   | Low      | The restore lists are hand-written and differ from the gateway list on purpose (no operational labels); a test pins them to the intake vocabulary.                                          | no action: independent by design, pinned by `tests/restore/test_schema_allowlist.py`                  |
| P-S2-01  | `checkpoints.recorded_at`, `groups.committed_at` (`0001_graph_mutation_log.sql:44,74`)               | Low      | Never referenced in source; `DEFAULT now()` bookkeeping timestamps of an insert-only log.                                                                                                   | no action: forensic timestamps of an append-only log; dropping needs a migration for no gain          |
| P-S2-02  | `pending_approvals_actor_idx`, `signing_credentials_actor_idx` (`passkey_signing/migrations`)        | Low      | No query filters `pending_approvals` by actor; the credentials actor index is a prefix of `signing_credentials_actor_rp_idx`. Static review only (`idx_scan` of a fresh cluster is not evidence). | issue #286                                                                                           |
| P-S2-03  | `api/ingestion_orchestration.py:835`, `mcp_interface/mcp_server.py:2250`, `change_monitor/*`, `company_merge/dedup.py:145` | Low | Graph indexes exist only on `id`; lookups by `RegulatoryInstrument.celex` and by `status` are unindexed, and graph `id` has no uniqueness constraint. Small graph today. | issue #285                                                                                           |
| P-S2-04  | `audit_events`, `pending_approvals`                                                                  | Low      | No retention or purge for either table (no `DELETE FROM` in source); audit is append-only by design, approvals are small and short lived.                                                  | no action: append-only by design; retention is a feature decision outside this audit                  |
| P-S3-01  | `tests/authz`, `tests/runtime_config`; all components                                                | Medium   | No migration test for authz or runtime_config; no test applies the previous release set then the current one; the empty-database and no-op tests are `postgres_live` and did not run here. | issue #282                                                                                           |
| P-S3-02  | `persistence/migration_runner.py:69`, `passkey_signing/migration_runner.py:34`                       | Low      | Two tracking tables (`ps_schema_migrations` keyed by component, `schema_migrations`) in two databases; documented isolation.                                                               | no action: documented, deliberate isolation; merging tracking tables is a persistent change            |
| P-S4-01  | all packages outside `graph_gateway/`                                                                | Low      | Raw-write sweep found no mutating graph query outside the gateway; all non-gateway handles are read-only.                                                                                   | no action: confirmed log-first write path (negative result)                                           |
| P-S5-01  | `scripts/verify-backup-restore.sh:136-146`                                                           | Medium   | Counts compare 3 of 13 tables; `runtime_config`, `ingestion_runs`, four `graph_log` tables and both signing tables are not compared; the authentik database is not backed up.                | issue #284                                                                                           |
| P-S5-02  | `scripts/verify-backup-restore.sh` (`e2e`)                                                           | Medium   | "A restored deployment passes the verifier" (AC-BI-005 second half) was not exercised in this run: the check wipes volumes and was not allowed against the developer cluster.               | issue #283                                                                                           |
| P-S5-03  | `charts/policy-system/files/ps-postgres-init.sh:2-18`                                                | Low      | Header says the state database holds "audit, authz, runtime config"; it also holds `ingestion_runs` and the graph log. Editing the script alters the rendered ConfigMap.                    | issue #287                                                                                           |

## False positives

- `PendingReview`, `ReingestProgress`, `MergedObligation` "missing from the domain document": declared operational labels, not domain nodes.
- `GraphReplayState` absent from the gateway allow-list: intentional (a writer must not forge replay progress).
- `ps_schema_migrations` versus `schema_migrations`: deliberate naming to avoid runner collision.
- `ingestion_runs` has only a primary key: every query is by `run_id`.
- `groups_id_graph_key` and `audit_events_*` expression indexes: used by the log foreign key and the allow-listed audit filters.

## Superseded and removed

Nothing was removed or changed. No schema, migration, chart, source or test file was edited; every finding is a documented no-action or a filed issue.

Grep proof (no removal, so no removed symbol to search for): `git diff --stat` and `git status --short` at closure show no tracked file changed and only this document added. Repository search excluding `docs/audits` and `.orchestrator` for the names this audit judged unused (`pending_approvals_actor_idx`, `signing_credentials_actor_idx`, `checkpoints.recorded_at`, `groups.committed_at`) still finds them only in their migration files (`passkey_signing/migrations/0001`, `0002`; `graph_gateway/migrations/0001`), as expected, because their removal is filed (#286) or declined, not done.

## Sibling-issue review

Method: the list of sub-issues of #246 was read with `gh api repos/:owner/:repo/issues/246/sub_issues --paginate`, and the body (not only the title) of every open audit sub-issue and of every issue filed by earlier audits was read. Issues filed by this audit: #282 to #287, each a native sub-issue of #246 (6 of 6 verified).

| Issue | Outcome |
| ----- | ------- |
| #251 coding standards | no change needed: no persistence-specific rule; test-only issue #282 follows the existing test conventions |
| #252 security | no change needed: no persistence security finding; the backup artefact copies the whole database cluster, a deployment fact already inside its scope |
| #253 logging | no change needed: no logging finding |
| #254 documentation | comment already posted: #287 (domain document pointer to operational and sentinel labels) is owned here |
| #255 API contracts | no change needed: no contract finding; the verifier output format change in #284 is a script interface, not an API |
| #257 config and deployment | comment posted: #287 item 1 (init script header) may be folded into any edit of that script; authentik database outside the backup scope (#284) |
| #259 observability | comment posted: #283 end-to-end restore overlaps its restart and readiness checks; #285 can use its latency metrics as the measured trigger |
| #260 guardrails | comment posted: candidate guards (migration-test coverage per component, restore coverage) and order after #282 and #284; #283 can serve as the restore guard |
| #263 test-only symbols | no change needed: graph-gateway symbols already listed there, none re-filed |
| #264 to #269 | no change needed: no persistence overlap |
| #270, #271, #272, #273 | no change needed: no persistence code is moved by these refactors beyond import paths |
| #274 edge identity copies | no change needed: copies are body-identical and pinned by tests; #287 and this audit's findings do not touch them |
| #275, #276 | no change needed: licence and npm tooling, unrelated |
| #277 FalkorDB image tag | order noted on #283 (pin before the end-to-end run) and #285; no change to #277 itself. The graph is rebuilt from the log on replay, so a version change does not alter stored state |
| #278 Python base image | no change needed |
| #279, #280, #281 | no change needed: dependency bumps, no persistence interaction |
| #282, #284 | filed by this audit; order: before any guardrail that asserts them (comment on #260); #283 order comment posted |
| #283 e2e verifier | comment posted: order after #277 and with or after #284 |
| #285 graph indexes | comment posted: use #259 metrics as the trigger; settle #277 first |
| #286 signing indexes | no change needed |
| #287 docs alignment | comment posted: ownership split between #254 and #257 |

## Acceptance criteria status

| AC | Status | Evidence |
| -- | ------ | -------- |
| AC-BI-001 | MET | This document: scope, environment and limitations, reproducible commands, findings table with id, location, severity, evidence, disposition |
| AC-BI-002 | MET with a stated limit | Tables, columns, indexes and constraints mapped from the live cluster (S2); graph labels and relationships mapped statically (S1) because the live graph was empty (P-S0-01); unmapped names listed (#287) |
| AC-BI-003 | MET | Orphan candidates are filed (#286) or justified no-action (P-S2-01); none removed (migrations are persistent change) |
| AC-BI-004 | MET with gaps filed | Migration ordering, idempotence and tests reviewed (S3); missing authz and runtime_config tests and N-1 to N test filed as #282 |
| AC-BI-005 | NOT MET | Backup covers the whole cluster, but "a restored deployment passes the verifier" was not demonstrated: the check wipes volumes and was not run against the developer cluster (decision D-13). The verifier also compares only 3 of 13 tables. Tracked by #283 (run end to end in a disposable cluster) and #284 (compare every table and the signing database). Not claimed as verified. |
| AC-BI-006 | MET with gap filed | Postgres hot-path lookups are indexed or keyed; unindexed graph lookups filed as #285 |
| AC-BI-007 | MET | Every findings row has exactly one disposition (no action with reason, or issue number) |
| AC-BI-008 | MET | Persistent-data, migration and script-contract items filed as #282 to #287, none fixed inline |
| AC-BI-009 | MET | Shipped-behaviour items filed with typed titles (`test`, `fix`, `perf`, `refactor`, `docs`); nothing bundled in this chore |
| AC-BI-010 | MET (vacuous) | No source fix made, so no root-cause fix or targeted test is required beyond the evidence runs in Method |
| AC-BI-011 | MET (vacuous) | Nothing removed; see Superseded and removed |
| AC-BI-012 | MET | Leakage scan of this document and of the bodies of #282 to #287: no secrets, credentials, connection strings, hostnames, addresses or vulnerability details; the only hits in this document are statements that none were recorded |
| AC-BI-013 | MET | Sibling-issue review section above |
