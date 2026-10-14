<!-- © 2026 Cartman ApS. All rights reserved. -->

# Test module classification

| Module | Tests | Class | Reason |
| --- | --- | --- | --- |
| `ps-cli/tests/modules/test_auth_handlers.py` | 20 | keep | Tests for ps_cli.modules.auth_handlers. |
| `ps-cli/tests/modules/test_config_handlers.py` | 15 | keep | Tests for ps_cli.modules.config_handlers. |
| `ps-cli/tests/modules/test_handlers.py` | 7 | keep | Tests for ps_cli.modules.handlers. |
| `ps-cli/tests/modules/test_parser.py` | 19 | keep | Tests for ps_cli.modules.parser: the `_context_name_type`, `_service_url_type` and |
| `ps-cli/tests/modules/test_parser_help_provenance_free.py` | 2 | keep | Every `ps-cli ... --help` screen is end-user text and must carry no provenance. |
| `ps-cli/tests/release/test_conftest_mock_github.py` | 1 | keep | Smoke test for the fake-`curl` test harness. |
| `ps-cli/tests/release/test_install_sh.py` | 7 | keep | Tests for the rewritten `install.sh`. |
| `ps-cli/tests/test_architecture_boundary.py` | 2 | keep | ps-cli must only ever reach PS Service over REST, never a direct dependency. |
| `ps-cli/tests/test_cli.py` | 42 | rewrite | identical-body pair claims distinct requirements (#268) |
| `ps-cli/tests/test_config.py` | 22 | keep | Tests for ps_cli.config: CliConfig resolution (packaged default / override / env var). |
| `ps-cli/tests/test_credentials.py` | 23 | keep | Tests for ps_cli.credentials: `CredentialStore`/`PersistenceBackend` Protocols and |
| `ps-cli/tests/test_device_flow.py` | 27 | keep | Tests for ps_cli.device_flow. |
| `ps-cli/tests/test_errors.py` | 5 | keep | Tests for ps_cli.errors: PsCliError and assert_contract(). |
| `ps-cli/tests/test_grant_access_role_client.py` | 5 | keep | Tests for `scripts/grant_access_role_client.py::resolve_bearer_token`. |
| `ps-cli/tests/test_http_client.py` | 36 | keep | Tests for ps_cli.http_client: PsServiceClient construction, insecure-URL warning, |
| `ps-cli/tests/test_intake_validation.py` | 5 | keep | Tests for ps_cli.intake_validation: validate_local_seed_file. |
| `ps-cli/tests/test_integration_auth_full_cycle.py` | 2 | keep | Capstone IdP-agnostic verification test. |
| `ps-cli/tests/test_integration_version.py` | 2 | keep | Integration test: `ps-cli --version` against one or more real spawned `ps-service`s. |
| `ps-cli/tests/test_oidc_discovery.py` | 28 | keep | Tests for ps_cli.oidc_discovery. |
| `ps-cli/tests/test_packaging.py` | 3 | keep | `ps-cli` must declare a console-script entry point in its packaging metadata. |
| `ps-cli/tests/test_render.py` | 8 | keep | Tests for ps_cli.render. |
| `ps-cli/tests/test_schema_packaging_matches_docs_source.py` | 1 | keep | `ps-cli`'s packaged schema copy must stay byte-identical to its docs source. |
| `ps-cli/tests/test_targets.py` | 13 | keep | Tests for ps_cli.targets: resolve_config_dir(), TargetsFile, load_targets(), write_targets(). |
| `ps-cli/tests/test_tls.py` | 8 | keep | ps-cli verifies TLS against the OS trust store via `truststore`. |
| `ps-cli/tests/test_toml_writer.py` | 7 | keep | Tests for ps_cli.toml_writer: escape_basic_string(), |
| `ps-service/tests/api/test_app_wiring.py` | 2 | keep | Tests that `create_app` mounts the REST router without disturbing the harness routes. |
| `ps-service/tests/api/test_catalog.py` | 6 | keep | Unit tests for the curated regulation catalog (`ps_service.api.catalog`). |
| `ps-service/tests/api/test_catalog_loading.py` | 2 | keep | Tests for `ps_service.api.catalog.load_regulation_catalog` . |
| `ps-service/tests/api/test_change_check_gateway.py` | 3 | keep | The amendment sweep resolves the Graph Write Gateway first. |
| `ps-service/tests/api/test_change_check_orchestration.py` | 18 | keep | Unit tests for `run_change_check_sweep`. |
| `ps-service/tests/api/test_change_check_orchestration_audit.py` | 17 | keep | Audit emission of the `check_regulations` / `POST /change-checks` sweep. |
| `ps-service/tests/api/test_change_check_orchestration_failures.py` | 5 | keep | Failure injection and retry for the `check_regulations` sweep's full pipeline. |
| `ps-service/tests/api/test_change_check_orchestration_pipeline.py` | 9 | keep | The `check_regulations` sweep runs the full pipeline, succession last. |
| `ps-service/tests/api/test_change_check_pipeline_runner.py` | 10 | keep | The sweep's injected `PipelineRunner`: shared stage sequence, config first, no leaks. |
| `ps-service/tests/api/test_deferred_surface_documented.py` | 1 | keep | The api package docstring documents the interim access posture and the deferred remote surface. |
| `ps-service/tests/api/test_error_handlers.py` | 14 | keep | Tests for ``ps_service.api.error_handlers`` . |
| `ps-service/tests/api/test_error_handlers_export.py` | 4 | keep | Tests for the ``POST /exports`` error mapping (`ps_service.api.errors` / |
| `ps-service/tests/api/test_error_handlers_restoration.py` | 5 | keep | Tests for the ``POST /restorations`` error mapping (`ps_service.api.errors` / |
| `ps-service/tests/api/test_export_orchestration.py` | 10 | keep | Tests for `ps_service.api.export_orchestration`. |
| `ps-service/tests/api/test_ingest_path_catalog_independence.py` | 2 | keep | Static guard: the ingest path never references or reads the curated catalog. |
| `ps-service/tests/api/test_ingestion_identity_live.py` | 5 | keep | Live FalkorDB proof of the case-insensitive identity queries. |
| `ps-service/tests/api/test_ingestion_orchestration.py` | 62 | rewrite | identical-body pair claims distinct requirements (#268) |
| `ps-service/tests/api/test_ingestion_orchestration_audit.py` | 15 | keep | Audit emission of the synchronous ingestion orchestration. |
| `ps-service/tests/api/test_ingestion_orchestration_live.py` | 3 | keep | Live verification: `resolve_via_cellar` against the real Cellar/ELI service. |
| `ps-service/tests/api/test_ingestion_parity.py` | 1 | keep | MCP and REST sync ingests record equivalent `ingestion_run` rows. |
| `ps-service/tests/api/test_ingestion_preflight_logs_nothing.py` | 5 | keep | A rejected pre-flight returns today's error and logs nothing to the graph. |
| `ps-service/tests/api/test_ingestion_request_models.py` | 7 | keep | Unit tests for the ``POST /ingestions`` request models (`ps_service.api.models`). |
| `ps-service/tests/api/test_ingestion_status.py` | 2 | keep | HTTP tests for ``GET /ingestions/{run_id}`` -- the live in-flight-stage read. |
| `ps-service/tests/api/test_ingestions_catalog.py` | 39 | rewrite | identical-body pair claims distinct requirements (#268) |
| `ps-service/tests/api/test_ingestions_catalog_gateway.py` | 5 | keep | ``POST /ingestions`` (catalog) writes the native graph through the gateway. |
| `ps-service/tests/api/test_ingestions_internal.py` | 11 | keep | HTTP tests for ``POST /ingestions`` -- the internal-document ingestion path. |
| `ps-service/tests/api/test_ingestions_internal_gateway.py` | 8 | keep | ``POST /ingestions`` (internal) writes the seed's instrument through the gateway. |
| `ps-service/tests/api/test_ingestions_internal_governance_counts.py` | 3 | keep | The imported policy tree's counts reach the response and the run log. |
| `ps-service/tests/api/test_ingestions_internal_owner.py` | 3 | keep | ``POST /ingestions`` (internal) refuses a governance-bearing seed with no verified caller. |
| `ps-service/tests/api/test_live_capstone_export.py` | 6 | keep | The ``falkordb_live``/``llm_live`` export capstone. |
| `ps-service/tests/api/test_live_capstone_external.py` | 3 | keep | The live external ingestion capstone. |
| `ps-service/tests/api/test_live_capstone_internal.py` | 4 | keep | The live internal-regulation ingestion capstone. |
| `ps-service/tests/api/test_live_ingest_internal_engprac.py` | 3 | keep | Live proof that `POST /ingestions` imports the engineering-practices tree as caller-owned drafts. |
| `ps-service/tests/api/test_live_restorations_engprac.py` | 1 | keep | Restore the shipped ENGPRAC-1.0 artifact over ``POST /restorations``. |
| `ps-service/tests/api/test_max_body_size_middleware.py` | 8 | keep | Tests for `ServiceConfig.max_request_body_bytes` + `main._MaxBodySizeMiddleware`. |
| `ps-service/tests/api/test_models_export.py` | 11 | keep | Unit tests for the ``POST /exports`` request/response models (`ps_service.api.models`) |
| `ps-service/tests/api/test_models_restoration.py` | 4 | keep | Unit tests for the ``POST /restorations`` request/response models (`ps_service.api.models`). |
| `ps-service/tests/api/test_near_miss_audit_actions.py` | 5 | keep | `near_miss.resolve` audit action registration and details model. |
| `ps-service/tests/api/test_near_miss_parity.py` | 1 | keep | MCP and REST keep-separate record the same `near_miss.resolve` row. |
| `ps-service/tests/api/test_near_miss_review_orchestration.py` | 14 | keep | Audit emission of `run_resolve_near_miss`. |
| `ps-service/tests/api/test_no_drift_surface.py` | 2 | keep | The drift detector adds no REST route, no MCP tool and no `/ready` field. |
| `ps-service/tests/api/test_openapi_provenance_free.py` | 3 | keep | The served OpenAPI document carries no provenance in any description, summary or title. |
| `ps-service/tests/api/test_protected_resource_metadata.py` | 9 | keep | HTTP tests for `GET /.well-known/oauth-protected-resource`. |
| `ps-service/tests/api/test_rest_auth_middleware.py` | 10 | keep | HTTP tests for `RestAuthMiddleware` -- the REST-side 401 path. |
| `ps-service/tests/api/test_restorations_from_catalog.py` | 23 | keep | HTTP tests for `POST /restorations/from-catalog`. |
| `ps-service/tests/api/test_restore_and_sweep_isolated_from_short_name_normalization.py` | 1 | keep | Static guard: restore and the sweep never use ingest-side short_name normalization. |
| `ps-service/tests/api/test_restore_orchestration.py` | 16 | keep | Tests for `ps_service.api.restore_orchestration`. |
| `ps-service/tests/api/test_restore_orchestration_audit.py` | 23 | keep | Audit emission of the restore orchestration. |
| `ps-service/tests/api/test_restore_orchestration_content_rejection.py` | 1 | keep | ``run_restoration`` with the REAL ``restore_instrument`` delegate, DB-free. |
| `ps-service/tests/api/test_restore_parity.py` | 1 | keep | MCP and REST catalog restores record the same `instrument.restore` rows. |
| `ps-service/tests/api/test_route_allowlist_audit.py` | 5 | keep | Route allow-list default-deny audit. |
| `ps-service/tests/api/test_routes_catalog.py` | 8 | keep | HTTP tests for `GET /catalog` (the runtime curated-content fetch). |
| `ps-service/tests/api/test_routes_change_checks.py` | 12 | keep | HTTP tests for `POST /change-checks`. |
| `ps-service/tests/api/test_routes_exports.py` | 17 | keep | HTTP tests for `POST /exports`. |
| `ps-service/tests/api/test_routes_near_misses.py` | 26 | keep | HTTP tests for `GET /near-misses`, `POST /near-misses/{review_id}/resolve`, and |
| `ps-service/tests/api/test_routes_restorations.py` | 18 | keep | HTTP tests for `POST /restorations` . |
| `ps-service/tests/api/test_run_context.py` | 5 | keep | End-to-end run-id propagation tests for the PS Service REST API. |
| `ps-service/tests/api/test_run_status.py` | 5 | keep | Tests for ``ps_service.api.run_status`` (the process-wide run-stage registry). |
| `ps-service/tests/api/test_schema_packaging_matches_docs_source.py` | 1 | keep | D7: `ps-service`'s packaged schema copy must stay byte-identical to its docs source. |
| `ps-service/tests/api/test_stage_failure_logging.py` | 6 | keep | A failed pipeline stage's detail reaches the operator log, scrubbed. |
| `ps-service/tests/api/test_unhandled_request_error_logging.py` | 10 | keep | An exception no handler names reaches the operator log, scrubbed. |
| `ps-service/tests/audit/test_actor.py` | 4 | keep | Shared audit-actor resolution (helper shared by the audit emitters). |
| `ps-service/tests/audit/test_component_independence.py` | 3 | keep | `audit` and `persistence` stay component-independent. |
| `ps-service/tests/audit/test_emit.py` | 12 | keep | Audit lifecycle primitives: fail-closed opening row, best-effort follow-up row. |
| `ps-service/tests/audit/test_migrations.py` | 4 | keep | Tests for audit migration 0002: `details` filter expression indexes. |
| `ps-service/tests/audit/test_models.py` | 11 | keep | Tests for `ps_service.audit.models`. |
| `ps-service/tests/audit/test_store_event_id.py` | 3 | keep | `record_standalone_with_event_id` hands back the inserted row's id. |
| `ps-service/tests/audit/test_store_no_mutation_methods.py` | 2 | keep | Structural proof that `AuditStore` exposes no mutation/deletion method. |
| `ps-service/tests/audit/test_store_query.py` | 7 | keep | Tests for `ps_service.audit.store.PsycopgAuditStore.query`. |
| `ps-service/tests/audit/test_store_record.py` | 7 | keep | Tests for `ps_service.audit.store.PsycopgAuditStore.record`. |
| `ps-service/tests/auth/test_algorithm_allowlist.py` | 4 | keep | Algorithm allow-list enforcement. |
| `ps-service/tests/auth/test_audit_logging.py` | 12 | keep | The auth audit-logging contract. |
| `ps-service/tests/auth/test_jwks_refresh.py` | 2 | keep | JWKS kid-miss refetch-once. |
| `ps-service/tests/auth/test_startup_fail_closed.py` | 10 | keep | PS Service refuses to start on absent config or failed discovery. |
| `ps-service/tests/authz/test_bootstrap_owner_startup_fail_closed.py` | 5 | keep | PS Service refuses to start when the RBAC bootstrap owner identity |
| `ps-service/tests/authz/test_require_access_role_dependency.py` | 5 | keep | Direct unit tests for `require_access_role`. |
| `ps-service/tests/authz/test_service.py` | 53 | rewrite | identical-body pair claims distinct requirements (#268); one duplicate test removed |
| `ps-service/tests/authz/test_store_transactional_audit.py` | 15 | keep | Tests proving `PsycopgAccessRoleStore` writes through `AuditStore` in-transaction. |
| `ps-service/tests/change_monitor/test_cellar_consolidated.py` | 15 | keep | Tests for `ps_service.change_monitor.cellar_consolidated`. |
| `ps-service/tests/change_monitor/test_errors.py` | 7 | keep | Tests for `ps_service.change_monitor.errors`. |
| `ps-service/tests/change_monitor/test_falkordb_client.py` | 9 | keep | Tests for `ps_service.change_monitor.falkordb_client`. |
| `ps-service/tests/change_monitor/test_falkordb_client_read_only.py` | 5 | keep | Change Monitor reads through the gateway's `ReadOnlyGraph` and imports no driver. |
| `ps-service/tests/change_monitor/test_frozen_reference.py` | 12 | keep | The frozen legacy reference is what the pre-gateway succession writer was. |
| `ps-service/tests/change_monitor/test_graph_reader.py` | 5 | keep | Tests for `ps_service.change_monitor.graph_reader`. |
| `ps-service/tests/change_monitor/test_live_capstone.py` | 2 | keep | Live end-to-end capstone — run for real against the live CELLAR/ELI service |
| `ps-service/tests/change_monitor/test_live_sweep_full_pipeline.py` | 4 | keep | Live FalkorDB proof of the full-pipeline amendment re-ingest. |
| `ps-service/tests/change_monitor/test_models.py` | 9 | keep | Tests for `ps_service.change_monitor.models`. |
| `ps-service/tests/change_monitor/test_no_graph_write_path.py` | 13 | keep | Regulatory Change Monitor holds no write path to FalkorDB. |
| `ps-service/tests/change_monitor/test_poll.py` | 8 | keep | Tests for `ps_service.change_monitor.poll`. |
| `ps-service/tests/change_monitor/test_regulation_independence.py` | 2 | keep | The regulation-independence AST scan, extended to cover the |
| `ps-service/tests/change_monitor/test_reingestion_docs_content.py` | 7 | keep | Content lint: the docs, skill and docstrings describe the full UC-4 re-ingest. |
| `ps-service/tests/change_monitor/test_reingestion_graph_state.py` | 8 | keep | Hermetic graph-level proof of an amendment re-ingest, on stateful graphs. |
| `ps-service/tests/change_monitor/test_reingestion_state_machine.py` | 5 | keep | Truth table for `classify_reingestion`. |
| `ps-service/tests/change_monitor/test_succession.py` | 12 | keep | Tests for `ps_service.change_monitor.succession`. |
| `ps-service/tests/change_monitor/test_succession_equivalence.py` | 2 | keep | The gateway-backed succession builds the graph the frozen legacy writer built. |
| `ps-service/tests/change_monitor/test_succession_gateway.py` | 22 | keep | The re-ingest succession is written only as Graph Write Gateway groups. |
| `ps-service/tests/change_monitor/test_succession_live.py` | 2 | keep | Live FalkorDB anchor for the frozen succession statements. |
| `ps-service/tests/change_monitor/test_succession_requests_checkpoints.py` | 5 | keep | The succession's terminal groups request a digest checkpoint. |
| `ps-service/tests/change_monitor/test_sweep_reingest_audit.py` | 1 | keep | The sweep's audited counts equal the graph delta of an amended re-ingest. |
| `ps-service/tests/change_monitor/test_trigger.py` | 22 | keep | Tests for `ps_service.change_monitor.trigger.trigger_reingestion` and `will_reingest`. |
| `ps-service/tests/company_merge/test_ac008_out_of_scope.py` | 2 | keep | Role/Requirement/Obligation dedup is out of scope for |
| `ps-service/tests/company_merge/test_check_baseline_preconditions_cli.py` | 4 | keep | Fake-wiring proof for `tools/company-merge/check_baseline_preconditions.py`. |
| `ps-service/tests/company_merge/test_check_baseline_preconditions_cli_live.py` | 1 | keep | Live regression test for `tools/company-merge/check_baseline_preconditions.py`. |
| `ps-service/tests/company_merge/test_company_merge_similarity_sweep_cli.py` | 7 | keep | Tests for `tools/company-merge/company_merge_similarity_sweep.py`'s CLI surface |
| `ps-service/tests/company_merge/test_curated_content_capability_loader.py` | 4 | keep | Tests for `tools/company-merge/company_merge_similarity_sweep.py`'s curated-content |
| `ps-service/tests/company_merge/test_dedup_abort_on_embedding_failure.py` | 1 | keep | Tests for `ps_service.company_merge.dedup.dedupe_canonical_nodes`: |
| `ps-service/tests/company_merge/test_dedup_combined_resolution.py` | 5 | keep | Tests for `ps_service.company_merge.dedup.dedupe_canonical_nodes`: |
| `ps-service/tests/company_merge/test_dedup_exact_match.py` | 6 | keep | Tests for `ps_service.company_merge.dedup`: `read_existing_canonical_index` and |
| `ps-service/tests/company_merge/test_dedup_false_convergence.py` | 1 | keep | Tests for `ps_service.company_merge.dedup.dedupe_canonical_nodes`: |
| `ps-service/tests/company_merge/test_dedup_incoming_embedding_cache.py` | 4 | keep | Tests for caching the INCOMING side of Company Merge's |
| `ps-service/tests/company_merge/test_dedup_merged_redirect.py` | 5 | keep | Live exact-key redirect over `merged` Capability tombstones. |
| `ps-service/tests/company_merge/test_dedup_near_miss.py` | 1 | keep | Tests for `ps_service.company_merge.dedup.dedupe_canonical_nodes`: near-miss surfacing -- |
| `ps-service/tests/company_merge/test_dedup_offline_exact_match.py` | 4 | keep | Tests for `ps_service.company_merge.dedup.resolve_capability_convergence_offline`: |
| `ps-service/tests/company_merge/test_dedup_offline_in_batch_convergence.py` | 2 | keep | Tests for `ps_service.company_merge.dedup.resolve_capability_convergence_offline`: |
| `ps-service/tests/company_merge/test_dedup_offline_merged_redirect.py` | 4 | keep | Offline exact-key redirect over `merged` Capability tombstones. |
| `ps-service/tests/company_merge/test_dedup_offline_no_route_embedding_import.py` | 4 | keep | AST-scan proof that |
| `ps-service/tests/company_merge/test_dedup_offline_policy_convergence.py` | 3 | keep | Tests for `ps_service.company_merge.dedup.resolve_capability_convergence_offline` |
| `ps-service/tests/company_merge/test_dedup_offline_semantic_match.py` | 4 | keep | Tests for `ps_service.company_merge.dedup.resolve_capability_convergence_offline`: |
| `ps-service/tests/company_merge/test_dedup_policy_convergence.py` | 2 | keep | Tests for `ps_service.company_merge.dedup.dedupe_canonical_nodes` with `kind="Policy"`: |
| `ps-service/tests/company_merge/test_dedup_semantic_match.py` | 11 | keep | Tests for `ps_service.company_merge.dedup.find_best_semantic_match`. |
| `ps-service/tests/company_merge/test_external_flow_leaves_a_head_checkpoint_on_every_touched_graph.py` | 1 | keep | An external ingest, Domain Mapper and Company Merge run leaves head checkpoints. |
| `ps-service/tests/company_merge/test_falkordb_client.py` | 8 | keep | Tests for `ps_service.company_merge.falkordb_client`. |
| `ps-service/tests/company_merge/test_falkordb_client_read_only.py` | 5 | keep | Company Merge reads through the gateway's `ReadOnlyGraph` and imports no driver. |
| `ps-service/tests/company_merge/test_generated_values_replay.py` | 4 | keep | Generated values are fixed before submission and replay-identical. |
| `ps-service/tests/company_merge/test_governance_import.py` | 8 | keep | Tests for `ps_service.company_merge.governance_import`. |
| `ps-service/tests/company_merge/test_graph_reader.py` | 13 | keep | Tests for `ps_service.company_merge.graph_reader`. |
| `ps-service/tests/company_merge/test_graph_reader_draft_governance.py` | 4 | keep | Tests for `graph_reader.read_baseline_graph`'s governance read. |
| `ps-service/tests/company_merge/test_graph_reader_vocabulary_drift.py` | 6 | keep | `graph_reader`'s read vocabulary must equal internal_seed's |
| `ps-service/tests/company_merge/test_graph_writer_canonical_nodes.py` | 11 | keep | Tests for `ps_service.company_merge.graph_writer.persist_canonical_nodes` |
| `ps-service/tests/company_merge/test_graph_writer_classification_validation.py` | 7 | keep | Tests for `ps_service.company_merge.graph_writer.validate_classification_edge_endpoints` |
| `ps-service/tests/company_merge/test_graph_writer_edge_rewiring.py` | 21 | keep | Tests for `ps_service.company_merge.graph_writer.persist_rewired_edges`, the edge writer. |
| `ps-service/tests/company_merge/test_graph_writer_embedding_backfill.py` | 11 | keep | Tests for the embedding backfill writers of `ps_service.company_merge.graph_writer`. |
| `ps-service/tests/company_merge/test_graph_writer_governance_drafts.py` | 14 | keep | Tests for `ps_service.company_merge.graph_writer.persist_governance_drafts`. |
| `ps-service/tests/company_merge/test_graph_writer_passthrough.py` | 9 | keep | Tests for `ps_service.company_merge.graph_writer. |
| `ps-service/tests/company_merge/test_graph_writer_primitive_builders.py` | 6 | keep | The pure builders `graph_writer` exposes for Restore. |
| `ps-service/tests/company_merge/test_identity_reuse.py` | 6 | keep | Identity-reuse proof: |
| `ps-service/tests/company_merge/test_legacy_reference_characterisation.py` | 7 | keep | The frozen legacy reference and today's production build the same graph. |
| `ps-service/tests/company_merge/test_legacy_resolve_reference.py` | 13 | keep | The frozen near-miss resolution statements and their interpreter. |
| `ps-service/tests/company_merge/test_live_capstone.py` | 1 | keep | The live 3-regulation Company Merge capstone. |
| `ps-service/tests/company_merge/test_live_merge_approval.py` | 8 | keep | Tests for ps_service.company_merge.live_merge_approval. |
| `ps-service/tests/company_merge/test_live_merge_assertions.py` | 4 | keep | Tests for the 3 snapshot functions in `_live_merge_assertions.py`: |
| `ps-service/tests/company_merge/test_live_policy_system_verification_idempotency.py` | 4 | keep | A second merge creates no duplicate canonical nodes or edges (hermetic). |
| `ps-service/tests/company_merge/test_live_policy_system_verification_pass1.py` | 8 | keep | Verification of the first merge into the single-tenant graph, made hermetic. |
| `ps-service/tests/company_merge/test_merge_baseline_graph.py` | 22 | keep | Tests for `ps_service.company_merge.merge.merge_baseline_graph` |
| `ps-service/tests/company_merge/test_merge_baseline_graph_counts.py` | 9 | keep | `merge_baseline_graph` reports net-new / matched Capability counts and the new-Obligation count. |
| `ps-service/tests/company_merge/test_merge_baseline_graph_gateway.py` | 13 | keep | `merge_baseline_graph` writes the regulatory spine through the Graph Write Gateway. |
| `ps-service/tests/company_merge/test_merge_capability_gateway.py` | 7 | keep | `merge_baseline_graph` mints canonical Capability nodes through the Graph Write Gateway. |
| `ps-service/tests/company_merge/test_merge_classification_gateway.py` | 12 | keep | `merge_baseline_graph` writes PracticeArea/RiskPath through the Graph Write Gateway. |
| `ps-service/tests/company_merge/test_merge_end_to_end_equivalence.py` | 4 | keep | `merge_baseline_graph` end to end against the frozen legacy reference. |
| `ps-service/tests/company_merge/test_merge_failure_mapping.py` | 9 | keep | Gateway failures reach Company Merge callers as `CompanyMergePersistenceError`. |
| `ps-service/tests/company_merge/test_merge_idempotency.py` | 4 | keep | Unit-level idempotent re-run: calling `merge_baseline_graph` TWICE with IDENTICAL |
| `ps-service/tests/company_merge/test_merge_internal_seed_draft_governance.py` | 6 | keep | `merge_baseline_graph` over the real engineering-practices seed. |
| `ps-service/tests/company_merge/test_merge_obligations_gateway.py` | 11 | keep | `merge_baseline_graph` writes the Obligation nodes through the Graph Write Gateway. |
| `ps-service/tests/company_merge/test_merge_requests_checkpoints.py` | 4 | keep | `merge_baseline_graph` requests a digest checkpoint on its terminal groups. |
| `ps-service/tests/company_merge/test_merge_rewired_edges_gateway.py` | 8 | keep | `merge_baseline_graph` writes the rewired edges through the Graph Write Gateway. |
| `ps-service/tests/company_merge/test_merge_test_doubles_read_only.py` | 4 | keep | The merge test doubles refuse graph writes instead of interpreting them. |
| `ps-service/tests/company_merge/test_merge_writer_failure_matrix.py` | 3 | keep | Every Company Merge stage writer maps a gateway failure the same way. |
| `ps-service/tests/company_merge/test_models.py` | 25 | keep | Tests for ps_service.company_merge.models. |
| `ps-service/tests/company_merge/test_no_graph_write_path.py` | 9 | keep | Company Merge holds no write path to FalkorDB. |
| `ps-service/tests/company_merge/test_obligation_redirect.py` | 15 | keep | Company Merge follows `MergedObligation` markers. |
| `ps-service/tests/company_merge/test_pending_review_get.py` | 3 | keep | `pending_review.get_pending_review`: a single review by id, WITHOUT the resolvable |
| `ps-service/tests/company_merge/test_pending_review_list.py` | 5 | keep | Tests for `ps_service.company_merge.pending_review.list_pending_reviews`: |
| `ps-service/tests/company_merge/test_pending_review_persist.py` | 11 | keep | Tests for `ps_service.company_merge.pending_review.persist_pending_reviews`. |
| `ps-service/tests/company_merge/test_pending_review_resolve.py` | 27 | keep | Tests for `ps_service.company_merge.pending_review.resolve_review`: |
| `ps-service/tests/company_merge/test_pending_review_resolve_tombstone.py` | 5 | keep | Near-miss merge and `merged` Capability tombstones. |
| `ps-service/tests/company_merge/test_preconditions.py` | 5 | keep | Tests for `ps_service.company_merge.preconditions`. |
| `ps-service/tests/company_merge/test_preconditions_live.py` | 1 | keep | Live regression test for `check_obligation_has_edge_cardinality`. |
| `ps-service/tests/company_merge/test_resolve_failure_matrix.py` | 3 | keep | Both `resolve_review` decisions map a gateway failure like every stage writer. |
| `ps-service/tests/company_merge/test_run_live_merge_cli.py` | 11 | keep | Fake-wiring proof for `tools/company-merge/run_live_merge.py`. |
| `ps-service/tests/company_merge/test_run_live_merge_cli_live.py` | 1 | keep | Live wiring smoke test for `tools/company-merge/run_live_merge.py`. |
| `ps-service/tests/company_merge/test_similarity.py` | 6 | keep | Tests for ps_service.company_merge.similarity. |
| `ps-service/tests/company_merge/test_similarity_threshold_dataset.py` | 10 | keep | Tests for `tools/company-merge/company_merge_similarity_sweep.py`'s labeled dataset. |
| `ps-service/tests/company_merge/test_similarity_threshold_sweep_recommendation.py` | 6 | keep | Tests for `tools/company-merge/company_merge_similarity_sweep.py`'s recommendation |
| `ps-service/tests/company_merge/test_similarity_threshold_sweep_scoring.py` | 22 | keep | Tests for `tools/company-merge/company_merge_similarity_sweep.py`. |
| `ps-service/tests/company_merge/test_uc3_round_trip.py` | 1 | keep | The real governance round-trip query proof. |
| `ps-service/tests/curated_source/test_artifact_client.py` | 6 | keep | Tests for ``ps_service.curated_source.artifact_client.fetch_artifact``. |
| `ps-service/tests/curated_source/test_catalog_client.py` | 9 | keep | Tests for ``ps_service.curated_source.catalog_client.fetch_catalog``. |
| `ps-service/tests/curated_source/test_config_key.py` | 6 | keep | Tests for the catalog-source runtime-config key. |
| `ps-service/tests/curated_source/test_http_fetch.py` | 4 | keep | Tests for ``ps_service.curated_source.http_fetch.fetch_bytes``. |
| `ps-service/tests/curated_source/test_instrument_lookup.py` | 5 | keep | Tests for ``ps_service.curated_source.instrument_lookup.resolve_canonical_instrument_id``. |
| `ps-service/tests/curated_source/test_manifest_parser.py` | 7 | keep | Tests for ``ps_service.curated_source.manifest_parser.parse_manifest_json``. |
| `ps-service/tests/curated_source/test_no_falkordb_dependency.py` | 2 | keep | `curated_source` has no FalkorDB/`redis` dependency of its own. |
| `ps-service/tests/curated_source/test_override_live.py` | 5 | keep | `postgres_live` tests for the catalog-source override against a real Postgres. |
| `ps-service/tests/curated_source/test_resolve.py` | 8 | keep | Tests for `ps_service.curated_source.resolve.resolve_effective_source`. |
| `ps-service/tests/curated_source/test_source_url.py` | 5 | keep | Tests for ``ps_service.curated_source.source_url.validate_source_url`` (D-VALIDATION). |
| `ps-service/tests/curated_source/test_store.py` | 7 | keep | Tests for `ps_service.curated_source.store`, the thin catalog-override adapter. |
| `ps-service/tests/dependency_health/test_registry.py` | 7 | keep | Unit tests for `ps_service.dependency_health.registry`. |
| `ps-service/tests/deploy_eval/test_eval_script.py` | 61 | keep | Tests for `scripts/deploy-ps-eval.sh`. |
| `ps-service/tests/deploy_eval/test_eval_script_integration.py` | 1 | keep | Real-`openssl` run of `scripts/deploy-ps-eval.sh`. |
| `ps-service/tests/deploy_eval/test_eval_static.py` | 5 | keep | Static structure checks for `scripts/deploy-ps-eval.sh`. |
| `ps-service/tests/deploy_lib/test_authentik_owner.py` | 26 | keep | Tests for `scripts/lib/authentik-owner.sh`. |
| `ps-service/tests/deploy_lib/test_fake_curl.py` | 22 | keep | Smoke tests for the shared test fakes. |
| `ps-service/tests/deploy_lib/test_local_tls.py` | 10 | keep | Logic tests for `scripts/lib/local-tls.sh` with a fake `openssl`. |
| `ps-service/tests/deploy_lib/test_local_tls_integration.py` | 9 | keep | Real-`openssl`, real-TLS-handshake tests for `scripts/lib/local-tls.sh`. |
| `ps-service/tests/deploy_lib/test_port_forward.py` | 6 | keep | Tests for the loopback `kubectl port-forward` helper in `scripts/lib/authentik-owner.sh`: |
| `ps-service/tests/deploy_llm/test_account_provisioning_state.py` | 3 | keep | ensure_account must not treat an existing-but-not-yet-provisioned AIServices account as ready |
| `ps-service/tests/deploy_llm/test_capacity_validation.py` | 4 | keep | Capacity-range validation against LLM_REGION's live-reported range (see |
| `ps-service/tests/deploy_llm/test_config_validation.py` | 8 | keep | Config validation for `scripts/deploy-llm.sh`. |
| `ps-service/tests/deploy_llm/test_confirmation_table.py` | 7 | keep | Naming + confirmation table + `[Y/n]` prompt for `scripts/deploy-llm.sh`. |
| `ps-service/tests/deploy_llm/test_decline_path.py` | 2 | keep | Decline path: answering `N` at the confirmation prompt. |
| `ps-service/tests/deploy_llm/test_deleted_account_purge.py` | 3 | keep | Soft-deleted AIServices account detection and explicit purge confirmation. |
| `ps-service/tests/deploy_llm/test_idempotent_rerun.py` | 4 | keep | Idempotent rerun against the same subscription/config makes zero changes. |
| `ps-service/tests/deploy_llm/test_provisioning.py` | 6 | keep | Core provisioning against a fresh subscription: create-if-absent chain + three-secret write. |
| `ps-service/tests/deploy_llm/test_quota_check.py` | 5 | keep | Quota hard stop at LLM_REGION (see `fail_region_not_viable` in scripts/deploy-llm.sh). |
| `ps-service/tests/deploy_llm/test_rbac_preflight.py` | 5 | keep | RBAC preflight for `scripts/deploy-llm.sh`. |
| `ps-service/tests/deploy_llm/test_region_selection.py` | 4 | keep | Region targeting: LLM_REGION used as configured, never auto-switched (see |
| `ps-service/tests/deploy_llm/test_rotate_key.py` | 7 | keep | `--rotate-key` mode: active-slot detection + regenerate-the-inactive-key . |
| `ps-service/tests/deploy_llm/test_sync_context_guard.py` | 3 | keep | `scripts/sync-llm-secrets-to-kind.sh`: kind-context guard. |
| `ps-service/tests/deploy_llm/test_sync_idempotent_update.py` | 3 | keep | `scripts/sync-llm-secrets-to-kind.sh`: idempotent rerun. |
| `ps-service/tests/deploy_llm/test_sync_secret_write.py` | 6 | keep | `scripts/sync-llm-secrets-to-kind.sh`: secret write, including the raw key values. |
| `ps-service/tests/deploy_ps/test_aks_cluster_creation.py` | 5 | keep | AKS cluster creation with AAD+RBAC+hardening flags for `scripts/deploy-ps-prod.sh`. |
| `ps-service/tests/deploy_ps/test_aks_vm_size_preflight.py` | 4 | keep | AKS node VM-size allowlist + vCPU quota preflight for `scripts/deploy-ps-prod.sh`. |
| `ps-service/tests/deploy_ps/test_authentik_ingress.py` | 6 | keep | Authentik's own Ingress object for `scripts/deploy-ps-prod.sh`. |
| `ps-service/tests/deploy_ps/test_capacity_validation.py` | 4 | keep | Capacity-range validation against the selected region's live-reported range, |
| `ps-service/tests/deploy_ps/test_cert_manager_and_cluster_issuer.py` | 4 | keep | cert-manager install + wait-for-Available before `ClusterIssuer` for `scripts/deploy-ps-prod.sh`. |
| `ps-service/tests/deploy_ps/test_config_validation.py` | 11 | keep | Config validation for `scripts/deploy-ps-prod.sh`. |
| `ps-service/tests/deploy_ps/test_confirmation_table.py` | 6 | keep | Naming + confirmation table + `[Y/n]` prompt for `scripts/deploy-ps-prod.sh`. |
| `ps-service/tests/deploy_ps/test_decline_path.py` | 2 | keep | Decline path: answering `N` at the confirmation prompt. |
| `ps-service/tests/deploy_ps/test_happy_path.py` | 4 | keep | Full happy-path capstone: fresh run + no-op rerun (end-to-end idempotency and the |
| `ps-service/tests/deploy_ps/test_helm_release.py` | 15 | keep | Helm release: Authentik-issuer wiring + narrow no-op comparison. |
| `ps-service/tests/deploy_ps/test_llm_provisioning.py` | 5 | keep | Core LLM resource provisioning: create-if-absent chain + three-secret write . Own copy of |
| `ps-service/tests/deploy_ps/test_llm_secret_sync.py` | 3 | keep | LLM secret sync into AKS + `values-prod.yaml` direct reference. |
| `ps-service/tests/deploy_ps/test_no_authentik_or_signing_keyvault_calls.py` | 3 | keep | Script-level proof that `scripts/deploy-ps-prod.sh` makes no Key Vault or kubectl calls for |
| `ps-service/tests/deploy_ps/test_owner_creation.py` | 10 | keep | First-owner creation in `scripts/deploy-ps-prod.sh`. |
| `ps-service/tests/deploy_ps/test_provider_registration.py` | 3 | keep | Resource-provider registration + poll for `scripts/deploy-ps-prod.sh`. |
| `ps-service/tests/deploy_ps/test_ps_service_ingress.py` | 4 | keep | PS Service Ingress for `scripts/deploy-ps-prod.sh`. |
| `ps-service/tests/deploy_ps/test_public_exposure_networking.py` | 5 | keep | Public exposure: application-routing add-on + DNS label for `scripts/deploy-ps-prod.sh`. |
| `ps-service/tests/deploy_ps/test_quota_check.py` | 8 | keep | Quota preflight at the selected region, covering the real per-model+SKU usage key, the |
| `ps-service/tests/deploy_ps/test_rbac_preflight.py` | 4 | keep | RBAC preflight for `scripts/deploy-ps-prod.sh` (user-only, no |
| `ps-service/tests/deploy_ps/test_region_selection.py` | 3 | keep | Region selection: `select_region` probes `LLM_REGION_CANDIDATES` in configured order, |
| `ps-service/tests/deploy_ps/test_rotate_key.py` | 4 | keep | `--rotate-key` mode for `scripts/deploy-ps-prod.sh`: active-slot detection + regenerate-the- |
| `ps-service/tests/domain_mapper/adapters/test_base.py` | 1 | keep | Tests for ps_service.domain_mapper.adapters.base. |
| `ps-service/tests/domain_mapper/adapters/test_cellar_eli.py` | 10 | rewrite | identical-body pair claims distinct requirements (#268) |
| `ps-service/tests/domain_mapper/test_ac008_out_of_scope.py` | 5 | keep | Proof that `ps_service.domain_mapper` never names a Policy/Standard/Control governance |
| `ps-service/tests/domain_mapper/test_baseline_graph_isolation.py` | 3 | keep | Proof of per-regulation baseline graph isolation. |
| `ps-service/tests/domain_mapper/test_derivation.py` | 47 | keep | Tests for ps_service.domain_mapper.derivation._derive_obligations. |
| `ps-service/tests/domain_mapper/test_derivation_gateway.py` | 9 | keep | Derivation writes its Obligation/Capability graph through the Graph Write Gateway. |
| `ps-service/tests/domain_mapper/test_edge_identity.py` | 4 | keep | Edge identity of the Domain Mapper's baseline writes. |
| `ps-service/tests/domain_mapper/test_errors.py` | 5 | keep | Tests for ps_service.domain_mapper.errors. |
| `ps-service/tests/domain_mapper/test_exclusion_audit_capture.py` | 4 | keep | Tests for `tools/domain-mapper/exclusion_audit.py`'s capture stage. |
| `ps-service/tests/domain_mapper/test_exclusion_audit_classification.py` | 5 | keep | Tests for `tools/domain-mapper/exclusion_audit.py`'s classification-proposal stage |
| `ps-service/tests/domain_mapper/test_exclusion_audit_cli.py` | 8 | keep | Tests for `tools/domain-mapper/exclusion_audit.py`'s CLI surface. |
| `ps-service/tests/domain_mapper/test_exclusion_audit_report.py` | 7 | keep | Tests for `tools/domain-mapper/exclusion_audit.py`'s row rendering and report assembly. |
| `ps-service/tests/domain_mapper/test_exclusion_audit_sampling.py` | 9 | keep | Tests for `tools/domain-mapper/exclusion_audit.py`'s sample-selection stage |
| `ps-service/tests/domain_mapper/test_extraction.py` | 59 | keep | Tests for ps_service.domain_mapper.extraction._extract_candidates_for_unit. |
| `ps-service/tests/domain_mapper/test_falkordb_client.py` | 8 | keep | Tests for `ps_service.domain_mapper.falkordb_client`. |
| `ps-service/tests/domain_mapper/test_falkordb_client_read_only.py` | 5 | keep | The Domain Mapper reads through `ReadOnlyGraph` and imports no driver. |
| `ps-service/tests/domain_mapper/test_graph_writer.py` | 27 | keep | Tests for `ps_service.domain_mapper.graph_writer`. |
| `ps-service/tests/domain_mapper/test_identity.py` | 24 | keep | Tests for ps_service.domain_mapper.identity. |
| `ps-service/tests/domain_mapper/test_live_capstone.py` | 4 | keep | The live 3-regulation end-to-end capstone. |
| `ps-service/tests/domain_mapper/test_live_exclusion_audit.py` | 4 | keep | Live wiring proof for `tools/domain-mapper/exclusion_audit.py`. |
| `ps-service/tests/domain_mapper/test_models.py` | 21 | keep | Tests for ps_service.domain_mapper.models. |
| `ps-service/tests/domain_mapper/test_no_graph_write_path.py` | 6 | keep | The Domain Mapper holds no write path to FalkorDB. |
| `ps-service/tests/domain_mapper/test_package_init.py` | 1 | keep | Tests for ps_service.domain_mapper's package front door (__init__.py). |
| `ps-service/tests/domain_mapper/test_persist_equivalence.py` | 6 | keep | The gateway path builds the same baseline graph the old direct-Cypher writer did. |
| `ps-service/tests/domain_mapper/test_persist_equivalence_live.py` | 2 | keep | `falkordb_live` + `postgres_live`: the Domain Mapper's gateway write path, real stores. |
| `ps-service/tests/domain_mapper/test_persist_failure_gateway.py` | 5 | keep | Persistence failures of the Domain Mapper stages through the gateway. |
| `ps-service/tests/domain_mapper/test_prompts.py` | 46 | keep | Tests for ps_service.domain_mapper.prompts. |
| `ps-service/tests/domain_mapper/test_prompts_live.py` | 1 | keep | Live test: the extraction prompt's multi-duty. |
| `ps-service/tests/domain_mapper/test_reuse_verification_live.py` | 2 | keep | Live-LLM evidence for the recalibrated capability-reuse verification prompt |
| `ps-service/tests/domain_mapper/test_schema_version.py` | 1 | keep | Tests for ps_service.domain_mapper's DOMAIN_SCHEMA_VERSION constant. |
| `ps-service/tests/domain_schema/test_cardinality.py` | 4 | keep | Cardinality / Multiplicity parsing, rendering and flipping. |
| `ps-service/tests/domain_schema/test_cli_measure.py` | 1 | keep | `python -m ps_service.domain_schema measure` reports the before/after size. |
| `ps-service/tests/domain_schema/test_cypher_vocabulary_scan.py` | 10 | keep | Every label and relationship type written in Cypher in |
| `ps-service/tests/domain_schema/test_definition_shape.py` | 4 | keep | Shape of `DOMAIN_SCHEMA` that no generated artifact can prove for itself. |
| `ps-service/tests/domain_schema/test_doc_generation.py` | 9 | keep | The committed domain-concepts document's generated regions equal the schema's rendering. |
| `ps-service/tests/domain_schema/test_documentation_consistency.py` | 6 | keep | Documentation does not say the `domain_concepts` |
| `ps-service/tests/domain_schema/test_freshness.py` | 13 | keep | `check` compares every generated artifact with the schema and reports drift. |
| `ps-service/tests/domain_schema/test_intake_generation.py` | 6 | keep | The generated intake schema equals the committed file byte for byte. |
| `ps-service/tests/domain_schema/test_intake_profile.py` | 6 | keep | The intake profile applied to the base schema equals the committed intake schema's defs. |
| `ps-service/tests/domain_schema/test_json_layout.py` | 16 | keep | `dumps_hand_layout` reproduces the committed intake schema's hand-edited layout. |
| `ps-service/tests/domain_schema/test_model_validation.py` | 17 | keep | Construction-time validation of the schema model. |
| `ps-service/tests/domain_schema/test_policy_status_matches_lifecycle.py` | 2 | keep | Policy `status` in the schema covers the statuses the lifecycle code uses. |
| `ps-service/tests/domain_schema/test_profile.py` | 22 | keep | Profiles derive a narrower schema from the base one (unit level). |
| `ps-service/tests/domain_schema/test_property_flags.py` | 4 | keep | Property flags: identity-bearing and lifecycle-managed markers on the real schema. |
| `ps-service/tests/domain_schema/test_render_doc_regions.py` | 6 | keep | Marker region engine: only the bytes inside generated regions are rewritten. |
| `ps-service/tests/domain_schema/test_render_doc_tables.py` | 4 | keep | Table renderers use the document's vocabulary for types, presence and cardinality. |
| `ps-service/tests/domain_schema/test_render_slim.py` | 3 | keep | Slim renderer behaviour on small, locally built schemas. |
| `ps-service/tests/domain_schema/test_slim_contract.py` | 7 | keep | The slim output contract on the real `DOMAIN_SCHEMA`. |
| `ps-service/tests/domain_schema/test_write_docs.py` | 4 | keep | `write-docs` regenerates the canonical document and its packaged copy. |
| `ps-service/tests/export/test_catalog_writer_catalog_json.py` | 4 | keep | Tests for `ps_service.export.catalog_writer.write_catalog_json` |
| `ps-service/tests/export/test_catalog_writer_manifest.py` | 5 | keep | Tests for `ps_service.export.catalog_writer.write_manifest`/`read_manifest` |
| `ps-service/tests/export/test_dump_restore_known_defect_live.py` | 2 | keep | Regression-documentation proof for a genuine FalkorDB server defect. |
| `ps-service/tests/export/test_embedding_equivalence.py` | 2 | keep | Export's artifacts through the gateway equal the legacy export's. |
| `ps-service/tests/export/test_embeddings.py` | 5 | keep | Tests for `ps_service.export.embeddings.backfill_capability_embeddings` |
| `ps-service/tests/export/test_embeddings_schema_pin.py` | 3 | keep | Embeddings backfill's label/property tables agree with the domain schema. |
| `ps-service/tests/export/test_engineering_practices_migration_live.py` | 2 | keep | Live proof: the engineering-practices `{short}_baseline`/ |
| `ps-service/tests/export/test_export_gateway.py` | 13 | keep | Export writes the baseline graph only through the Graph Write Gateway. |
| `ps-service/tests/export/test_export_instrument.py` | 6 | keep | Tests for `ps_service.export.export_instrument.export_instrument` |
| `ps-service/tests/export/test_export_instrument_cli.py` | 9 | keep | Real-wiring proof for `tools/curated-export/export_instrument.py`. |
| `ps-service/tests/export/test_export_instrument_cli_live.py` | 2 | keep | `falkordb_live` proof for `tools/curated-export/export_instrument.py`. |
| `ps-service/tests/export/test_falkordb_client_read_only.py` | 6 | keep | Export reads through the gateway's `ReadOnlyGraph` and imports no driver. |
| `ps-service/tests/export/test_falkordb_connection.py` | 7 | keep | Tests for Export's read handle and the gateway key port's handles. |
| `ps-service/tests/export/test_models.py` | 8 | keep | Tests for ps_service.export.models. |
| `ps-service/tests/export/test_no_graph_write_path.py` | 12 | keep | Export holds no write path to FalkorDB. |
| `ps-service/tests/export/test_serialize.py` | 8 | keep | Tests for `ps_service.export.serialize`. |
| `ps-service/tests/export/test_serialize_live.py` | 2 | keep | Live FalkorDB proof for `ps_service.export.serialize.serialize_graph`. |
| `ps-service/tests/export/test_serialize_roundtrip_live.py` | 1 | keep | Round-trip proof: `serialize_graph` -> `to_json_bytes` -> `parse_serialized_graph_json` |
| `ps-service/tests/graph_cleanup/test_audit_actions.py` | 23 | keep | `capability.merge` audit action registration. |
| `ps-service/tests/graph_cleanup/test_capability_merge_continuity.py` | 1 | keep | The capability-merge flow end to end: preview, passkey signature, executor, gateway group. |
| `ps-service/tests/graph_cleanup/test_capability_merge_gateway.py` | 21 | keep | `merge-capabilities` writes through the Graph Write Gateway. |
| `ps-service/tests/graph_cleanup/test_capability_merge_plan.py` | 11 | keep | Pure planner/validator for `merge-capabilities`, case 1. |
| `ps-service/tests/graph_cleanup/test_capability_merge_preview_and_approval.py` | 7 | keep | Preview and pair-bound approval for `merge-capabilities`. |
| `ps-service/tests/graph_cleanup/test_capability_unmerge_continuity.py` | 1 | keep | The capability-unmerge flow end to end: preview, signature, executor, gateway group. |
| `ps-service/tests/graph_cleanup/test_capability_unmerge_gateway.py` | 18 | keep | `unmerge` of a Capability writes through the Graph Write Gateway. |
| `ps-service/tests/graph_cleanup/test_case2_approval.py` | 6 | keep | Case-2 acknowledgment gate on the approval. |
| `ps-service/tests/graph_cleanup/test_case2_plan.py` | 15 | keep | Pure planner for merge case 2, exactly one capability governed. |
| `ps-service/tests/graph_cleanup/test_case3_approval.py` | 5 | keep | Case 3 on the preview/approval path. |
| `ps-service/tests/graph_cleanup/test_case3_plan.py` | 12 | keep | Pure planner for merge case 3, both capabilities governed. |
| `ps-service/tests/graph_cleanup/test_check_cleanup_approval.py` | 22 | keep | `check-cleanup-approval` and the lazy reconciler. |
| `ps-service/tests/graph_cleanup/test_cleanup_equivalence.py` | 22 | keep | The gateway-backed cleanup builds the graph the frozen legacy writer built. |
| `ps-service/tests/graph_cleanup/test_cleanup_read_graph_fidelity.py` | 18 | keep | `CleanupReadGraph` answers the reads exactly as the statements say. |
| `ps-service/tests/graph_cleanup/test_cleanup_requests_checkpoints.py` | 2 | keep | Every Graph Cleanup group requests a digest checkpoint. |
| `ps-service/tests/graph_cleanup/test_discovery_embeddings.py` | 10 | keep | Capability candidate discovery, embedding basis. |
| `ps-service/tests/graph_cleanup/test_discovery_governance.py` | 7 | keep | Capability candidate governance, obligation count and merge case. |
| `ps-service/tests/graph_cleanup/test_discovery_names.py` | 5 | keep | Capability merge-candidate discovery, name basis. |
| `ps-service/tests/graph_cleanup/test_discovery_obligations.py` | 10 | keep | Duplicate-obligation discovery within one Role. |
| `ps-service/tests/graph_cleanup/test_durability_e2e.py` | 6 | keep | A cleanup merge survives live ingest and offline restore. |
| `ps-service/tests/graph_cleanup/test_execute_capability_merge.py` | 14 | keep | Executor for `merge-capabilities`. |
| `ps-service/tests/graph_cleanup/test_execute_capability_unmerge.py` | 18 | keep | Executor for capability `unmerge`. |
| `ps-service/tests/graph_cleanup/test_execute_case2.py` | 10 | keep | Executor for merge case 2. |
| `ps-service/tests/graph_cleanup/test_execute_case3.py` | 7 | keep | Executor for merge case 3, same policy. |
| `ps-service/tests/graph_cleanup/test_execute_obligation_merge.py` | 14 | keep | Executor for `merge-obligations`. |
| `ps-service/tests/graph_cleanup/test_execute_obligation_unmerge.py` | 16 | keep | Executor for obligation `unmerge`. |
| `ps-service/tests/graph_cleanup/test_execute_release.py` | 15 | keep | Executor for `release-capability-governance`. |
| `ps-service/tests/graph_cleanup/test_frozen_reference.py` | 22 | keep | The frozen legacy reference is what the direct writer was. |
| `ps-service/tests/graph_cleanup/test_graph_reader_merge_state.py` | 8 | keep | Reader for `merge-capabilities` : pure reads. |
| `ps-service/tests/graph_cleanup/test_graph_reader_obligation_merge_state.py` | 5 | keep | Reader for `merge-obligations` : pure reads. |
| `ps-service/tests/graph_cleanup/test_graph_reader_obligation_unmerge_state.py` | 5 | keep | Reader for obligation `unmerge`: live state around a marker. |
| `ps-service/tests/graph_cleanup/test_graph_reader_release_state.py` | 5 | keep | Reader for `release-capability-governance` : pure reads. |
| `ps-service/tests/graph_cleanup/test_graph_reader_unmerge_state.py` | 7 | keep | Reader for capability `unmerge`: live state around a tombstone. |
| `ps-service/tests/graph_cleanup/test_graph_submission.py` | 16 | keep | The Graph Cleanup side of the gateway seam. |
| `ps-service/tests/graph_cleanup/test_graph_writer_capability_merge_live.py` | 11 | keep | Live FalkorDB proof of the FROZEN capability-merge statement. |
| `ps-service/tests/graph_cleanup/test_graph_writer_obligation_merge_live.py` | 4 | keep | Live FalkorDB proof of the FROZEN obligation-merge statement. |
| `ps-service/tests/graph_cleanup/test_graph_writer_obligation_unmerge_live.py` | 6 | keep | Live FalkorDB proof of the obligation-unmerge statement. |
| `ps-service/tests/graph_cleanup/test_graph_writer_unmerge_live.py` | 5 | keep | Live FalkorDB proof of the capability-unmerge statement. |
| `ps-service/tests/graph_cleanup/test_no_graph_write_path.py` | 15 | keep | Graph Cleanup holds no write path to FalkorDB. |
| `ps-service/tests/graph_cleanup/test_obligation_merge_continuity.py` | 1 | keep | The obligation-merge flow end to end: preview, passkey signature, executor, gateway group. |
| `ps-service/tests/graph_cleanup/test_obligation_merge_gateway.py` | 20 | keep | `merge-obligations` writes through the Graph Write Gateway. |
| `ps-service/tests/graph_cleanup/test_obligation_merge_plan.py` | 10 | keep | Pure planner/validator for `merge-obligations`. |
| `ps-service/tests/graph_cleanup/test_obligation_merge_preview_and_approval.py` | 9 | keep | Preview and pair-bound approval for `merge-obligations`, plus `check-cleanup-approval` |
| `ps-service/tests/graph_cleanup/test_obligation_unmerge_continuity.py` | 1 | keep | The obligation-unmerge flow end to end: preview, signature, executor, gateway group. |
| `ps-service/tests/graph_cleanup/test_obligation_unmerge_gateway.py` | 19 | keep | `unmerge` of an Obligation writes through the Graph Write Gateway. |
| `ps-service/tests/graph_cleanup/test_release_continuity.py` | 1 | keep | The release flow end to end: preview, passkey signature, executor, gateway group. |
| `ps-service/tests/graph_cleanup/test_release_gateway.py` | 15 | keep | `release-capability-governance` writes through the Graph Write Gateway. |
| `ps-service/tests/graph_cleanup/test_release_plan.py` | 10 | keep | Pure planner for `release-capability-governance`. |
| `ps-service/tests/graph_cleanup/test_release_preview_and_approval.py` | 7 | keep | Preview and approval for `release-capability-governance`, plus `check-cleanup-approval` |
| `ps-service/tests/graph_cleanup/test_unmerge_capability_plan.py` | 20 | keep | Pure planner for capability `unmerge`. |
| `ps-service/tests/graph_cleanup/test_unmerge_capability_preview_and_approval.py` | 8 | keep | Preview and approval for capability `unmerge`, plus `check-cleanup-approval`. |
| `ps-service/tests/graph_cleanup/test_unmerge_locator.py` | 10 | keep | Locating the merge an `unmerge` reverses, from the audit trail . |
| `ps-service/tests/graph_cleanup/test_unmerge_obligation_plan.py` | 15 | keep | Pure planner for obligation `unmerge`. |
| `ps-service/tests/graph_cleanup/test_unmerge_obligation_preview_and_approval.py` | 6 | keep | Preview and approval for obligation `unmerge`, plus `check-cleanup-approval`. |
| `ps-service/tests/graph_gateway/test_connection_factory.py` | 5 | keep | The gateway-owned FalkorDB connection factory and `/ready` probe. |
| `ps-service/tests/graph_gateway/test_default_gateway.py` | 5 | keep | The production composition of the Graph Write Gateway. |
| `ps-service/tests/graph_gateway/test_digest_checkpoint.py` | 8 | keep | A requested checkpoint records the canonical digest at the group's last position. |
| `ps-service/tests/graph_gateway/test_digest_checkpoint_cases.py` | 9 | keep | The checkpoint recorder's edge cases. |
| `ps-service/tests/graph_gateway/test_digest_edges.py` | 8 | keep | Edges, their direction and parallel edges are part of the digest. |
| `ps-service/tests/graph_gateway/test_digest_labels.py` | 5 | keep | A node's label set is part of the digest. |
| `ps-service/tests/graph_gateway/test_digest_live.py` | 13 | keep | `falkordb_live`: the digest scan against a real FalkorDB. |
| `ps-service/tests/graph_gateway/test_digest_order.py` | 6 | keep | The digest does not depend on insertion order or FalkorDB internal ids. |
| `ps-service/tests/graph_gateway/test_digest_scan_templates.py` | 1 | keep | The digest scans read properties only for the rows they return. |
| `ps-service/tests/graph_gateway/test_digest_values.py` | 13 | keep | Property and embedding values are compared exactly by the digest. |
| `ps-service/tests/graph_gateway/test_docs_content.py` | 116 | keep | Content lint: the operator and architecture docs describe the graph log provisioning. |
| `ps-service/tests/graph_gateway/test_drift_check.py` | 12 | keep | `check_drift`: compare a graph with its head checkpoint, alert, never repair. |
| `ps-service/tests/graph_gateway/test_drift_end_to_end.py` | 1 | keep | A real writer, a head checkpoint, an out-of-band write, then the alert. |
| `ps-service/tests/graph_gateway/test_drift_live.py` | 3 | keep | `falkordb_live`: the drift check against a real FalkorDB. |
| `ps-service/tests/graph_gateway/test_drift_monitor.py` | 5 | keep | The periodic drift monitor: one pass per interval over every logged graph. |
| `ps-service/tests/graph_gateway/test_edge_identity_backfill.py` | 8 | keep | `backfill_edge_identity` stamps the identity-less edges of a legacy graph. |
| `ps-service/tests/graph_gateway/test_equivalence_harness.py` | 7 | keep | The equivalence harness passes for equal write paths. |
| `ps-service/tests/graph_gateway/test_exact_floats.py` | 14 | keep | Exact 64-bit float reads from FalkorDB. |
| `ps-service/tests/graph_gateway/test_exact_floats_live.py` | 6 | keep | `falkordb_live`: exact 64-bit float reads against a real FalkorDB. |
| `ps-service/tests/graph_gateway/test_fake_property_columns.py` | 3 | keep | The in-memory FalkorDB fake's property columns are what its readable first version produced. |
| `ps-service/tests/graph_gateway/test_falkordb_key_port.py` | 9 | keep | `FalkorDBKeyPort` against a hand-written Redis/FalkorDB stand-in. |
| `ps-service/tests/graph_gateway/test_gateway_allow_list.py` | 8 | keep | Label allow-list of the Graph Write Gateway. |
| `ps-service/tests/graph_gateway/test_gateway_apply.py` | 7 | keep | Batched, idempotent apply through the public gateway. |
| `ps-service/tests/graph_gateway/test_gateway_catch_up.py` | 12 | keep | `catch_up`, `is_caught_up` and submit-time catch-up. |
| `ps-service/tests/graph_gateway/test_gateway_concurrency.py` | 4 | keep | Per-graph serialization of submitted groups. |
| `ps-service/tests/graph_gateway/test_gateway_edges.py` | 12 | keep | Edge primitives through the Graph Write Gateway. |
| `ps-service/tests/graph_gateway/test_gateway_falkordb_live.py` | 6 | keep | `falkordb_live` tests: the gateway's Cypher against a real FalkorDB. |
| `ps-service/tests/graph_gateway/test_gateway_in_transaction.py` | 10 | keep | Submitting a group on the caller's own transaction. |
| `ps-service/tests/graph_gateway/test_gateway_indexes.py` | 7 | keep | `id` index management through the public gateway. |
| `ps-service/tests/graph_gateway/test_gateway_live.py` | 4 | keep | `postgres_live` tests: the gateway over the real graph log store. |
| `ps-service/tests/graph_gateway/test_gateway_logging.py` | 5 | keep | The Graph Write Gateway's structured logging, across every path. |
| `ps-service/tests/graph_gateway/test_gateway_node_primitives.py` | 13 | keep | Property and delete-node primitives through the Graph Write Gateway. |
| `ps-service/tests/graph_gateway/test_gateway_noop.py` | 24 | keep | No-op suppression: a mutation that would not change the graph is never logged. |
| `ps-service/tests/graph_gateway/test_gateway_only_graph_access.py` | 10 | keep | Only the gateway package may build a write-capable FalkorDB client. |
| `ps-service/tests/graph_gateway/test_gateway_optional_audit_link.py` | 12 | keep | `MutationGroup.audit_event_id` is optional: a group with no audit event carries no link. |
| `ps-service/tests/graph_gateway/test_gateway_pending.py` | 11 | keep | Committed, apply pending and the permanent-error path. |
| `ps-service/tests/graph_gateway/test_gateway_preconditions.py` | 6 | keep | Caller preconditions on a group. |
| `ps-service/tests/graph_gateway/test_gateway_reconciler.py` | 8 | keep | The background reconciler applies committed-but-pending entries on recovery. |
| `ps-service/tests/graph_gateway/test_gateway_recovery.py` | 11 | keep | Startup recovery of the Graph Write Gateway . |
| `ps-service/tests/graph_gateway/test_gateway_retry.py` | 11 | keep | Pre-append retry, then fail closed. |
| `ps-service/tests/graph_gateway/test_gateway_stamp_edge_identity.py` | 9 | keep | `StampEdgeIdentity` through the Graph Write Gateway. |
| `ps-service/tests/graph_gateway/test_gateway_submit.py` | 9 | keep | Gateway: submit an upsert-node group through log, apply and outcome. |
| `ps-service/tests/graph_gateway/test_graph_reader_exact_floats.py` | 4 | keep | State reads give back stored 64-bit floats bit-identically. |
| `ps-service/tests/graph_gateway/test_label_allow_list.py` | 3 | keep | The replay sentinel label is never reachable by ordinary writers. |
| `ps-service/tests/graph_gateway/test_migrations.py` | 7 | keep | Fast checks of the `graph_gateway` migration files and startup wiring. |
| `ps-service/tests/graph_gateway/test_models.py` | 8 | keep | Fast tests for the graph mutation log's input models. |
| `ps-service/tests/graph_gateway/test_multi_graph_group.py` | 23 | keep | A group over several graphs commits in one transaction. |
| `ps-service/tests/graph_gateway/test_multi_graph_postgres_live.py` | 4 | keep | `postgres_live` twin of the multi-graph group. |
| `ps-service/tests/graph_gateway/test_package_exports.py` | 3 | keep | The package front door exposes the gateway's public surface. |
| `ps-service/tests/graph_gateway/test_payloads.py` | 17 | keep | Fast tests for graph log payload encoding. |
| `ps-service/tests/graph_gateway/test_persisted_elements_read.py` | 6 | keep | `GraphWriteGateway.read_persisted_elements` enumerates a graph's ids and edge identities. |
| `ps-service/tests/graph_gateway/test_provision.py` | 12 | keep | Fast tests for the `graph_gateway` provisioning CLI's environment handling. |
| `ps-service/tests/graph_gateway/test_provision_failure_live.py` | 3 | keep | `postgres_live` tests: how the provisioning CLI fails. |
| `ps-service/tests/graph_gateway/test_provision_live.py` | 24 | keep | `postgres_live` tests for the privileged provisioning path of the graph mutation log. |
| `ps-service/tests/graph_gateway/test_provision_ordinary_live.py` | 7 | keep | `postgres_live` tests: the provisioning CLI applies the ordinary ps_state migrations itself. |
| `ps-service/tests/graph_gateway/test_read_log_position.py` | 7 | keep | `GraphWriteGateway.read_log_position` reads a graph's last log position and nothing else. |
| `ps-service/tests/graph_gateway/test_read_only.py` | 7 | keep | `ReadOnlyGraph`: every read goes to the server-side read-only call. |
| `ps-service/tests/graph_gateway/test_replace_semantics.py` | 22 | keep | A replacing leg leaves its graph holding exactly the leg's content. |
| `ps-service/tests/graph_gateway/test_replace_semantics_live.py` | 1 | keep | `falkordb_live` twin of the replace semantics. |
| `ps-service/tests/graph_gateway/test_replay.py` | 12 | keep | Replay rebuilds a graph from the log and verifies it against a checkpoint. |
| `ps-service/tests/graph_gateway/test_replay_failed_persistence.py` | 4 | keep | A replay that failed leaves its graph gated across a restart. |
| `ps-service/tests/graph_gateway/test_replay_floats.py` | 2 | keep | 64-bit floats survive log, replay and graph bit for bit. |
| `ps-service/tests/graph_gateway/test_replay_gating.py` | 11 | keep | A graph that failed or is in a replay refuses writes; other graphs do not. |
| `ps-service/tests/graph_gateway/test_replay_indexes.py` | 4 | keep | Replay sets up each label's `id` index once and writes in batches. |
| `ps-service/tests/graph_gateway/test_replay_live.py` | 15 | keep | `falkordb_live` + `postgres_live`: replay from the real log into a real FalkorDB. |
| `ps-service/tests/graph_gateway/test_replay_paging.py` | 7 | keep | Replay reads the log in pages, in order, and refuses a log it cannot trust. |
| `ps-service/tests/graph_gateway/test_replay_perf_live.py` | 4 | keep | `falkordb_live` + `postgres_live`: replay of a CRA x10 log on the real stores. |
| `ps-service/tests/graph_gateway/test_replay_resume.py` | 12 | keep | A replay that is interrupted resumes from its progress record. |
| `ps-service/tests/graph_gateway/test_replay_stop.py` | 3 | keep | A replay can be asked to stop and stops between pages, resumable. |
| `ps-service/tests/graph_gateway/test_reply_parser.py` | 1 | keep | FalkorDB replies are parsed by hiredis's C parser. |
| `ps-service/tests/graph_gateway/test_startup_check_live.py` | 10 | keep | `postgres_live` tests for the fail-closed startup verifier. |
| `ps-service/tests/graph_gateway/test_startup_replay.py` | 15 | keep | Startup replay finds the graphs whose projection is gone or half built. |
| `ps-service/tests/graph_gateway/test_startup_replay_retry.py` | 6 | keep | `run_startup_replay` keeps the startup replay going through an infrastructure outage. |
| `ps-service/tests/graph_gateway/test_state_read_payload.py` | 6 | keep | The reads that precede a write do not drag node payloads over the wire. |
| `ps-service/tests/graph_gateway/test_store_append_live.py` | 14 | keep | `postgres_live` tests for appending and reading log groups. |
| `ps-service/tests/graph_gateway/test_store_audit_link_live.py` | 6 | keep | `postgres_live` tests for the same-transaction audit link of the graph log. |
| `ps-service/tests/graph_gateway/test_store_checkpoint_range_live.py` | 5 | keep | `postgres_live`: the checkpoint range read the replay uses. |
| `ps-service/tests/graph_gateway/test_store_marker_checkpoint_live.py` | 15 | keep | `postgres_live` tests for the applied marker and digest checkpoint. |
| `ps-service/tests/graph_gateway/test_store_no_mutation_methods.py` | 4 | keep | Structural proof that the graph log store exposes no rewrite method. |
| `ps-service/tests/graph_gateway/test_store_null_audit_link_live.py` | 2 | keep | `postgres_live` tests for a graph-log group with no audit link. |
| `ps-service/tests/graph_gateway/test_store_payloads_live.py` | 13 | keep | `postgres_live` tests for payloads of the graph log. |
| `ps-service/tests/graph_gateway/test_store_pending_graphs.py` | 5 | keep | `graphs_with_pending_entries` on the in-memory log store. |
| `ps-service/tests/graph_gateway/test_store_pending_graphs_live.py` | 1 | keep | `postgres_live` test for `graphs_with_pending_entries` . |
| `ps-service/tests/graph_gateway/test_store_unavailable.py` | 4 | keep | Tests for the graph log store when `ps_state` is unreachable or unconfigured. |
| `ps-service/tests/graph_gateway/test_swap_apply.py` | 18 | keep | A multi-graph group applied by swap. |
| `ps-service/tests/graph_gateway/test_swap_port_live.py` | 7 | keep | `falkordb_live` twin of the swap key port. |
| `ps-service/tests/graph_gateway/test_swap_recovery.py` | 17 | keep | An interrupted swap set is completed as one unit; readers see none or all. |
| `ps-service/tests/graph_gateway/test_swap_set_postgres_live.py` | 2 | keep | `postgres_live` twin of the swap-set link (migration `0002`). |
| `ps-service/tests/graph_gateway/test_system_audit.py` | 3 | keep | The system-audit helper gives ungated callers an audit event id to attach to a group. |
| `ps-service/tests/ingestion/adapters/cellar_eli/test_adapter.py` | 11 | keep | Tests for ps_service.ingestion.adapters.cellar_eli.adapter. |
| `ps-service/tests/ingestion/adapters/cellar_eli/test_fetch.py` | 17 | keep | Tests for ps_service.ingestion.adapters.cellar_eli.fetch. |
| `ps-service/tests/ingestion/adapters/cellar_eli/test_metadata.py` | 48 | rewrite | identical-body pair claims distinct requirements (#268) |
| `ps-service/tests/ingestion/adapters/cellar_eli/test_metadata_live.py` | 6 | keep | Live verification: `extract_metadata`'s RDF-based `effective_date` resolution |
| `ps-service/tests/ingestion/adapters/cellar_eli/test_structure.py` | 23 | keep | Tests for ps_service.ingestion.adapters.cellar_eli.structure. |
| `ps-service/tests/ingestion/adapters/cellar_eli/test_structure_live.py` | 4 | keep | Live parser-parity check: base-act XHTML vs its consolidated expression. |
| `ps-service/tests/ingestion/adapters/internal_seed/test_adapter.py` | 3 | keep | Tests for `ps_service.ingestion.adapters.internal_seed.adapter.InternalSeedIngestionAdapter`. |
| `ps-service/tests/ingestion/adapters/internal_seed/test_models_vocabulary_pin.py` | 9 | keep | `NodeLabel`/`EdgeType` are pinned to the code-defined domain schema. |
| `ps-service/tests/ingestion/adapters/internal_seed/test_persist.py` | 33 | keep | Tests for `ps_service.ingestion.adapters.internal_seed.persist`. |
| `ps-service/tests/ingestion/adapters/internal_seed/test_persist_baseline_gateway.py` | 10 | keep | The internal-seed baseline graph goes through the gateway. |
| `ps-service/tests/ingestion/adapters/internal_seed/test_persist_equivalence.py` | 4 | keep | The migrated internal-seed writer builds the same graphs the pre-migration writer did. |
| `ps-service/tests/ingestion/adapters/internal_seed/test_persist_gateway.py` | 7 | keep | The thin vertical path of the internal-seed ingestion through the gateway. |
| `ps-service/tests/ingestion/adapters/internal_seed/test_persist_native_gateway.py` | 12 | keep | The internal-seed native graph is written as one gateway group. |
| `ps-service/tests/ingestion/adapters/internal_seed/test_reconcile_cascade.py` | 7 | keep | Whole-document reconciliation across labels and rename cycles. |
| `ps-service/tests/ingestion/adapters/internal_seed/test_reconcile_isolation_and_audit.py` | 5 | keep | Reconciliation stays inside the target's graphs and takes nothing from the submission. |
| `ps-service/tests/ingestion/adapters/internal_seed/test_reconcile_retirement.py` | 14 | keep | A resubmitted internal-seed document retires what it no longer contains. |
| `ps-service/tests/ingestion/adapters/internal_seed/test_schema.py` | 17 | keep | Tests for ps_service.ingestion.adapters.internal_seed.schema. |
| `ps-service/tests/ingestion/adapters/test_base.py` | 1 | keep | Tests for ps_service.ingestion.adapters.base. |
| `ps-service/tests/ingestion/adapters/test_errors.py` | 3 | keep | Tests for ps_service.ingestion.adapters.errors. |
| `ps-service/tests/ingestion/test_dg008_docs_content.py` | 4 | keep | Content lint: `dataset-gates.md` documents the post-persist gate DG-008. |
| `ps-service/tests/ingestion/test_edge_identity_backfill_vertical.py` | 3 | keep | Re-ingesting onto a legacy-shaped graph adds no parallel edge: the submit path backfills first. |
| `ps-service/tests/ingestion/test_errors.py` | 2 | keep | Tests for ps_service.ingestion.errors. |
| `ps-service/tests/ingestion/test_falkordb_client.py` | 10 | keep | Tests for `ps_service.ingestion.falkordb_client`. |
| `ps-service/tests/ingestion/test_falkordb_client_delegates.py` | 2 | keep | `ps_service.ingestion.falkordb_client` delegates to the gateway factory. |
| `ps-service/tests/ingestion/test_falkordb_client_read_only.py` | 5 | keep | Ingestion reads through the gateway's `ReadOnlyGraph` and imports no driver. |
| `ps-service/tests/ingestion/test_gateway_write_path_live.py` | 9 | keep | `falkordb_live` + `postgres_live`: ingestion's gateway write path against real stores. |
| `ps-service/tests/ingestion/test_graph_submission.py` | 10 | keep | The ingestion-side seam to the Graph Write Gateway. |
| `ps-service/tests/ingestion/test_graph_writer.py` | 9 | keep | Tests for `ps_service.ingestion.graph_writer`. |
| `ps-service/tests/ingestion/test_graph_writer_builders.py` | 15 | keep | The pure builders of the external (Cellar/ELI) native write. |
| `ps-service/tests/ingestion/test_ingestion_rerun.py` | 6 | keep | A rerun after an interrupted ingestion equals an uninterrupted run. |
| `ps-service/tests/ingestion/test_models.py` | 17 | keep | Tests for ps_service.ingestion.models. |
| `ps-service/tests/ingestion/test_models_schema_pin.py` | 1 | keep | Ingestion's hand-copied enum Literals equal the domain schema. |
| `ps-service/tests/ingestion/test_no_graph_write_path.py` | 6 | keep | Ingestion holds no write path to FalkorDB . |
| `ps-service/tests/ingestion/test_pipeline.py` | 10 | keep | Tests for `ps_service.ingestion.pipeline`. |
| `ps-service/tests/ingestion/test_pipeline_gateway.py` | 13 | keep | The external ingestion pipeline writes its native graph through the gateway. |
| `ps-service/tests/ingestion/test_pipeline_live.py` | 7 | keep | Live end-to-end test for `ps_service.ingestion.pipeline.ingest_regulatory_instrument` |
| `ps-service/tests/ingestion_runs/test_audit_actions.py` | 15 | keep | Tests for the ingestion-run audit actions' registration, typed details and builders. |
| `ps-service/tests/ingestion_runs/test_component_independence.py` | 1 | keep | `ingestion_runs` writes audit rows only via `AuditStore`. |
| `ps-service/tests/ingestion_runs/test_dispatch.py` | 11 | keep | Tests for `ps_service.ingestion_runs.dispatch`. |
| `ps-service/tests/ingestion_runs/test_migrations.py` | 5 | keep | Tests for the `ingestion_runs` migration. |
| `ps-service/tests/ingestion_runs/test_store.py` | 20 | keep | Tests for the `IngestionRunStore` contract and `PsycopgIngestionRunStore`. |
| `ps-service/tests/invitations/test_audit_actions.py` | 5 | keep | `user.invite` typed audit details. |
| `ps-service/tests/invitations/test_client.py` | 8 | keep | Tests for `ps_service.invitations.client.create_invitation`. |
| `ps-service/tests/invitations/test_service.py` | 6 | keep | `invite_user_audited`: audit-first orchestration around the Authentik call. |
| `ps-service/tests/invitations/test_service_live.py` | 1 | keep | `user.invite` rows against a real Postgres. `postgres_live`-marked. |
| `ps-service/tests/invitations/test_startup.py` | 6 | keep | PS Service refuses to start when the Authentik service |
| `ps-service/tests/llm_interface/test_client_default_completion_caller.py` | 1 | keep | Tests for ps_service.llm_interface.client.default_completion_caller. |
| `ps-service/tests/llm_interface/test_client_default_embedding_caller.py` | 1 | keep | Tests for ps_service.llm_interface.client.default_embedding_caller. |
| `ps-service/tests/llm_interface/test_client_default_structured_completion_caller.py` | 1 | keep | Tests for ps_service.llm_interface.client.default_structured_completion_caller. |
| `ps-service/tests/llm_interface/test_connectivity.py` | 5 | keep | Tests for ps_service.llm_interface.connectivity.check_connectivity. |
| `ps-service/tests/llm_interface/test_errors.py` | 2 | keep | Tests for ps_service.llm_interface.errors. |
| `ps-service/tests/llm_interface/test_models.py` | 2 | keep | Tests for ps_service.llm_interface.models. |
| `ps-service/tests/llm_interface/test_route_completion_live_provider.py` | 1 | keep | Live test: RouteCompletion against the real, configured LLM Provider. |
| `ps-service/tests/llm_interface/test_route_completion_logs_run_id.py` | 1 | keep | A RouteCompletion call emits a structured log entry with the bound run_id. |
| `ps-service/tests/llm_interface/test_route_completion_mocked.py` | 7 | keep | Tests for ps_service.llm_interface.completion.route_completion (mocked provider calls). |
| `ps-service/tests/llm_interface/test_route_embedding_live_provider.py` | 2 | keep | Live tests: RouteEmbedding against the real, configured LLM Provider. |
| `ps-service/tests/llm_interface/test_route_embedding_logs_run_id.py` | 1 | keep | A RouteEmbedding call emits a structured log entry with the bound run_id. |
| `ps-service/tests/llm_interface/test_route_embedding_mocked.py` | 6 | keep | Tests for ps_service.llm_interface.embedding.route_embedding (mocked provider calls). |
| `ps-service/tests/llm_interface/test_route_structured_completion_live_provider.py` | 2 | keep | Live test: RouteStructuredCompletion against the real, configured LLM Provider. |
| `ps-service/tests/llm_interface/test_route_structured_completion_logs_run_id.py` | 1 | keep | A RouteStructuredCompletion call emits a log entry with the bound run_id. |
| `ps-service/tests/llm_interface/test_route_structured_completion_mocked.py` | 20 | keep | Tests for ps_service.llm_interface.structured_completion.route_structured_completion. |
| `ps-service/tests/logging/test_emitter_default_log_path.py` | 3 | keep | no module docstring |
| `ps-service/tests/logging/test_emitter_fallback_on_write_failure.py` | 4 | keep | Tests for the writer thread's failure-safe fallback path. |
| `ps-service/tests/logging/test_emitter_flush_barrier.py` | 4 | keep | Tests for LogEmitter.flush/stop barrier semantics. |
| `ps-service/tests/logging/test_emitter_mirror_failures.py` | 3 | keep | Tests for ``EmitterConfig.mirror_failures_to_stderr``. |
| `ps-service/tests/logging/test_emitter_non_blocking.py` | 1 | keep | emit() must never block on the sink, even while the sink is stalled. |
| `ps-service/tests/logging/test_errors.py` | 1 | keep | Tests for ps_service.logging.errors sanity. |
| `ps-service/tests/logging/test_facade_emit_log_entry.py` | 4 | keep | Tests for ps_service.logging.facade.emit_log_entry. |
| `ps-service/tests/logging/test_models_to_json_line.py` | 5 | keep | Tests for ps_service.logging.models.LogEntry.to_json_line (AC#3). |
| `ps-service/tests/logging/test_run_context_binding.py` | 4 | keep | Tests for ps_service.logging.run_context.bind_run_context/current_run_id (AC#1). |
| `ps-service/tests/logging/test_run_context_thread_isolation.py` | 3 | keep | Tests for run_id propagation across real threads. |
| `ps-service/tests/mcp_interface/test_access_role_tools.py` | 27 | keep | Tests for the registered access-role MCP tools. |
| `ps-service/tests/mcp_interface/test_audit_context_helper.py` | 5 | keep | The one MCP audit-context resolution shared by every audited tool. |
| `ps-service/tests/mcp_interface/test_audit_events_details_filter.py` | 5 | keep | `list-audit-events` allow-listed `details` filter via the MCP tool. |
| `ps-service/tests/mcp_interface/test_audit_new_actions_end_to_end_postgres_live.py` | 4 | keep | Every new audit action round-trips through real Postgres and `list_audit_events` |
| `ps-service/tests/mcp_interface/test_audit_parity_matrix.py` | 4 | keep | Cross-transport parity of the new audit rows. |
| `ps-service/tests/mcp_interface/test_audit_secret_sweep.py` | 6 | keep | No new audit row or log line carries a secret, free-text error, trace or path. |
| `ps-service/tests/mcp_interface/test_backfill_gateway_tools.py` | 6 | keep | Backfills write through the Graph Write Gateway, only when there is something to fill. |
| `ps-service/tests/mcp_interface/test_catalog_source_authz_gate.py` | 4 | keep | Tests for the `SystemAdmin`-or-above authz gate on the catalog-source MCP |
| `ps-service/tests/mcp_interface/test_catalog_source_live.py` | 2 | keep | `postgres_live` tests for the catalog-source MCP tools against a real Postgres. |
| `ps-service/tests/mcp_interface/test_catalog_source_skills.py` | 15 | keep | Tests for the `set-catalog-source`/`reset-catalog-source`/`get-catalog-source` |
| `ps-service/tests/mcp_interface/test_check_instrument_ingestion_status_tool.py` | 10 | keep | Tests for the registered `check_instrument_ingestion_status` MCP tool. |
| `ps-service/tests/mcp_interface/test_check_regulations_audit.py` | 3 | keep | `check_regulations` writes `ingestion_run.*` audit rows per re-ingest. |
| `ps-service/tests/mcp_interface/test_check_regulations_authz_gate.py` | 5 | keep | Tests for the `ComplianceOfficer` authz gate on the `check_regulations` MCP |
| `ps-service/tests/mcp_interface/test_check_regulations_tool.py` | 5 | keep | Tests for the registered `check_regulations` MCP tool. |
| `ps-service/tests/mcp_interface/test_create_policy_draft_gateway.py` | 4 | keep | The `create-policy-draft` MCP tool writes through the Graph Write Gateway. |
| `ps-service/tests/mcp_interface/test_cypher_tool.py` | 12 | keep | Tests for the registered `cypher` MCP tool driven through |
| `ps-service/tests/mcp_interface/test_dev_cli_retired.py` | 3 | keep | Tests that the retired Query Engine dev CLI module is fully gone. |
| `ps-service/tests/mcp_interface/test_domain_concepts_merged_tombstone.py` | 8 | keep | The domain-concepts document describes the Capability `merged` tombstone. |
| `ps-service/tests/mcp_interface/test_domain_concepts_resource.py` | 13 | keep | Tests for the `GetDomainConcepts` MCP resource. |
| `ps-service/tests/mcp_interface/test_domain_concepts_tool.py` | 10 | keep | Tests for the `domain_concepts` MCP tool. |
| `ps-service/tests/mcp_interface/test_errors.py` | 4 | keep | Tests for `ps_service.mcp_interface.errors`. |
| `ps-service/tests/mcp_interface/test_failure_detail_scrubbing.py` | 4 | keep | MCP-action and background-ingestion failure logs are scrubbed. |
| `ps-service/tests/mcp_interface/test_get_catalog_listing_tool.py` | 7 | keep | Tests for the registered `get-catalog-listing` MCP tool. |
| `ps-service/tests/mcp_interface/test_get_ingestion_status_tool.py` | 14 | keep | Tests for the `get_ingestion_status` MCP tool. |
| `ps-service/tests/mcp_interface/test_graph_cleanup_authz_gate.py` | 3 | keep | Authentication + `ComplianceOfficer` gate on every graph-cleanup MCP tool. |
| `ps-service/tests/mcp_interface/test_graph_cleanup_find_capabilities_tool.py` | 4 | keep | `find-capability-merge-candidates` for a granted `ComplianceOfficer`. |
| `ps-service/tests/mcp_interface/test_graph_cleanup_find_obligations_tool.py` | 2 | keep | `find-duplicate-obligations` for a granted `ComplianceOfficer`. |
| `ps-service/tests/mcp_interface/test_graph_cleanup_merge_capabilities_tool.py` | 11 | keep | `merge-capabilities` (preview + approval) and `check-cleanup-approval`. |
| `ps-service/tests/mcp_interface/test_graph_cleanup_merge_obligations_tool.py` | 5 | keep | `merge-obligations` (preview + approval) through the MCP tool. |
| `ps-service/tests/mcp_interface/test_graph_cleanup_release_governance_tool.py` | 8 | keep | `release-capability-governance` (preview + approval) through the MCP tool. |
| `ps-service/tests/mcp_interface/test_graph_cleanup_tool_sweep.py` | 6 | keep | Cross-cutting sweeps over every graph-cleanup MCP tool. |
| `ps-service/tests/mcp_interface/test_graph_cleanup_unmerge_obligation_tool.py` | 7 | keep | `unmerge` (preview + approval) through the MCP tool, obligation merges. |
| `ps-service/tests/mcp_interface/test_graph_cleanup_unmerge_tool.py` | 6 | keep | `unmerge` (preview + approval) through the MCP tool, capability merges. |
| `ps-service/tests/mcp_interface/test_handle_mcp_tool_call.py` | 10 | keep | Tests for `mcp_server.handle_mcp_tool_call` -- the injectable |
| `ps-service/tests/mcp_interface/test_handle_mcp_tool_call_run_context.py` | 4 | keep | Tests for run-correlation in `mcp_server.handle_mcp_tool_call`. |
| `ps-service/tests/mcp_interface/test_http_transport.py` | 5 | keep | Tests for `ps_service.mcp_interface.http_transport`. |
| `ps-service/tests/mcp_interface/test_ingest_regulation_audit.py` | 6 | keep | `ingest_regulation` writes `ingestion_run.*` audit rows. |
| `ps-service/tests/mcp_interface/test_ingest_regulation_authz_gate.py` | 5 | keep | Tests for the `ComplianceOfficer` authz gate on the `ingest_regulation` MCP |
| `ps-service/tests/mcp_interface/test_ingest_regulation_tool.py` | 25 | keep | Tests for the registered `ingest_regulation` MCP tool . |
| `ps-service/tests/mcp_interface/test_ingestion_run_tools_authz_gate.py` | 5 | keep | ComplianceOfficer gate on `start_ingestion` and `get_ingestion_status` . |
| `ps-service/tests/mcp_interface/test_ingestion_tool_preflight_parity.py` | 9 | keep | Pre-flight parity characterization for the catalog-ingestion MCP tools. |
| `ps-service/tests/mcp_interface/test_invite_user_authz_gate.py` | 3 | keep | Tests for the `SystemAdmin`-or-above authz gate on the `invite-user` MCP |
| `ps-service/tests/mcp_interface/test_invite_user_tool.py` | 11 | keep | Tests for the registered `invite-user` MCP tool. |
| `ps-service/tests/mcp_interface/test_live_capstone.py` | 3 | keep | The `falkordb_live` end-to-end capstone for the MCP Interface |
| `ps-service/tests/mcp_interface/test_mcp_auth.py` | 3 | keep | End-to-end MCP auth tests . |
| `ps-service/tests/mcp_interface/test_near_miss_tools.py` | 23 | keep | Tests for the registered `near_misses_list`/`near_misses_resolve` MCP tools. |
| `ps-service/tests/mcp_interface/test_patch_fields_schema_alignment.py` | 2 | keep | The MCP patch-field validation follows the schema-derived sets and enum order. |
| `ps-service/tests/mcp_interface/test_policy_lifecycle_tools.py` | 129 | keep | Tests for the registered `create-policy-draft` MCP tool. |
| `ps-service/tests/mcp_interface/test_ps_assess_instrument_applicability_skill_content.py` | 4 | keep | Content lint for `ps-assess-instrument-applicability/SKILL.md`. |
| `ps-service/tests/mcp_interface/test_ps_author_policy_branch_detection.py` | 13 | keep | Tests for `ps-author-policy`'s branch-detection sequence. |
| `ps-service/tests/mcp_interface/test_ps_author_policy_citations.py` | 3 | keep | Tests for `ps-author-policy`'s citation-persistence contract. |
| `ps-service/tests/mcp_interface/test_ps_author_policy_control_loop.py` | 5 | keep | Tests for `ps-author-policy`'s Control field-by-field authoring loop. |
| `ps-service/tests/mcp_interface/test_ps_author_policy_governed_chain.py` | 2 | keep | Stateful create -> propose -> approve -> detect chain for `ps-author-policy`. |
| `ps-service/tests/mcp_interface/test_ps_author_policy_policy_loop.py` | 2 | keep | Tests for `ps-author-policy`'s Policy field-by-field authoring loop. |
| `ps-service/tests/mcp_interface/test_ps_author_policy_session_summary.py` | 1 | keep | Tests for `ps-author-policy`'s session-end summary. |
| `ps-service/tests/mcp_interface/test_ps_author_policy_skill_content.py` | 12 | keep | Structural/content lint for `ps-author-policy/SKILL.md` . |
| `ps-service/tests/mcp_interface/test_ps_author_policy_standard_loop.py` | 4 | keep | Tests for `ps-author-policy`'s Standard field-by-field authoring loop. |
| `ps-service/tests/mcp_interface/test_ps_graph_cleanup_skill_content.py` | 34 | keep | Content lint for `ps-graph-cleanup/SKILL.md` and its registrations. |
| `ps-service/tests/mcp_interface/test_ps_ingest_regulation_skill_content.py` | 8 | keep | Content lint for `ps-ingest-regulation/SKILL.md` and its docs. |
| `ps-service/tests/mcp_interface/test_ps_list_audit_events_skill_content.py` | 4 | keep | Content lint for `ps-list-audit-events/SKILL.md`. |
| `ps-service/tests/mcp_interface/test_ps_list_ingested_live.py` | 5 | keep | Live FalkorDB proof of the `ps-list-ingested` fixed query. |
| `ps-service/tests/mcp_interface/test_ps_list_ingested_skill_content.py` | 22 | keep | Content lint for `ps-list-ingested/SKILL.md`. |
| `ps-service/tests/mcp_interface/test_ps_policy_lifecycle_skill_content.py` | 4 | keep | Content lint for `ps-policy-lifecycle/SKILL.md`. |
| `ps-service/tests/mcp_interface/test_ps_qna_skill_content.py` | 7 | keep | Content lint for `ps-qna/SKILL.md`. |
| `ps-service/tests/mcp_interface/test_ps_skills_connector_names.py` | 4 | keep | Every shipped skill names the single `ps-mcp` connector and none of the old ones. |
| `ps-service/tests/mcp_interface/test_restore_instrument_authz_gate.py` | 5 | keep | Tests for the `ComplianceOfficer` authz gate on the `restore_instrument` MCP |
| `ps-service/tests/mcp_interface/test_restore_instrument_tool.py` | 30 | keep | Tests for the registered `restore_instrument` MCP tool. |
| `ps-service/tests/mcp_interface/test_scope_guard.py` | 5 | keep | Scope guards for `mcp_server.py`. |
| `ps-service/tests/mcp_interface/test_standard_control_draft_gateway.py` | 8 | keep | The Standard and Control draft tools write through the Graph Write Gateway. |
| `ps-service/tests/mcp_interface/test_start_ingestion_tool.py` | 31 | keep | Tests for the `start_ingestion` MCP tool. |
| `ps-service/tests/mcp_interface/test_tools_list_provenance_free.py` | 6 | keep | The served MCP `tools/list` payload, server instructions and resource descriptions carry no |
| `ps-service/tests/mcp_interface/test_unseeded_error.py` | 2 | keep | Tests for `handle_mcp_tool_call`'s `GraphUnseededError` delegation. |
| `ps-service/tests/mcp_interface/test_update_policy_draft_gateway.py` | 7 | keep | The `update-policy-draft` MCP tool writes through the Graph Write Gateway. |
| `ps-service/tests/passkey_signing/test_credential_rp_scoping.py` | 4 | keep | A signing credential is only usable on the `rp_id` it was enrolled under. |
| `ps-service/tests/passkey_signing/test_enrollment_ceremony.py` | 5 | keep | HTTP tests for the companion-browser enrollment ceremony . |
| `ps-service/tests/passkey_signing/test_executor_dispatch.py` | 5 | keep | Approval-executor registry and `sign/verify` dispatch . |
| `ps-service/tests/passkey_signing/test_graph_cleanup_obligation_signing_to_merge.py` | 5 | keep | Signing ceremony to obligation-merge execution over HTTP. |
| `ps-service/tests/passkey_signing/test_graph_cleanup_release_signing.py` | 5 | keep | Signing ceremony to governance-release execution over HTTP. |
| `ps-service/tests/passkey_signing/test_graph_cleanup_signed_failure_sweep.py` | 2 | keep | Fail-closed and sanitised failure over HTTP for EVERY cleanup executor. |
| `ps-service/tests/passkey_signing/test_graph_cleanup_signing_to_merge.py` | 7 | keep | Signing ceremony to merge execution over HTTP. |
| `ps-service/tests/passkey_signing/test_graph_cleanup_unmerge_obligation_signing.py` | 7 | keep | Signing ceremony to obligation-unmerge execution over HTTP. |
| `ps-service/tests/passkey_signing/test_graph_cleanup_unmerge_signing.py` | 6 | keep | Signing ceremony to capability-unmerge execution over HTTP. |
| `ps-service/tests/passkey_signing/test_merge_audit.py` | 7 | keep | The passkey-approved near-miss merge is audited as `near_miss.resolve`. |
| `ps-service/tests/passkey_signing/test_migration_runner.py` | 2 | keep | Tests for `ps_service.passkey_signing.migration_runner`. |
| `ps-service/tests/passkey_signing/test_signing_ceremony.py` | 5 | keep | HTTP tests for the companion-browser signing ceremony. |
| `ps-service/tests/passkey_signing/test_signing_failure_visibility.py` | 4 | keep | Regression tests for two silent failure modes in the signing ceremony. |
| `ps-service/tests/passkey_signing/test_signing_hardening.py` | 8 | keep | Adversarial hardening tests for the companion-browser signing ceremony. |
| `ps-service/tests/passkey_signing/test_store.py` | 8 | keep | Tests for `ps_service.passkey_signing.store` . |
| `ps-service/tests/persistence/test_connection.py` | 3 | keep | Tests for `ps_service.persistence.connection`. |
| `ps-service/tests/persistence/test_database_isolation_live.py` | 12 | keep | `postgres_live` tests for the PS Postgres init script's database isolation. |
| `ps-service/tests/persistence/test_migration_lock_live.py` | 6 | keep | `postgres_live` tests for the migration advisory lock shared by both runners. |
| `ps-service/tests/persistence/test_migration_runner.py` | 12 | keep | Tests for `ps_service.persistence.migration_runner`. |
| `ps-service/tests/persistence/test_privileged_migration_runner.py` | 6 | keep | Fast tests for `ps_service.persistence.privileged_migration_runner`. |
| `ps-service/tests/persistence/test_run_as_role_live.py` | 4 | keep | `postgres_live` tests for `apply_pending_migrations(run_as_role=...)`. |
| `ps-service/tests/persistence/test_verify_privileged_migrations.py` | 6 | keep | Fast tests for the fail-closed startup verifier's error and log shape. |
| `ps-service/tests/policy_lifecycle/test_audit_actions.py` | 14 | keep | Tests for `ps_service.policy_lifecycle.audit_actions`. |
| `ps-service/tests/policy_lifecycle/test_audit_consistency.py` | 5 | keep | Cross-action audit-vocabulary consistency check. |
| `ps-service/tests/policy_lifecycle/test_backfill_gateway.py` | 13 | keep | The governance backfill is a read-resolve-write through the gateway. |
| `ps-service/tests/policy_lifecycle/test_capability_tombstone.py` | 1 | keep | Policy Lifecycle ignores `merged` Capability tombstones. |
| `ps-service/tests/policy_lifecycle/test_cascade_gateway.py` | 9 | keep | Propose, reject and revert cascade a Policy's tree as ONE gateway group. |
| `ps-service/tests/policy_lifecycle/test_child_identity.py` | 6 | keep | Child ids derive from the parent and a re-add is idempotent. |
| `ps-service/tests/policy_lifecycle/test_claim_create_gateway.py` | 12 | keep | Creating a Policy that claims Capabilities is a guarded gateway group. |
| `ps-service/tests/policy_lifecycle/test_create_gateway.py` | 11 | keep | Creating a Policy is ONE gateway group linked to its `applied` audit row. |
| `ps-service/tests/policy_lifecycle/test_draft_edit_gateway.py` | 21 | keep | Draft Policy edits are one gateway group, with no audit link and no precondition. |
| `ps-service/tests/policy_lifecycle/test_errors.py` | 11 | keep | Tests for `ps_service.policy_lifecycle.errors`. |
| `ps-service/tests/policy_lifecycle/test_fork_repoint_gateway.py` | 10 | keep | Approving a fork moves its prior's governed Capabilities as one guarded group. |
| `ps-service/tests/policy_lifecycle/test_governed_by_live.py` | 6 | keep | Live FalkorDB proof of the `GOVERNED_BY` write paths. |
| `ps-service/tests/policy_lifecycle/test_graph_submission.py` | 15 | keep | The Policy Lifecycle side of the Graph Write Gateway seam. |
| `ps-service/tests/policy_lifecycle/test_graph_writer.py` | 17 | keep | Tests for `ps_service.policy_lifecycle.graph_writer`'s read functions (`find_*`, `read_*`). |
| `ps-service/tests/policy_lifecycle/test_immutability_matrix.py` | 1 | keep | Immutability matrix test. |
| `ps-service/tests/policy_lifecycle/test_legacy_reference_characterisation.py` | 25 | keep | The frozen legacy writer is the original one, and its interpreter applies Cypher like FalkorDB. |
| `ps-service/tests/policy_lifecycle/test_lifecycle_failure_mapping.py` | 9 | keep | Gateway failures reach Policy Lifecycle callers as its persistence error. |
| `ps-service/tests/policy_lifecycle/test_lifecycle_test_doubles_read_only.py` | 4 | keep | the lifecycle test doubles do not interpret graph writes. |
| `ps-service/tests/policy_lifecycle/test_no_graph_write_path.py` | 10 | keep | Policy Lifecycle holds no write path to FalkorDB. |
| `ps-service/tests/policy_lifecycle/test_patchable_fields_golden.py` | 4 | keep | Golden: schema-derived patchable sets equal fixed expected-value lists. |
| `ps-service/tests/policy_lifecycle/test_policy_lifecycle_equivalence.py` | 17 | keep | The gateway-backed lifecycle writes build the graph the frozen legacy writer built. |
| `ps-service/tests/policy_lifecycle/test_policy_lifecycle_equivalence_live.py` | 3 | keep | `falkordb_live`: the gateway-backed lifecycle writers against the frozen legacy Cypher. |
| `ps-service/tests/policy_lifecycle/test_policy_read_graph_fidelity.py` | 11 | keep | the real `find_*`/`read_*` functions behave over `PolicyReadGraph` as expected. |
| `ps-service/tests/policy_lifecycle/test_rules.py` | 10 | keep | Tests for `ps_service.policy_lifecycle.rules`. |
| `ps-service/tests/policy_lifecycle/test_rules_status_pin.py` | 4 | keep | Hand-copied status/action Literals equal the domain schema. |
| `ps-service/tests/policy_lifecycle/test_service.py` | 108 | keep | Tests for the `ps_service.policy_lifecycle.service` entry points. |
| `ps-service/tests/query_engine/test_errors.py` | 7 | keep | Tests for ps_service.query_engine.errors. |
| `ps-service/tests/query_engine/test_execute_cypher_query_error_wrapping.py` | 2 | keep | Tests for `execute_cypher_query`'s FalkorDB-failure wrapping. |
| `ps-service/tests/query_engine/test_execute_cypher_query_logging.py` | 8 | keep | Tests for `execute_cypher_query`'s structured logging. |
| `ps-service/tests/query_engine/test_execute_cypher_query_row_cap.py` | 4 | keep | Tests for `execute_cypher_query`'s row-count cap enforcement. |
| `ps-service/tests/query_engine/test_execute_cypher_query_success.py` | 4 | keep | Tests for `execute_cypher_query`'s success-path shape mapping. |
| `ps-service/tests/query_engine/test_execute_cypher_query_timeout.py` | 5 | keep | Tests for `execute_cypher_query`'s timeout enforcement. |
| `ps-service/tests/query_engine/test_execute_cypher_query_unseeded.py` | 4 | keep | Tests for `execute_cypher_query`'s unseeded-graph guard. |
| `ps-service/tests/query_engine/test_execute_cypher_query_write_guard.py` | 5 | keep | Tests for `execute_cypher_query`'s write-clause guard. |
| `ps-service/tests/query_engine/test_falkordb_client.py` | 2 | keep | Tests for `ps_service.query_engine.falkordb_client`. |
| `ps-service/tests/query_engine/test_falkordb_client_read_only.py` | 3 | keep | The Query Engine reads through the gateway's `ReadOnlyGraph` and imports no driver. |
| `ps-service/tests/query_engine/test_live_capstone.py` | 2 | keep | The live capstone proving the read path against real |
| `ps-service/tests/query_engine/test_models.py` | 3 | keep | Tests for ps_service.query_engine.models. |
| `ps-service/tests/query_engine/test_ro_query_rejects_write_live.py` | 4 | keep | Live: FalkorDB itself rejects a write that evades the textual guard. |
| `ps-service/tests/release/test_bump_from_commits.py` | 14 | keep | `scripts/release/bump-from-commits.sh` -- the conventional-commit classifier. |
| `ps-service/tests/release/test_create_github_release.py` | 5 | keep | `previous-release-tag.sh` and `create-github-release.sh` . |
| `ps-service/tests/release/test_docs_installation_hostname.py` | 8 | keep | The evaluator hostname documentation (`authentik.test`, `.local` rejected). |
| `ps-service/tests/release/test_docs_mcp_connector_rule.py` | 17 | keep | The docs state the direct-connector rule consistently. |
| `ps-service/tests/release/test_docs_plugin_names.py` | 3 | keep | The guides carry no stale `ps-*` plugin/connector names and their links resolve. |
| `ps-service/tests/release/test_lint_commit_header.py` | 16 | keep | `scripts/release/lint-commit-header.sh` -- the conventional-header guard rail. |
| `ps-service/tests/release/test_next_version.py` | 2 | keep | Arithmetic and boundary validation for `next-version.sh`. |
| `ps-service/tests/release/test_on_main_workflow.py` | 15 | keep | Static and executed assertions on `.github/workflows/on_main.yml`'s header lint. |
| `ps-service/tests/release/test_on_ready_workflow.py` | 5 | keep | Static and executed assertions on `.github/workflows/on_ready.yml`'s header lint. |
| `ps-service/tests/release/test_plugin_manifests.py` | 12 | keep | Plugin and marketplace manifests carry the `ps-*` ids. |
| `ps-service/tests/release/test_pr_to_ready_workflow.py` | 4 | keep | Static assertions on `.github/workflows/pr-to-ready.yml`'s PR-title lint. |
| `ps-service/tests/release/test_pre_push_hook.py` | 10 | keep | `.githooks/pre-push` -- the local twin of two CI gates. |
| `ps-service/tests/release/test_publish_release.py` | 7 | keep | Commit + tag + atomic push, and the push-race contract. |
| `ps-service/tests/release/test_release_ci.py` | 4 | keep | `.insitu.yml` runs the default pytest suite in the `trunk-worthy` wave. |
| `ps-service/tests/release/test_release_driver.py` | 5 | keep | End-to-end proof of `release.sh`'s no-release path and its fail-fast boundary . |
| `ps-service/tests/release/test_release_rehearsal.py` | 1 | keep | End-to-end rehearsal of the release pipeline against a real clone of this repo. |
| `ps-service/tests/release/test_release_scripts.py` | 2 | keep | File-mode invariants over `scripts/release/`. |
| `ps-service/tests/release/test_version_files.py` | 6 | keep | Version-file sync + verify. |
| `ps-service/tests/restore/test_audit_actions.py` | 9 | keep | `instrument.restore` typed audit details and failure classification. |
| `ps-service/tests/restore/test_baseline_reader.py` | 10 | keep | `restore.baseline_reader`: the baseline reader's fixed queries answered from a parsed artifact. |
| `ps-service/tests/restore/test_export_restore_roundtrip_live.py` | 1 | keep | Live FalkorDB proof of the real export -> artifact -> restore -> equality |
| `ps-service/tests/restore/test_leg_primitives.py` | 9 | keep | `restore.leg_primitives`: an artifact's native or baseline leg as gateway primitives. |
| `ps-service/tests/restore/test_merge_primitives.py` | 19 | keep | `restore.merge_primitives`: the `policy_system` leg of a restore as primitives. |
| `ps-service/tests/restore/test_models.py` | 4 | keep | Tests for ps_service.restore.models. |
| `ps-service/tests/restore/test_no_graph_write_path.py` | 9 | keep | Restore holds no write path to FalkorDB. |
| `ps-service/tests/restore/test_restore_concurrency.py` | 4 | keep | A restore guards `policy_system` with its log position, retrying up to three times. |
| `ps-service/tests/restore/test_restore_gateway.py` | 18 | keep | A restore is ONE multi-graph group through the Graph Write Gateway. |
| `ps-service/tests/restore/test_restore_instrument_all_or_nothing_live.py` | 1 | keep | Live proof that a failure while building the restore leaves everything untouched. |
| `ps-service/tests/restore/test_restore_instrument_audit_log.py` | 5 | keep | Tests for `restore_instrument`'s audit log entries. |
| `ps-service/tests/restore/test_restore_instrument_classification_passthrough.py` | 6 | keep | Offline restore-path parity for the PracticeArea/RiskPath/classification passthrough. |
| `ps-service/tests/restore/test_restore_instrument_concurrency_live.py` | 2 | keep | Live proof of the restore's optimistic concurrency on `policy_system`. |
| `ps-service/tests/restore/test_restore_instrument_content_validation.py` | 3 | keep | Tests for `restore_instrument`'s content-validation ordering. |
| `ps-service/tests/restore/test_restore_instrument_draft_governance.py` | 6 | keep | Tests for the restore's draft-governance import. |
| `ps-service/tests/restore/test_restore_instrument_draft_governance_live.py` | 2 | keep | Live FalkorDB proof that a restored internal tree is a normal draft. |
| `ps-service/tests/restore/test_restore_instrument_integrity.py` | 5 | keep | Tests for `ps_service.restore.restore_instrument.restore_instrument`'s |
| `ps-service/tests/restore/test_restore_instrument_interruption_live.py` | 1 | keep | Live proof that an interruption before the group is submitted leaves nothing behind. |
| `ps-service/tests/restore/test_restore_instrument_merged_redirect.py` | 2 | keep | Offline restore follows `MERGED_INTO` tombstone redirects. |
| `ps-service/tests/restore/test_restore_instrument_native_live.py` | 1 | keep | Live FalkorDB proof for `restore_instrument`'s native leg. |
| `ps-service/tests/restore/test_restore_instrument_obligation_redirect.py` | 4 | keep | Offline restore follows `MergedObligation` markers. |
| `ps-service/tests/restore/test_restore_instrument_schema_mismatch.py` | 3 | keep | Tests for `ps_service.restore.restore_instrument.restore_instrument`'s |
| `ps-service/tests/restore/test_restore_instrument_uses_the_gateway.py` | 4 | keep | `restore_instrument` writes only through the Graph Write Gateway. |
| `ps-service/tests/restore/test_restore_merge_governance_parity.py` | 1 | keep | Restore and live merge import a policy tree through one function. |
| `ps-service/tests/restore/test_restore_requests_checkpoints.py` | 3 | keep | A restore requests a digest checkpoint on every leg of its one group. |
| `ps-service/tests/restore/test_restore_swap.py` | 8 | keep | A restore is applied by swap: readers see none or all of it. |
| `ps-service/tests/restore/test_schema_allowlist.py` | 22 | keep | Tests for `ps_service.restore.schema_allowlist`. |
| `ps-service/tests/restore/test_schema_allowlist_curated_content.py` | 3 | keep | DB-free proof that every shipped ``curated-content/*/{baseline,native}.json`` passes |
| `ps-service/tests/runtime_config/test_audit_actions.py` | 3 | keep | Tests for the runtime-config audit actions' registration and typed details. |
| `ps-service/tests/runtime_config/test_component_independence.py` | 3 | keep | `runtime_config` and `authz` write audit rows only via `AuditStore`. |
| `ps-service/tests/runtime_config/test_registry.py` | 3 | keep | Tests for the runtime-config key registry. |
| `ps-service/tests/runtime_config/test_store.py` | 10 | keep | Fast tests for `PsycopgRuntimeConfigStore`. |
| `ps-service/tests/runtime_config/test_store_live.py` | 18 | keep | `postgres_live` tests for `PsycopgRuntimeConfigStore` against a real Postgres. |
| `ps-service/tests/scripts/test_authentik_blueprint.py` | 21 | keep | Structural tests for the bundled Authentik blueprints. |
| `ps-service/tests/scripts/test_chart_no_token_literal.py` | 3 | keep | Static guard: the chart ships no token literal and prints none. |
| `ps-service/tests/scripts/test_check_docstring_provenance.py` | 66 | keep | Tests for `scripts/check_docstring_provenance.py`. |
| `ps-service/tests/scripts/test_check_mock_boundaries.py` | 11 | keep | Unit tests for `scripts/check_mock_boundaries.py`. |
| `ps-service/tests/scripts/test_coding_standards_documentation.py` | 8 | keep | Pin the Documentation guidance in the coding-standards documents. |
| `ps-service/tests/scripts/test_docstring_provenance_wiring.py` | 10 | keep | Tests that the provenance check is wired into the commit hook and the CI gate. |
| `ps-service/tests/scripts/test_grant_access_role.py` | 11 | keep | End-to-end tests for `scripts/grant-access-role.sh`. |
| `ps-service/tests/scripts/test_kind_cluster_config.py` | 4 | keep | Structural regression test for `deploy/kind/cluster.yaml`. |
| `ps-service/tests/scripts/test_measure_skills.py` | 3 | keep | Unit tests for `scripts/measure_skills.py`. |
| `ps-service/tests/scripts/test_no_legacy_deploy_ps_name.py` | 3 | keep | Guard for the `deploy-ps` -> `deploy-ps-prod` rename. |
| `ps-service/tests/scripts/test_ps_upgrade.py` | 3 | keep | Regression tests for `scripts/ps-upgrade.sh`. |
| `ps-service/tests/scripts/test_verify_backup_restore.py` | 9 | keep | Tests for `scripts/verify-backup-restore.sh backup`. |
| `ps-service/tests/scripts/test_verify_backup_restore_e2e.py` | 12 | keep | Tests for `scripts/verify-backup-restore.sh seed / e2e`. |
| `ps-service/tests/scripts/test_verify_backup_restore_restore.py` | 24 | keep | Tests for `scripts/verify-backup-restore.sh restore / wait-ready / verify-provision`. |
| `ps-service/tests/scripts/test_verify_chart_independence.py` | 1 | keep | Regression test for `scripts/verify-chart-independence.sh`. |
| `ps-service/tests/test_config.py` | 64 | keep | Tests for `ps_service.config`: `ServiceConfig` and `load_config()`. |
| `ps-service/tests/test_container_image.py` | 15 | keep | Tests that exercise the *built* `ps-service` runtime image. |
| `ps-service/tests/test_dockerfile.py` | 6 | keep | Static contract tests for the repository-root `Dockerfile` and `.dockerignore`. |
| `ps-service/tests/test_live_marker_selection.py` | 2 | keep | A `postgres_live` run must stay green without FalkorDB or an LLM provider. |
| `ps-service/tests/test_main.py` | 104 | rewrite | identical-body pair claims distinct requirements (#268) |
| `ps-service/tests/test_main_integration.py` | 8 | keep | Subprocess-based integration tests for the `ps_service` process harness. |
| `ps-service/tests/test_release_workflow.py` | 38 | keep | Static assertions on `.github/workflows/on_semver.yml` — the ps-service release pipeline. |
| `ps-service/tests/test_startup_env_parity.py` | 1 | keep | Proves the container smoke test's env vars are sufficient to boot `create_app`. |
| `ps-service/tests/test_state_migrations.py` | 4 | keep | Tests for `ps_service.state_migrations`, the one shared list of PS state migration sources. |
| `ps-service/tests/test_upgrade_scripts_wait_for_jobs.py` | 3 | keep | Static contract test: every policy-system `helm upgrade` waits for Jobs. |
| `ps-service/tests/tools/test_graph_digests.py` | 8 | keep | Tests for `tools/backup-restore-verify/graph_digests.py`. |
| `ps-service/tests/tools/test_seed_tools.py` | 3 | keep | Tests for the `tools/backup-restore-verify` seed helpers. |
| `ps-service/tests/tools/test_tools_help_provenance_free.py` | 3 | keep | Tests that no operator-facing `--help` screen of a tool or script carries provenance. |
| `ps-test-support/tests/test_mock_oidc_provider_device_flow.py` | 11 | keep | Proves `MockOidcProvider`'s own device-authorization + token endpoint simulation. |
| `ps-test-support/tests/test_serialize_live_markers.py` | 2 | keep | Proves `serialize_live_markers` keeps live marker groups out of xdist. |

Totals: keep 745, merge 0, rewrite 7, remove 0; modules 752.
