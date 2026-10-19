<!-- © 2026 Cartman ApS. All rights reserved. -->

# Logging Cleanliness and Data Leakage Audit - Findings Report (Issue #253, umbrella #246)

## Scope examined

Covered: `ps-service/src/ps_service/logging`, `ps-service/src/ps_service/audit`, every log, print and stderr call site in `ps-service/src`, `ps-cli/src`, `tools`, `scripts` and `ps-test-support`, exception handlers, and the Helm chart and image log configuration.

Out of scope and routed: security threat coverage (#252, closed), log shipping, retention and server log destination (#259), coding-standard conformance (#251), documentation drift (#254), API contract (#255).

## Method and commands

- Call-site inventory: an AST script (appendix) over `ps-service/src ps-cli/src tools scripts ps-test-support`, run with `uvx python@3.14 callsites.py . <dirs>`. It counts `emit_log_entry`, `print`, `logging.*`/logger calls, `traceback.print_exc`, `sys.stderr.write`, `sys.stdout.write` and `warnings.warn`.
- Cross-check: `grep -c 'emit_log_entry(' -r ps-service/src` gives 95 matches in 45 files against 92 AST calls; the 3 extra matches are the definition line and two message or docstring mentions.
- Debris scan: `grep -rnE 'sys\.(stderr|stdout)\.write|warnings\.warn|basicConfig|getLogger|breakpoint\(|pprint|pdb' ps-service/src ps-cli/src tools scripts ps-test-support --include='*.py'`.
- Outcome and field vocabulary: AST script `outcomes.py` (appendix) over `ps-service/src`, resolving module-level string constants.
- Exception sinks: AST script `exc_sinks.py` (appendix) listing every log or print argument that references an `except ... as X` name.
- Leakage test: `ps-service/tests/logging/test_log_leakage_canary.py` (see Canary test).
- Limits: static analysis cannot see dynamic field values; the live cluster was not queried; third-party library loggers were checked for configuration only (no `basicConfig` or verbosity switch exists in the source).
- Per D-16 of this audit wave, site lists and the description of any weakness are held privately until the filed fix issues ship. This document gives categories and counts only.

## Severity scale

- High: a secret or personal-data leak path reachable by a third party (reported privately, never in this document).
- Medium: log content that can carry sensitive values or breaks the documented convention in a way operators rely on.
- Low: inconsistency or noise with no leakage effect.

## Inventory

| Package | Facade `emit_log_entry` | stdlib logger | `print` | `sys.stderr`/`sys.stdout` write |
| --- | --- | --- | --- | --- |
| `ps-service/src` | 92 (45 files) | 1 logger, 1 call | 0 | 9 (6 stderr, 3 stdout) |
| `ps-cli/src` | 0 | 0 | 29 (+1 `traceback.print_exc`) | 0 |
| `tools` | 0 | 0 | 77 | 7 |
| `scripts` | 0 | 0 | 0 | 9 |
| `ps-test-support` | 0 | 0 | 0 | 0 |

Non-facade service sinks: the stdlib fallback in `api/error_handlers.py` (used only when the facade itself fails, to avoid recursion), the facade's own stderr mirror and last-resort fallback in `logging/emitter.py`, and startup/command-line sinks in `graph_gateway/provision.py` and `domain_schema/__main__.py`.

## Findings

| id | location | severity | evidence | disposition |
| --- | --- | --- | --- | --- |
| LOG-01 | `ps-service/src` (all packages) | Low | No stray `print`, `breakpoint`, `pprint` or `basicConfig` in service code; 0 `print` calls by AST count. | no action: nothing to remove; service code uses the facade |
| LOG-02 | `api/error_handlers.py` stdlib logger fallback | Low | One `logging.getLogger` with one call, reached only when emitting through the facade failed; logs the exception class name only. | no action: logging-failure fallback must not recurse into the facade |
| LOG-03 | `graph_gateway/provision.py`, `domain_schema/__main__.py` stderr/stdout writes | Low | Command-line entry points that run before or outside the service log; provisioning errors are class name or fixed sanitised text. | no action: command-line contract |
| LOG-04 | `ps-cli/src` (29 `print`, 1 traceback) | Low | User-facing command output; error and `-v` traceback go to stderr. No access token, refresh token or Authorization header reaches a print call (grep over print arguments finds none). | no action: stdout and stderr are the command-line contract |
| LOG-05 | `tools` (77 `print`, 7 stderr writes) and `scripts` | Low | Operator-run local tools; output is not shipped. Some print exception text of local connection failures. | no action: operator-run local tool; output not shipped to third parties |
| LOG-06 | Exception text in operator log entries | Medium | Exception text or a traceback is placed in a log field without the shared sanitising helper at 7 service sites; other sites log the class name only or use the helper. | issue #293 |
| LOG-07 | User identifiers in operator log entries | Medium | No log field carries an identifier of the acting user or principal: the 7 service sites that did, one on every successful authentication, log none, and the Audit component records the actor. The coding standard states the rule as current behaviour, and the canary test asserts it on the accepted-token, MCP tool-call and invite paths. | fixed (issue #294) |
| LOG-08 | Outcome vocabulary | Medium | 20 distinct outcome labels plus 25 passthrough and 4 computed values across 92 calls; success is spelled `succeeded` (15) and `success` (5), failure `failed` (16), `failure` (2) and `error` (2). Only `failed` is mirrored to stderr. | issue #295 |
| LOG-09 | Expected conditions logged as `failed` | Low | At least one expected user condition (an invalid or expired link) uses the ERROR-equivalent outcome. | issue #295 |
| LOG-10 | Failure-cause field names | Low | The cause is spelled `reason` (9), `error` (5), `exception_type` (4), `detail` (4), `error_kind` (2), `error_type` (2). | issue #295 |
| LOG-11 | Server request log | Medium | The server's own request log is unstructured text with no correlation identifier and bypasses the facade. | issue #296 |
| LOG-12 | Audit-row coverage | Medium | Rows are written for graph cleanup, runtime configuration, ingestion runs, near-miss resolution, export, restore, access roles, policy lifecycle and invitations, and, since this change, for role-gate denials (`access.gate`), passkey enrolment (`passkey.enroll`) and `cypher` use (`query.cypher`). Authentication outcomes, denials of callers with no verified identity, rejected passkey steps and read tools other than `cypher` have no audit row. Columns present: actor subject and issuer, action, resource, outcome, details, `occurred_at`. | fixed in this change for role-gate denials, passkey enrolment and `cypher` use; still open: failed authentication and unauthenticated denials (the identity provider's event log is the record; there is no verified actor), passkey rejections, other read tools, audit retention (issue #297) |
| LOG-13 | Logging convention not documented | Low | `logging/models.py` was the only field definition; the Python coding standard had no logging convention. | fixed in this change (section "Logging Conventions" in `docs/coding-standards/level2-python-instructions.md`) |
| LOG-14 | No regression test for log leakage | Medium | No test drove representative flows with fake secrets and searched all sinks. | fixed in this change (new canary test) |
| LOG-15 | Log file in the container filesystem | Low | `PS_LOGGING_DIR` is set in the image; a grep of the chart templates and values finds no log volume, rotation or level setting, and `emitter.py` appends without rotation. | no action: retention and shipping belong to #259 (cross-reference only) |
| LOG-16 | Middleware and request/response logging | Low | No middleware logs bodies or headers; the middleware classes in `main.py` and `auth/middleware.py` do not log request content. Error handlers log the exception type and a scrubbed detail. | no action: no leakage path |
| LOG-17 | LLM prompts and completions | Low | The canary test asserts that a prompt and a reply stay out of every sink on a success and a provider-error path. No library verbosity switch exists in the source. | no action: covered by the canary test |

## Canary test

`ps-service/tests/logging/test_log_leakage_canary.py` holds ten tests. Each uses a per-test `tmp_path` log file with the real `configure()` and failure mirroring, then searches the log file, captured stderr and stdout, and all `logging` records at DEBUG for the fake `canary-` values.

| Flow | Canaries asserted absent |
| --- | --- |
| Rejected bearer token (expired token with a canary subject, and a garbage canary token) | token, user |
| Accepted bearer token with a canary subject | raw token, `Bearer ` prefix, user |
| Unhandled request error with key=value, bearer and URL shapes in the message | token |
| Request rejected by body validation with canary policy text | policy |
| LLM completion success | prompt, reply text |
| LLM completion failing with the prompt in the provider error | prompt, token |
| Denied request with no verified principal and canary values in the body | policy, token, user |
| `cypher` call whose query text and database error carry canary values | policy, token |
| MCP tool calls by a canary principal (`cypher`, gate refusal) | user |
| Invite flow by a canary caller for a canary email | user, email |

Free-text values inside exception text are outside the shapes the sanitising helper covers and are tracked in #293, so no test papers over them.

Mutation proof: the service package was copied to a scratch directory outside the repository, the unhandled-error log field was changed to carry the raw message, and the test run with that copy first on the import path failed with "canary values reached a log sink" (1 failed, 5 passed). The repository tree was not touched.

## Correlation

Records are JSON lines in one format. `run_id` is bound by `bind_run_context` on request paths that depend on `provide_run_id` and in pipeline runs; startup and background entries carry none. The container architecture document's statement about where `run_id` is bound differs from the code; that drift belongs to #254.

## False positives

| Candidate | Why it is not a finding |
| --- | --- |
| `main.py` startup failure entry | Logs class names and a fixed reason only. |
| `domain_mapper/extraction.py` failure entries | Log `error_kind` only. |
| `audit/emit.py` failure entry | Logs the exception class name only. |
| `ps-cli` device-flow prompt | Prints the verification URI and user code intentionally; not a token. |

## Superseded and removed

No code, test, config or document became obsolete, so nothing was removed or renamed. Grep proof that no stale reference exists to a replaced convention: `grep -rn "Logging Conventions" . --exclude-dir=.orchestrator --exclude-dir=node_modules --exclude-dir=spikes --exclude-dir=.venv --exclude-dir=docs/audits` finds only the new heading in the Python coding standard, and the change set is two new files plus one added section.

## Sibling-issue review

Open #246 sub-issues were enumerated through the sub-issues API and every issue filed by the earlier audits (#263 to #292) was read. Filed by this audit: #293, #294, #295, #296 and #297, all native sub-issues of #246.

| Issue | Outcome |
| --- | --- |
| #251 conformance | commented: include the new Logging Conventions section; outcome vocabulary and user-identifier rule are target state |
| #254 documentation | commented: run-identifier binding drift in the architecture document; new standards section |
| #259 observability | commented: shipping, retention and rotation stay there; decide before #296 |
| #260 guardrails | commented: canary test is the existing guard; vocabulary check scheduled after #295 |
| #297 audit rows | commented: threat-coverage map stays with the security audit; no deliverable overlap |
| #255, #257 | no change needed: no log-related scope; #257 re-check only if #296 adds chart values |
| #252 (closed) | no change needed: overlap is limited to audit-row gaps, owned by #297 |
| #263 to #292 except those above | no change needed: no logging, log-format or audit-row content |

## Appendix: tool scripts

The three scripts live outside the repository (`.orchestrator/tracker/i253/tools/`); they are plain `ast` walkers.

- `callsites.py` walks `*.py` under the given roots and prints every call whose dotted name is `print`, a logger method, `emit_log_entry`, `traceback.print_exc`, `sys.stderr.write`, `sys.stdout.write` or `warnings.warn`.
- `outcomes.py` resolves module-level string constants and counts `outcome=` values and `extra` dictionary keys of `emit_log_entry` calls.
- `exc_sinks.py` lists log and print arguments that reference an exception variable bound by `except ... as`.

## Enforcement

Enforced rules fail automatically, in the pre-commit hook on the files they watch and in the CI `trunk-worthy` wave over the whole tree. Review-only rules rely on review. Check ids are the hook and CI ids listed in [CONTRIBUTING.md](../../CONTRIBUTING.md#automated-guardrails).

| Rule | Status | Check id or reason |
| --- | --- | --- |
| No `print` in service code | enforced | `ruff-check` (`T201`), with the allow-list pinned by `pytest` |
| Secrets do not appear in log entries | enforced | `pytest` (log-leakage canary test) |
| Logging code passes lint and strict typing | enforced | `ruff-check`, `basedpyright` |
| One outcome vocabulary and expected conditions not logged as failures | review-only | tracked in #295; a closed-set check in #327 |
| Exception text in log entries | review-only | tracked in #293 |
| User identifiers do not appear in log entries | enforced | `pytest` (log-leakage canary test) |
| Server request logs in the structured format | review-only | tracked in #296 |
| Audit records for role-gate denials, passkey enrolment and `cypher` use | enforced | `pytest` (gate-declaration structural test: every registered tool and route declares a role gate or is marked authenticated-only; each declared gate is covered by the denial matrix) |
| Audit records for other security-relevant actions (failed authentication, unauthenticated denials, passkey rejections, other read tools, audit retention) | review-only | still open under LOG-12 |
