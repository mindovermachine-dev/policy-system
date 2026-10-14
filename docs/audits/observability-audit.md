<!-- © 2026 Cartman ApS. All rights reserved. -->

# Observability, Resilience and Operability Audit - Findings Report (Issue #259, umbrella #246)

## Scope examined

In scope: `dependency_health`, `main.py`, `ingestion_runs`, `change_monitor`, `llm_interface`, `graph_gateway`, the chart probes and resources, and `docs/artifacts/operations-guide.md` (all under `ps-service/src/ps_service/` unless a path says otherwise). Every outbound call in `ps-service/src` and `ps-cli/src` was traced for timeout, retry and failure behaviour; the readiness model, shutdown sequence, in-flight ingestion handling, chart workloads and the operations guide were read against the code.

Not examined: anything that needs a cluster, Docker or a live process. No `kubectl`, `helm install`/`upgrade`, `az` or container command was run and no process was sent SIGTERM; shutdown, restart and recovery behaviour is derived from code and from the existing tests, which were run. Findings that would need a live check are marked "not verified live" and tracked.

Out of scope and routed: logging content and audit rows (#253, issues #293-#297), security hardening (#252, issues #291, #292), service tunables as chart values (#257, issue #309), backup and restore (#256), guardrails (#260), guide rewrite (#254).

## Method and commands

Tool versions: `helm version --short` gives v4.3.0 (plus the `unittest` plugin 1.1.2), `gh --version` 2.102.0, `uv run --frozen python -B --version` 3.14.6. All commands below are read-only; `git status --short` was empty after each.

- Outbound call enumeration, pass 1 (literal call syntax for the drivers and wrappers the service uses): a grep over `ps-service/src/ps_service` for `psycopg.connect(`, `client_factory(host`, `litellm.(completion|embedding)(`, `transport(request, timeout`, `connect_timeout`, `socket_timeout`, saved as the call-site list (21 lines).
- Pass 2: `grep -rnE 'urlopen|httpx\.|requests\.|redis\.|FalkorDB\(|PyJWKClient|psycopg\.connect' ps-service/src ps-cli/src`, reconciled line by line against pass 1.
- Constants: `grep -rnE '^_?[A-Z_]*(TIMEOUT|ATTEMPTS|BACKOFF|DEADLINE)[A-Z_]*(: [a-z]+)? =' --include='*.py' .` from `ps-service/src/ps_service` (26 lines).
- Health-mark sites: `grep -rnE 'mark_(un)?healthy\(' --include='*.py' .` (74 lines); used to name the actor that restores each gating dependency.
- Library defaults, read without network: `uv run --frozen python -B -c "import litellm; print(litellm.num_retries, litellm.request_timeout)"` prints `None 6000.0`; `DEFAULT_MAX_RETRIES` in the installed litellm `constants.py` is 2; `grep -rn 'num_retries\|max_retries' ps-service/src` is empty; `jwt/jwks_client.py` defaults `timeout` to 30; `inspect.signature(falkordb.FalkorDB.__init__)` shows `socket_timeout=None` and `socket_connect_timeout=None`.
- Chart: `helm template r charts/policy-system --set llm.existingSecret=x --set psService.localTestBypass.enabled=true` (default) and the same with `-f charts/policy-system/values-prod.yaml` plus fake issuer, audience and bootstrap-owner values (prod); an `awk` over each render lists `readinessProbe`, `livenessProbe`, `startupProbe`, `resources`, `terminationGracePeriodSeconds` per workload. `helm lint` on both renders and `helm unittest charts/policy-system` (255 tests, 33 suites passed).
- Existing tests run, one file per command: `tests/test_main.py` (114 passed), `tests/graph_gateway/test_gateway_reconciler.py` (8 passed), `tests/graph_gateway/test_gateway_recovery.py` (11 passed), `tests/mcp_interface/test_get_ingestion_status_tool.py` (17 passed).
- Duplicate search before filing: `gh issue list --state all --search 'timeout OR probe OR readiness OR shutdown OR resources OR healthcheck OR startupProbe'` and a metrics search; #309 and #292 bodies read in full.
- Limits: the call-site greps find literal call syntax only. Callers through injected factories or transports, retries inside libraries and dynamic calls were traced by hand, so AC-BI-002 is claimed against the union of both greps plus the hand trace, never as complete.

## Severity scale

- High: a failure that takes the service down or hides an outage with no operator remedy. None found.
- Medium: a missing bound or recovery path that can turn a dependency fault into a longer outage.
- Low: an inconsistency, an undocumented default or a missing convenience with a clear workaround.

## Outbound calls (AC-BI-002)

| call | site | timeout | retry or failure behaviour | error mapping and `/ready` effect |
| ---- | ---- | ------- | -------------------------- | --------------------------------- |
| FalkorDB connect | `graph_gateway/connection.py:91` (`client_factory(host=host, port=port)`) | none passed; the driver defaults are `socket_timeout=None` and `socket_connect_timeout=None` | none at connect; gateway steps use `call_with_backoff` (`graph_gateway/retry.py:68`; defaults `gateway.py:106-110`: 4 attempts, 0.2 s doubling, reconciler and startup replay cap 30 s) | connection and timeout errors become `GraphUnavailableError` and `mark_unhealthy(FALKORDB)` (`retry.py:168-172`); FalkorDB gates `/ready` |
| FalkorDB read tool | `query_engine/cypher_query.py:222` (`graph.query(..., timeout=timeout_ms)`), default `config.py:29` 5000 ms | server-side 5 s | none; the error is returned to the caller | does not mark health (see OBS-18) |
| FalkorDB gateway writes | `graph_gateway/gateway.py` through `retry.py` | no client timeout per attempt | retried with backoff as above; permanent errors not retried | gateway marks `FALKORDB` |
| PS state Postgres | `persistence/connection.py:51` (`psycopg.connect(...)`) | none (no `connect_timeout`, no statement timeout) | none per request; startup connect failure is fatal (`main.py:382-431`) | store errors mark `STATE_POSTGRES`; gates `/ready` |
| Passkey-signing Postgres | `passkey_signing/store.py:227` | none | none | not gating (`main.py:250` `_GATING_DEPENDENCIES`), named in `unhealthy_dependencies` only |
| Provisioning Postgres connect (admin job) | `graph_gateway/provision.py:196-205` | `connect_timeout=_CONNECT_TIMEOUT_SECONDS` (30 s, `:69`), deadline 120 s (`:71`) | loops until the deadline | exits non-zero; the chart checks `(backoffLimit+1) * connectTimeoutSeconds <= activeDeadlineSeconds` (`ps-state-provision-job.yaml:3-5`). Separate path from the two rows above |
| LLM completion, structured, embedding | `llm_interface/client.py:135,174,207` | 60 s explicit per call (`completion.py:21`, `structured_completion.py:39`, `embedding.py:21`) | the client library retries twice by default; the service sets no `num_retries` or `max_retries` | `openai.OpenAIError` becomes `LlmProviderError`; tracked, not gating |
| Cellar and ELI | `ingestion/adapters/cellar_eli/fetch.py:93,178` (`:40` 30 s), `change_monitor/cellar_consolidated.py:218` (`:69` 30 s) | 30 s explicit | none; the change monitor is polled periodically by its caller | typed fetch errors; tracked, not gating |
| Curated source | `curated_source/http_fetch.py:79` (`:19` 30 s) | 30 s explicit | none | typed error; not in the registry |
| IdP OIDC discovery | `auth/discovery.py:153` (`:19` 30 s), called at startup from `auth/startup.py:40` | 30 s explicit | none; a startup failure ends the process (fail closed) | `AuthConfigurationError` |
| IdP key-set fetch | `auth/verifier.py:119` (`jwt.PyJWKClient`, `cache_jwk_set=True`, `lifespan=300`) | library default 30 s, lazy on first use, one refetch on an unknown key id | none beyond that | token rejected with a closed reason; readiness unaffected |
| Authentik invite | `invitations/client.py:191` (`:29` 30 s) | 30 s explicit | none | typed error; not in the registry |
| ps-cli to PS Service | `ps-cli/src/ps_cli/http_client.py:521` (`httpx.Timeout(connect=5, read=30, write=5, pool=5)`), long operations `:74,:82` (read 1800 s) | explicit | none; `httpx.ReadTimeout` and `TransportError` mapped (`:569-592`) | user-facing error |
| ps-cli IdP calls | `ps-cli/src/ps_cli/oidc_discovery.py:48`, `device_flow.py:48` (5 s each) | explicit | device flow polls by design | user-facing error |

Every row of the call-site list is accounted for: the 21 lines of pass 1 are the FalkorDB connect (1), `psycopg.connect` (provision, state, passkey: 3 plus the provision constants), the three LLM calls, the five `transport(request, timeout` sites and the `connect_timeout` constants; pass 2 added the `PyJWKClient` and the ps-cli `httpx` clients. There is no `httpx` or `requests` in `ps-service/src`.

## Shutdown, restart and in-flight work (AC-BI-004)

Not verified live: no process was signalled. The table is from code and the existing tests that were run.

| in-flight work | on SIGTERM | persisted state | resume or clean mark | evidence |
| -------------- | ---------- | --------------- | -------------------- | -------- |
| HTTP requests | uvicorn stops accepting and waits `graceful_shutdown_seconds` (default 10, `config.py`, passed at `main.py:1164`); requests still running afterwards are cut | none | the caller retries | `tests/test_main.py::test_main_calls_uvicorn_run_with_app_host_and_graceful_shutdown_timeout` |
| Graph write gateway, drift monitor, startup replay, reconciler | lifespan shutdown calls `_shutdown_graph_gateway` (`main.py:683`, called at `:1000`): drift monitor, replay, then reconciler, each stopped with a 5 s cap (`main.py:556-558`), run one after another; a stop that fails or times out is logged and shutdown goes on | the graph log is append-only; the applied marker only advances after an applied batch, so an interrupted pass redoes the pending entries | startup replay and the reconciler resume from the marker on the next start | `test_main.py::test_shutdown_stops_the_replay_between_pages_and_the_reconciler`, `test_gateway_reconciler.py::test_a_reconciler_that_does_not_stop_is_abandoned_and_reported`, `test_gateway_recovery.py` |
| Ingestion run (MCP `start_ingestion`) | worker is a daemon thread (`ingestion_runs/dispatch.py:8-10,94`); it is killed with the process | the run row stays `running` | reconciled to `failed` with reason `interrupted` only when someone polls that run id (`mcp_server.py:1270,1493`); no startup sweep; the run is not resumed | `test_get_ingestion_status_tool.py::test_a_run_row_left_running_by_a_restarted_process_polls_as_interrupted` |
| REST ingestion progress | in-memory stage map (`api/run_status.py`), empty after a restart | none | best-effort by its own contract | module docstring |
| Change-monitor sweep | runs inside a request or tool call; cut at the grace window | progress markers are written after each completed stage; the succession link is the last write | the next call resumes at the first missing stage | `change_monitor/trigger.py` module docstring (read; not run live) |

Worst-case shutdown sequence: 5 s + 5 s + 5 s = 15 s only if all three stops hang, longer than the 10 s uvicorn window and shorter than the 30 s the pod receives by default (the chart sets no `terminationGracePeriodSeconds` for PS Service; the two `30` values in the prod render belong to the authentik subchart). Tracked in #317. The poll-only reconciliation of `running` rows is tracked in #316.

## Probes against real dependencies and recovery (AC-BI-003)

`/health` is a constant 200 (`main.py:1037`) and never reads a dependency, so liveness stays up whichever dependency is down. `/ready` (`main.py:1053`) returns 200 only when `app.state.ready` and `all_healthy(_GATING_DEPENDENCIES)` (`main.py:1105`); the gating set is FalkorDB, state Postgres and graph replay (`main.py:250`), and `unhealthy_dependencies` names the six `_READY_DEPENDENCIES` that are down (`main.py:226`).

| dependency | gating | unhealthy: `/ready` | who marks it healthy again | test evidence | verdict |
| ---------- | ------ | ------------------- | -------------------------- | ------------- | ------- |
| FalkorDB | yes | 503, lists `falkordb` | at startup: each `/ready` poll re-probes while `app.state.ready` is false (`main.py:780,1100`; `test_ready_recovers_once_falkordb_becomes_reachable_after_a_failed_startup_probe`, `test_ready_keeps_reporting_not_ready_on_repeated_polls_while_falkordb_stays_down`). Mid-run: only a successful gateway apply or a graph writer call (`gateway.py:1355,1427`, `*/graph_writer.py`), never a probe | `test_ready_flips_to_not_ready_when_a_dependency_is_marked_unhealthy_after_successful_startup` and `test_ready_self_heals_once_the_unhealthy_dependency_recovers` call `mark_healthy` by hand; no test restores the dependency and expects recovery without traffic | startup recovers; mid-run recovery depends on traffic that the 503 removes: OBS-17, #318 |
| State Postgres | yes | 503, lists `state_postgres` | store calls (`persistence/connection.py:95`, `audit/store.py:581`, `ingestion_runs/store.py:272`) and the startup retry | `test_ready_is_not_ready_when_state_postgres_check_fails` | same as above |
| Graph replay | yes | 503, lists `graph_replay`, all other paths answer 503 with `Retry-After` | the replay task ends and marks it healthy (`main.py:636`) | `test_ready_reports_not_ready_while_startup_replay_runs_then_ready_when_it_ends`, `test_ready_does_not_self_heal_to_ready_while_replay_is_running`, `test_infrastructure_error_during_startup_replay_holds_not_ready_and_retries` | meets the criterion |
| LLM Interface | no, by design | 200, listed | next successful call | `test_ready_stays_ready_when_llm_interface_is_marked_unhealthy_live`, `test_ready_self_heals_llm_interface_name_from_unhealthy_dependencies_without_restart` | no action: documented design (`main.py:244-250` and the `ready` docstring at `:1053`) |
| Cellar and ELI | no, by design | 200, listed | next successful call | the Cellar equivalents of the two tests above | no action |
| Passkey-signing Postgres | no, by design | 200, listed | next successful call | `test_ready_stays_ready_when_only_passkey_signing_postgres_check_fails` | no action |

Hypothesis tested: after a mid-run FalkorDB or state-database outage the pod leaves rotation and never returns without a restart. Confirmed from code: the re-probe at `main.py:1100` runs only while `app.state.ready` is false, the registry changes only through `mark_*` calls, the query engine never marks health (`grep -rn 'mark_' ps-service/src/ps_service/query_engine` is empty), and liveness is blind to dependencies, so nothing restarts the pod. Not verified live. Tracked in #318 as a Medium `fix`.

## Chart probes, resources and grace (AC-BI-005)

From the default and prod renders (`helm lint` clean on both; `helm unittest` 255 passed).

| workload | readiness | liveness | startup | resources | termination grace | justified in the chart |
| -------- | --------- | -------- | ------- | --------- | ----------------- | ---------------------- |
| PS Service | `/ready`, 5 s timeout, 15 s period, 5 failures (`values.yaml` `psService.probes`, template `ps-service-deployment.yaml:121`) | `/health`, same numbers (`:128`) | none, no initial delay | requests 500m / 512Mi, limits 2 CPU / 2Gi (`values.yaml` `psService.resources`) | default 30 s | numbers have no rationale comment (OBS-26) |
| FalkorDB | none | none | none | none | default | no (OBS-21, OBS-25) |
| PS Postgres | `pg_isready` over TCP (`ps-postgres-deployment.yaml:76`) | none, deliberately (comment `:74`) | none | none | default | probe: yes; resources: no (OBS-25) |
| Authentik Postgres | none | none | none | none | default | no (OBS-22, OBS-25) |
| Provision Job | not applicable | not applicable | not applicable | none; bounded by `backoffLimit` and `activeDeadlineSeconds` (checked at render) | not applicable | deadline arithmetic: yes; resources: no (OBS-25) |
| Authentik server and worker (subchart) | set | set | set | set | 30 s | subchart defaults, no action |

PS Service migrations run during startup, before the server accepts connections, and the liveness probe counts from container start; a start slower than five failed 15 s periods would be restarted (OBS-23). The `waitForPostgres` init container (`values.yaml`, 300 s) already holds the pod back until Postgres accepts connections.

## Operations guide against failure modes (AC-BI-006)

| failure mode | guide location | status |
| ------------ | -------------- | ------ |
| FalkorDB down at start, or graph replay running | `operations-guide.md:324-365` | covered (`/ready` 503 with `graph_replay`, retry with backoff) |
| State Postgres unreachable | `:568-569` | covered; matches the code (startup connect failure is fatal, `main.py:382-431`) |
| Privileged migration missing | `:286-292` | covered |
| Gated graph | `:348-365` | covered |
| Graph write gateway not running (503) | `:847` | covered |
| LLM unreachable | `:849` | covered |
| `not_ready` causes | `:845` | incomplete: names only FalkorDB and ingestion config, not state Postgres or graph replay |
| `/ready` 503 persisting after a mid-run outage | none | missing (until #318 ships) |
| SIGTERM or restart during an ingestion | none | missing (until #316 ships) |
| Passkey-signing Postgres down | none | missing |
| IdP or key-set unreachable | none | missing |
| Cellar down | none | missing |
| Container killed at its memory limit | none | missing |

The guide is owned by #254, which is rewriting it, so no guide line was edited here. The exact lines and replacement text were posted as a comment on #254. The operations guide, the chart and the source are unchanged by this audit.

## Findings

| id | location | severity | evidence | disposition |
| -- | -------- | -------- | -------- | ----------- |
| OBS-01 | `graph_gateway/connection.py:91` | Medium | FalkorDB client built with no connect or socket timeout (driver defaults are `None`); a silent peer blocks the caller until the operating system gives up | issue #314 |
| OBS-02 | `graph_gateway/gateway.py` write path | Low | Each gateway write attempt has no client-side timeout; the retry loop bounds attempts, not the duration of one | issue #314 |
| OBS-03 | `persistence/connection.py:51` | Medium | State Postgres connect has no `connect_timeout` and no statement timeout; the startup migration connect (`main.py:382-431`) can stall the start | issue #314 |
| OBS-04 | `passkey_signing/store.py:227` | Medium | Passkey-signing Postgres connect has no timeout | issue #314 |
| OBS-05 | `llm_interface/client.py:135,174,207` | Low | Per-call timeout is explicit (60 s) but the retry count is the client library default (2), not set by the service, and changeable by an environment variable | issue #315 |
| OBS-06 | `auth/verifier.py:119` | Low | Key-set fetch timeout is the JWT library default (30 s), not set in the service | issue #315 |
| OBS-07 | Cellar, curated source, IdP discovery, invitations (`fetch.py:40,93,178`, `http_fetch.py:19`, `discovery.py:19`, `invitations/client.py:29`, `cellar_consolidated.py:69`) | Low | Explicit 30 s timeout, no retry, typed error mapping; the change monitor re-polls periodically | no action: bounded and mapped; retry would belong to the caller's schedule |
| OBS-08 | `ps-cli/src/ps_cli/http_client.py:521`, `oidc_discovery.py:48`, `device_flow.py:48` | Low | Every httpx client has an explicit `httpx.Timeout`; errors are mapped | no action: explicit |
| OBS-09 | `graph_gateway/provision.py:196-205`; `ps-state-provision-job.yaml:3-5` | Low | Provisioning has a connect timeout, a deadline and a render-time check that the retry budget fits | no action: bounded |
| OBS-10 | `graph_gateway/retry.py:68`; `gateway.py:106-110` | Low | Gateway retries are bounded: 4 attempts, 0.2 s doubling, 30 s cap for background loops; permanent errors are not retried | no action: documented behaviour |
| OBS-11 | `main.py:556-558,683` | Low | Shutdown stops three components sequentially with 5 s caps (15 s worst case) against a 10 s server window | issue #317 |
| OBS-12 | `charts/policy-system/templates/ps-service-deployment.yaml` | Low | No `terminationGracePeriodSeconds`; the pod gets 30 s regardless of the configured shutdown window | issue #317 |
| OBS-13 | `ingestion_runs/dispatch.py:8-10`; `mcp_server.py:1270,1493` | Medium | Run rows left `running` by a restart are reconciled only when polled; no startup sweep | issue #316 |
| OBS-14 | `api/run_status.py` | Low | REST progress is an in-memory stage map, empty after restart | no action: best-effort by its documented contract |
| OBS-15 | `main.py:683-720` | Low | Failures and timeouts of the three stops are caught and logged; shutdown proceeds | no action: tested (`test_a_reconciler_that_does_not_stop_is_abandoned_and_reported`) |
| OBS-16 | whole process | Low | No live SIGTERM or restart was performed (no cluster access) | no action: shutdown paths are covered by the code reading and the existing tests above; not verified live |
| OBS-17 | `main.py:1100,1105` | Medium | A gating dependency that fails after startup keeps `/ready` at 503 until traffic that the 503 removes marks it healthy; liveness is blind to it | issue #318 |
| OBS-18 | `query_engine/falkordb_client.py` | Low | The read-only query path never marks FalkorDB healthy or unhealthy | issue #318 |
| OBS-19 | `main.py:244-250,1053` | Low | LLM, Cellar and passkey-signing Postgres are non-gating by design and listed in `unhealthy_dependencies` | no action: documented design |
| OBS-20 | `main.py:1037` | Low | `/health` is a constant 200 | no action: a liveness probe must not depend on external systems |
| OBS-21 | `charts/policy-system/templates/falkordb-deployment.yaml` | Medium | No readiness or liveness probe: the kubelet never restarts a hung FalkorDB and never holds traffic back from it | issue #319 |
| OBS-22 | `charts/policy-system/templates/authentik-postgres-deployment.yaml` | Low | No probes | issue #319 |
| OBS-23 | `charts/policy-system/templates/ps-service-deployment.yaml:121-134` | Low | No startup probe and no initial delay; liveness counts from container start while migrations run | issue #319 |
| OBS-24 | `charts/policy-system/templates/ps-postgres-deployment.yaml:74-76` | Low | Readiness only, with a comment that explains why there is no liveness probe | no action: justified in the chart |
| OBS-25 | FalkorDB, PS Postgres, authentik Postgres, provision Job | Medium | No resource requests or limits | issue #320 |
| OBS-26 | `charts/policy-system/values.yaml` `psService.resources`, `psService.probes` | Low | The numbers have no rationale comment | issue #320 |
| OBS-27 | `ps-service/src` | Low | No metrics of any kind (grep for metrics libraries and a metrics route is empty); state is visible only through `/ready` and logs | issue #321 |
| OBS-28 | `operations-guide.md:845` | Low | The not-ready row omits state Postgres and graph replay | issue #254 |
| OBS-29 | `operations-guide.md` troubleshooting table | Low | No row for the mid-run recovery gap, interrupted ingestion, passkey Postgres, IdP, Cellar or memory-limit kills | issue #254 |
| OBS-30 | `operations-guide.md:568-569` | Low | "Does not start while unreachable" matches the code | no action: accurate |
| OBS-31 | authentik server and worker | Low | Probes, resources and grace are set by the subchart defaults | no action: third-party chart, hardening belongs to #292 |

## False positives and reasons

- "The `/ready` startup latch never recovers": refuted. `main.py:1100` re-probes every gating dependency on each poll while `app.state.ready` is false, so the startup case is handled (`test_ready_recovers_once_falkordb_becomes_reachable_after_a_failed_startup_probe`). Only the mid-run case is a finding (OBS-17).
- "`persistence/provision.py` has no connect timeout": that path does not exist; the only provisioning connect is `graph_gateway/provision.py:205`, which is bounded (OBS-09).
- "The litellm module has no retries": the module attribute `num_retries` is `None`, but the OpenAI client path retries twice by default, so the real behaviour is OBS-05.
- "Guide line 568 contradicts the code": it does not; startup failure is fatal (OBS-30).
- "`PS_QUERY_TIMEOUT_MS` and graceful-shutdown seconds are not tunable": already tracked in #309, so no new issue; a comment was posted there.

## Removal proof

No removal; grep not applicable. This audit changes no source, test, chart or guide file; the only tracked file added is this document.

## Superseded and removed items

None. Nothing was made obsolete.

## Acceptance criteria confirmation

Status: met, partial (the audit part is met and the remediation is tracked in an issue because the rule forbids doing it inline), or not applicable.

| AC | status | evidence |
| -- | ------ | -------- |
| AC-BI-001 | met | This document: "Scope examined", "Method and commands" with tool versions and exact commands, and "Findings" with id, location, severity, evidence and disposition. |
| AC-BI-002 | partial | "Outbound calls" lists every outbound call found by two greps plus the hand trace, each with timeout, retry and failure behaviour. Gaps (no connect or socket timeout for FalkorDB and Postgres, library-default retry and key-set timeout) are tracked in #314 and #315, not fixed inline, because they change shipped behaviour. |
| AC-BI-003 | partial | "Probes against real dependencies and recovery": liveness stays up for every dependency, readiness reports not-ready for the three gating ones, startup recovery is tested. Mid-run recovery without traffic is not provided and no test restores a dependency and expects recovery: tracked in #318. Not verified live. |
| AC-BI-004 | partial | "Shutdown, restart and in-flight work" tabulates each kind of in-flight work from code and the tests that were run. Interrupted runs are cleaned only on poll, and the shutdown budget exceeds the server window: tracked in #316 and #317. No live SIGTERM. |
| AC-BI-005 | partial | "Chart probes, resources and grace": every workload is listed; PS Service is set, the data stores and the Job are not. Tracked in #319 and #320; no chart value was changed. |
| AC-BI-006 | partial | "Operations guide against failure modes": each failure mode has a covered or missing status with line numbers. Missing rows are routed to #254 with replacement text (comment posted) because #254 owns and is rewriting the guide. |
| AC-BI-007 | met | Every row of "Findings" has exactly one disposition (`issue #N`, `no action: <reason>`); the validator output is in "Verification evidence". |
| AC-BI-008 | met | Each filed issue (#314-#321) has Problem, Options and Recommendation; nothing touching a public contract or persistent data was changed inline. |
| AC-BI-009 | met | The eight issues are typed `fix` or `feat`; no timeout, retry, probe, resource, health or shutdown behaviour was changed; `git status --short` shows only this document. |
| AC-BI-010 | not applicable | No fix was made inline; no code was touched. The existing tests named in this document were run and pass. |
| AC-BI-011 | met | "Removal proof": nothing was made obsolete; no removal. |
| AC-BI-012 | met | The scan in "Verification evidence" finds no secret values, credentials, endpoints or exploit detail in this document or in the new issue bodies; resilience gaps are described generically. No private-advisory candidate was found. |
| AC-BI-013 | met | "Sibling issue review" covers every open sub-issue of #246 except #259 and every issue filed from #263 onward (61 rows, read from the issue bodies); comments were posted on #254, #260, #273, #277, #292 and #309. |

## Issues filed

| issue | title | findings |
| ----- | ----- | -------- |
| #314 | fix(ps-service): bound connection and socket waits for FalkorDB and Postgres | OBS-01 to OBS-04 |
| #315 | fix(ps-service): make LLM retry and key-set fetch timeout settings explicit | OBS-05, OBS-06 |
| #316 | fix(ps-service): reconcile interrupted ingestion runs at startup | OBS-13 |
| #317 | fix(ps-service): align the shutdown sequence with the termination grace window | OBS-11, OBS-12 |
| #318 | fix(ps-service): let /ready recover after a mid-run dependency outage without a restart | OBS-17, OBS-18 |
| #319 | fix(chart): add startup and missing readiness probes to chart-owned workloads | OBS-21 to OBS-23 |
| #320 | feat(chart): set resource requests and limits on data stores and the provision job | OBS-25, OBS-26 |
| #321 | feat(ps-service): expose operational metrics | OBS-27 |

Existing issues reused instead of filing: #309 (graceful-shutdown seconds and query timeout as chart values; comment posted), #254 (operations guide; comment posted). All eight are native sub-issues of #246 and carry "Part of #246" and "Related: #259".

## Sibling issue review

Reviewed against the outcomes above: 61 issues, being every open sub-issue of #246 except #259 itself (the paginated sub-issue list holds 62 open items including #259) and every issue filed from #263 onward (#263-#321, including this audit's eight). The earlier audit sub-issues #247-#253, #255-#258 are closed. The body of every issue was fetched with `gh issue view` and read before its row was written. Comments were posted only where an order or scope note applies.

| issue | state | title | decision |
| ----- | ----- | ----- | -------- |
| #254 | open | chore(audit): audit documentation against the code and complete container architecture | Owns the operations guide; the guide gaps found here (incomplete not-ready row, missing failure-mode rows) were posted there as a comment with line numbers and replacement text. Commented. |
| #260 | open | chore(audit): add automated guardrails that keep the cleanup from regressing | Guardrail list; a chart test that every chart-owned workload declares probes and resources is a candidate once the chart issues ship. Commented, scope otherwise unchanged. |
| #263 | open | refactor(ps-service): review production symbols reachable only from tests | Reviews production symbols reachable only from tests; this audit adds and removes no symbols. No change needed. |
| #264 | open | build(deps): declare starlette and redis as direct ps-service dependencies | Declares starlette and redis as direct dependencies; no timeout or retry setting changes here. No change needed. |
| #265 | open | chore(spikes): remove local spike directories whose owning issues are closed | Removes local spike directories; no overlap. No change needed. |
| #266 | open | chore(repo): resolve the placeholder revision in .git-blame-ignore-revs | Placeholder revision in the blame-ignore file; no overlap. No change needed. |
| #267 | open | chore(skills): single-source the duplicated reasoning skill | Duplicate reasoning skill; no overlap. No change needed. |
| #268 | open | test(ps-service): strengthen identical-body tests that claim distinct requirements | Identical-body tests; the tests this audit cites (readiness, reconciler, run status) are not in the pairs. No change needed. |
| #269 | open | test(ps-service): close mutation-testing gaps found in the test-value audit | Mutation-testing survivors; the new issues add no code. No change needed. |
| #270 | open | refactor(ps-service): move shared errors, models and write-access helpers out of api so components do not import the entry layer | Moves shared errors and models out of the entry layer; the readiness and shutdown fixes do not add imports across that boundary. No change needed. |
| #271 | open | refactor(ps-service): break the config/curated_source and domain_mapper/ingestion package cycles | Package cycles in config, curated_source and domain_mapper; no overlap. No change needed. |
| #272 | open | refactor(mcp_interface): split mcp_server.py by tool group | Splits mcp_server.py; the run-status reconcile function the startup sweep reuses lives there, so #316 follows the moved code if the split lands first. No change to scope or criteria. |
| #273 | open | refactor(ps-service): split the mixed-responsibility modules api/ingestion_orchestration, main and policy_lifecycle/service | Proposes to split main.py, where the shutdown helpers (#317) and the /ready handler (#318) live. Order note posted (rebase whichever lands second). Commented. |
| #274 | open | refactor(ps-service): decide whether the per-component edge_identity and graph-name helper copies stay independent | Per-component helper copies; no overlap. No change needed. |
| #275 | open | docs(licence): declare the project licence | Project licence; no overlap. No change needed. |
| #276 | open | chore(deps): clear markdown-tooling npm advisories and refresh npm dev dependencies | Markdown tooling advisories; dev-only. No change needed. |
| #277 | open | fix(chart): pin the FalkorDB image tag instead of latest | Pins the FalkorDB image tag in the same Deployment template that #319 and #320 edit. Order note posted. Commented. |
| #278 | open | fix(docker): pin the Python base image by digest | Pins the Python base image; no overlap with probes or resources. No change needed. |
| #279 | open | fix(deps): bump ps-service runtime dependencies within their current major and pin structlog | Runtime dependency bumps including the LLM client and the JWT library, whose retry and timeout defaults #315 makes explicit; explicit values make #315 independent of the bump. No change needed. |
| #280 | open | fix(deps): decide when to move openai to the next major version | Decides the next openai major; the LLM client retry default that #315 makes explicit lives in that library, so the decision should re-check it. No change needed. |
| #281 | open | chore(deps): bump Python dev tooling (ruff, basedpyright, pre-commit) | Dev tooling bumps; no overlap. No change needed. |
| #282 | open | test(ps-service): add migration-apply and N-1 to N upgrade tests for authz and runtime_config | Migration tests for authz and runtime_config; no overlap. No change needed. |
| #283 | open | test(ops): run the backup-restore verifier end to end in a disposable cluster or CI | Runs the backup-restore verifier end to end; restore flows are not changed by the new issues. No change needed. |
| #284 | open | fix(scripts): extend backup-restore verifier counts to every state table and the signing database | Extends verifier row counts; no overlap. No change needed. |
| #285 | open | perf(ps-service): decide on graph indexes for RegulatoryInstrument.celex and status lookups | Graph index decision; the connect and query timeouts of #314 are independent of indexes. No change needed. |
| #286 | open | refactor(ps-service): decide on the unused and redundant signing database indexes | Signing database index decision; no overlap with the signing connection timeout in #314. No change needed. |
| #287 | open | docs(ps-service): align persistence descriptions with current state (init script header, operational and sentinel labels) | Persistence description fixes including the PS Postgres init script header; no probe or resource content. No change needed. |
| #288 | open | fix(ps-service): add request-size limiting hardening for inbound and outbound payload paths | Request-size limits for inbound and outbound payloads; size, not duration, so distinct from the timeouts in #314 and #315. No change needed. |
| #289 | open | fix(ps-service): add input-validation hardening for public metadata endpoints | Input validation of public metadata endpoints; no overlap. No change needed. |
| #290 | open | docs(architecture): reconcile MCP interface authorization rows with the implemented role gates | Authorization rows of the architecture document; no overlap. No change needed. |
| #291 | open | feat(ps-service): add request-throttling hardening | Request throttling; throttling bounds request rate, #314 bounds dependency waits. Related, not duplicate. No change needed. |
| #292 | open | fix(chart): apply a pod security-context and network-policy hardening baseline | Pod security-context and network-policy baseline over the same chart-owned workloads that #319 and #320 touch. Order note posted. Commented. |
| #293 | open | fix(ps-service): log hygiene hardening for exception text in operator log entries | Exception text in log entries; the new issues add no log fields. No change needed. |
| #294 | open | fix(ps-service): log hygiene hardening for user identifiers in operator log entries | User identifiers in log entries; no overlap. No change needed. |
| #295 | open | fix(ps-service): unify the log outcome vocabulary and stop logging expected conditions as failures | Log outcome vocabulary; the readiness and shutdown fixes should use the settled vocabulary when they log. No change needed. |
| #296 | open | feat(ps-service): emit server request logs in the structured log format | Structured server request log; no overlap with probes or metrics (#321 covers metrics only). No change needed. |
| #297 | open | feat(ps-service): add audit records for security-relevant actions not yet audited | Audit rows for security-relevant actions; metrics in #321 are a separate signal. No change needed. |
| #298 | open | refactor(tests): remove the remaining type-ignore suppressions | Type-ignore removal in tests; no overlap. No change needed. |
| #299 | open | docs(coding-standards): decide whether provenance rules cover config-file comments | Provenance rules for config-file comments; the comments proposed for chart values in #319 and #320 must follow whichever rule it settles. No change needed. |
| #300 | open | chore(lint): prune stale lint ignores and require reasons on suppressions | Lint ignores; no overlap. No change needed. |
| #301 | open | refactor(ps-service): resolve the graph name once through the service configuration | Resolves the graph name once; no overlap with the FalkorDB connection settings in #314. No change needed. |
| #302 | open | docs(coding-standards): settle ambiguous naming, exception and suppression rules | Coding-standard wording; no overlap. No change needed. |
| #303 | open | fix(ps-service): declare error responses, security scheme and API metadata in the OpenAPI document | OpenAPI error responses; the readiness body shape is unchanged by #318. No change needed. |
| #304 | open | feat(ps-service)!: settle the MCP tool naming convention and converge status models | MCP tool naming; no overlap. No change needed. |
| #305 | open | fix(ps-service)!: converge JSON error body shapes across REST, approval routes and MCP | Converges error body shapes, including the readiness 503 body; #318 keeps that body unchanged. No change needed. |
| #306 | open | feat(ps-service): define pagination and bounds for list operations | Pagination for list operations; no overlap. No change needed. |
| #307 | open | fix(ps-service): declare request validation in models instead of hand-coded parsers | Request validation in models; no overlap. No change needed. |
| #308 | open | feat(ps-cli): show whether an ingest was fresh or already ingested | ps-cli ingest outcome display; the CLI timeouts were found explicit. No change needed. |
| #309 | open | feat(chart): expose service tunables as chart values | Service tunables as chart values, including graceful-shutdown seconds and query timeout; the new tunables from #314, #315 and #317 should be added to its list. Commented. |
| #310 | open | fix(scripts): resolve shellcheck findings in deploy and release scripts | shellcheck findings; no overlap. No change needed. |
| #311 | open | refactor(scripts): move duplicated deploy shell functions into scripts/lib | Duplicated deploy shell functions; no overlap. No change needed. |
| #312 | open | fix(scripts): use a single source for Azure LLM deployment defaults | LLM deployment defaults; no overlap. No change needed. |
| #313 | open | ci(workflows): supply-chain hardening of workflow dependencies | CI supply-chain hardening; no overlap. No change needed. |
| #314 | open | fix(ps-service): bound connection and socket waits for FalkorDB and Postgres | Filed by this audit: connection and socket waits for FalkorDB and Postgres. |
| #315 | open | fix(ps-service): make LLM retry and key-set fetch timeout settings explicit | Filed by this audit: explicit LLM retry and key-set fetch timeout. |
| #316 | open | fix(ps-service): reconcile interrupted ingestion runs at startup | Filed by this audit: startup reconcile of interrupted ingestion runs. |
| #317 | open | fix(ps-service): align the shutdown sequence with the termination grace window | Filed by this audit: shutdown sequence and pod termination grace. |
| #318 | open | fix(ps-service): let /ready recover after a mid-run dependency outage without a restart | Filed by this audit: readiness recovery after a mid-run outage. |
| #319 | open | fix(chart): add startup and missing readiness probes to chart-owned workloads | Filed by this audit: startup and missing readiness probes. |
| #320 | open | feat(chart): set resource requests and limits on data stores and the provision job | Filed by this audit: resource requests and limits. |
| #321 | open | feat(ps-service): expose operational metrics | Filed by this audit: operational metrics. |

## Verification evidence

- Findings validator (`validate_findings.py`, reads this table and the GitHub API): 31 rows, one disposition each; every `issue #N` exists, is open and is a native sub-issue of #246 (or the existing owner #254). Result: OK.
- Tracked tree: `git status --short` lists only `docs/audits/observability-audit.md`.
- Full suite: skipped. No Python source or test file changed, so the tree code equals main; the targeted tests above were run instead.
- `uv run pre-commit run --files docs/audits/observability-audit.md`: prettier and cspell passed, the other hooks have no files to check.
- Over the unchanged tree: `uv run --frozen ruff check .` passed, `ruff format --check` clean, `basedpyright` 0 errors, `scripts/check_docstring_provenance.py --tracked` exit 0, `scripts/check_mock_boundaries.py` exit 0.
- Public hygiene: a grep of this document and of the eight issue bodies for credential words, hosts and addresses finds only option names, chart value names and the fake-value render flags; no secret, endpoint or exploit detail.
