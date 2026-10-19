#!/usr/bin/env bash
# Backup and restore verification harness for PS Postgres (issue #216).
#
#   scripts/verify-backup-restore.sh backup [--out-dir DIR] [--allow-context CTX]
#   scripts/verify-backup-restore.sh counts [--allow-context CTX]
#   scripts/verify-backup-restore.sh retain-secrets [--chart REF] [--chart-version V] [--interval S]
#                                    [--timeout S] [--allow-context CTX]
#   scripts/verify-backup-restore.sh restore --artifact FILE [--chart REF] [--chart-version V]
#                                    [--yes] [--interval S] [--timeout S] [--allow-context CTX]
#   scripts/verify-backup-restore.sh verify-provision [--allow-context CTX]
#   scripts/verify-backup-restore.sh wait-ready [--interval S] [--timeout S] [--allow-context CTX]
#   scripts/verify-backup-restore.sh seed [--instrument ID ...] [--allow-context CTX]
#   scripts/verify-backup-restore.sh e2e [--out-dir DIR] [--instrument ID ...] [--chart REF]
#                                    [--chart-version V] [--yes] [--skip-restore]
#                                    [--interval S] [--timeout S] [--allow-context CTX]
#
# backup  Runs one `pg_basebackup` over the local socket inside the ps-postgres pod, as the admin
#         role, streams the tar to DIR/ps-postgres-<UTC timestamp>.tar (default DIR: the current
#         directory) and verifies it: the exec exits 0, the file is non-empty, `tar tf` reads it.
#         Prints the artifact path on stdout. A failed backup leaves no artifact behind.
#         The artifact holds the whole data directory, including the stored role password
#         hashes and the audit trail: keep it out of the repository and encrypt it at rest.
# counts  Prints the row counts of `audit_log.audit_events`, `access_role_assignments` and
#         `graph_log.entries`, the figures compared before and after a backup or restore.
# retain-secrets
#         Prepares a running release for `restore` and `e2e`: copies the three generated
#         credentials Secrets (`<release>-ps-postgres-{admin,state,signing}-credentials`) to
#         `ps-{admin,state,signing}-retained`, stops ps-service and PS Postgres (a single-node disk
#         cannot be attached by the replacement pod while the old one holds it), then runs
#         `helm upgrade --reset-then-reuse-values` with the three `existingSecret` values and
#         `--wait --wait-for-jobs`. It does nothing when the release already has the three values
#         set and the Secrets exist. The copies live in the same cluster as the originals, which
#         is enough for a kind cluster; on a real cluster keep operator-managed copies outside it
#         (operations guide, Restore). The passwords do not change, so earlier backups stay valid.
# restore Replaces the PS Postgres data directory with a backup artifact and empties FalkorDB, then
#         brings the release back up in this order: stop ps-service, FalkorDB and PS Postgres;
#         extract the artifact into `pgdata` and empty the FalkorDB volume from one helper pod;
#         start PS Postgres and FalkorDB; `helm upgrade --reset-then-reuse-values --wait
#         --wait-for-jobs` (runs the new provisioning Job); verify the Job and the Postgres logs
#         (verify-provision); start ps-service and wait for `/ready` (wait-ready); log the counts.
#         It refuses unless the three credentials Secrets are retained (psPostgres.admin,
#         state and signing `existingSecret` set on the release and the Secrets present), the
#         artifact is a readable tar, and the operator types "yes" (or passes --yes).
#         The chart defaults to the published OCI chart (as scripts/ps-upgrade.sh); --chart takes
#         a chart path or reference and --chart-version pins its version.
# verify-provision
#         Checks that the current release revision's `ps-state-provision-r<N>` Job is Complete and
#         that the PS Postgres logs hold no `ps-postgres-init: created roles` line (the init
#         script runs only on an empty data directory, so its line means the restore did not
#         keep the restored data).
# wait-ready
#         Polls `/ready` inside the ps-service pod until it answers 200 with status ready; logs
#         each 503 on the way. PS_READY_TOKEN (optional, an access token read from the
#         environment and handed to the pod on stdin) is sent as a Bearer header and then also
#         requires an empty `gated_graphs`; without it the reply is status only, the gated check
#         is skipped and `gated_graphs=unchecked` is logged. restore and e2e wait the same way
#         (e2e still proves every graph by comparing digests).
# seed    Fills a running deployment with content to verify: restores curated instruments (default
#         ENGPRAC-1.0 and CRA-1.0, the first imports draft Policy/Standard/Control nodes) through
#         the restore path and grants access roles through the access-role store, by piping
#         tools/backup-restore-verify/seed_content.py and seed_grants.py into the ps-service pod.
#         The ps-service image must write graphs through the Graph Write Gateway.
# e2e     The whole check on one cluster: seed; snapshot the digest of every logged graph and the
#         row counts; take one backup; wipe the PS Postgres and FalkorDB volumes; restore
#         (the restore sequence above, minus the stop, which the wipe already did); snapshot and
#         count again; compare. Exits 0 only if every graph digest and every count is equal.
#         The credentials Secrets are retained (see restore) and the operator types "yes".
#         --skip-restore leaves the wiped volumes unrestored: the comparison must then fail.
#
# Refuses to run unless the active kubectl context is kind-* or named by --allow-context.
# Semantic log lines (event=<name> key=value ...) go to stderr; stdout carries only results.
#
# The digest tool runs inside the ps-service pod with the read-only FalkorDB handle (tools/
# backup-restore-verify/graph_digests.py); the compare of two snapshots runs on this machine
# through `uv run`.
#
# Exit codes: 1 usage/preflight/business failure, 0 success.
set -euo pipefail

if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
  readonly COLOR_RED=$'\033[31m'
  readonly COLOR_RESET=$'\033[0m'
else
  readonly COLOR_RED=""
  readonly COLOR_RESET=""
fi

print_error() {
  local format="$1"
  shift
  # shellcheck disable=SC2059 # the caller supplies the printf format; the colour codes wrap it
  printf "${COLOR_RED}${format}${COLOR_RESET}" "$@" >&2
}

log_event() {
  printf 'event=%s' "$1" >&2
  shift
  printf ' %s' "$@" >&2
  printf '\n' >&2
}

readonly EXIT_FAILURE=1
readonly RELEASE_NAME="policy-system"
readonly POSTGRES_DEPLOYMENT="deploy/${RELEASE_NAME}-ps-postgres"
readonly ADMIN_USER="${PS_POSTGRES_ADMIN_USER:-postgres_admin}"
readonly STATE_DATABASE="ps_state"
readonly SERVICE_DEPLOYMENT="deploy/${RELEASE_NAME}-ps-service"
readonly FALKORDB_DEPLOYMENT="deploy/${RELEASE_NAME}-falkordb"
readonly POSTGRES_PVC="${RELEASE_NAME}-ps-postgres-data"
readonly FALKORDB_PVC="${RELEASE_NAME}-falkordb-data"
readonly HELPER_POD="${RELEASE_NAME}-restore-helper"
readonly DEFAULT_CHART="oci://ghcr.io/mindovermachine-dev/charts/policy-system"
# shellcheck disable=SC2016 # the command substitutions run in the helper pod, not here
readonly WIPE_COMMAND='rm -rf /pg/* /pg/.[!.]* /pg/..?* /fdb/* /fdb/.[!.]* /fdb/..?*; echo "pg_entries=$(ls -A /pg | wc -l) fdb_entries=$(ls -A /fdb | wc -l)"'
readonly INIT_LINE="ps-postgres-init: created roles"
readonly STOP_TIMEOUT="180s"
readonly START_TIMEOUT="300s"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly SCRIPT_DIR REPO_ROOT
readonly TOOLS_DIR="${REPO_ROOT}/tools/backup-restore-verify"
readonly USAGE="Usage: scripts/verify-backup-restore.sh {backup [--out-dir DIR]|counts|retain-secrets [--chart REF] [--chart-version V]|restore --artifact FILE [--chart REF] [--chart-version V] [--yes]|verify-provision|wait-ready [--interval S] [--timeout S]|seed [--instrument ID ...]|e2e [--out-dir DIR] [--instrument ID ...] [--yes] [--skip-restore]} [--allow-context CTX]"

KUBE_CONTEXT=""
ALLOWED_CONTEXT=""
ARTIFACT=""
CHART="$DEFAULT_CHART"
CHART_VERSION=""
ASSUME_YES="false"
POLL_INTERVAL="2"
READY_TIMEOUT="600"
SKIP_RESTORE="false"
OUT_DIR="."
INSTRUMENTS=()

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    print_error 'This script requires "%s" on PATH.\n' "$1"
    exit "$EXIT_FAILURE"
  }
}

require_allowed_context() {
  KUBE_CONTEXT="$(kubectl config current-context)"
  if [[ "$KUBE_CONTEXT" != kind-* && "$KUBE_CONTEXT" != "$ALLOWED_CONTEXT" ]]; then
    print_error 'Refusing to run: kubectl context "%s" is not a kind-* cluster (use --allow-context)\n' \
      "$KUBE_CONTEXT"
    exit "$EXIT_FAILURE"
  fi
}

count_rows() {
  local table="$1"
  kubectl exec "$POSTGRES_DEPLOYMENT" -- \
    psql -U "$ADMIN_USER" -d "$STATE_DATABASE" -tA -c "SELECT count(*) FROM ${table}"
}

counts_line() {
  printf 'audit_events=%s access_role_assignments=%s graph_log_entries=%s' \
    "$(count_rows audit_log.audit_events)" \
    "$(count_rows access_role_assignments)" \
    "$(count_rows graph_log.entries)"
}

log_counts() {
  local phase="$1" line
  line="$(counts_line)"
  # shellcheck disable=SC2086 # the line is space-separated key=value words by construction
  log_event counts "phase=${phase}" $line
}

run_counts() {
  require_command kubectl
  require_allowed_context
  printf '%s\n' "$(counts_line)"
}

fail_backup() {
  local artifact="$1" reason="$2"
  rm -f -- "$artifact"
  log_event backup_failed "context=${KUBE_CONTEXT}" "reason=${reason}"
  print_error 'Backup failed: %s\n' "$reason"
  exit "$EXIT_FAILURE"
}

run_backup() {
  local out_dir="$1" artifact
  require_command kubectl
  require_command tar
  require_allowed_context
  mkdir -p -- "$out_dir"
  artifact="${out_dir}/ps-postgres-$(date -u +%Y%m%dT%H%M%SZ).tar"

  log_event backup_start "context=${KUBE_CONTEXT}" "artifact=${artifact}"
  log_counts pre
  if ! kubectl exec "$POSTGRES_DEPLOYMENT" -- \
    pg_basebackup -U "$ADMIN_USER" -D - -Ft -X fetch -c fast >"$artifact"; then
    fail_backup "$artifact" "pg_basebackup exited non-zero"
  fi
  [[ -s "$artifact" ]] || fail_backup "$artifact" "artifact is empty"
  tar tf "$artifact" >/dev/null 2>&1 || fail_backup "$artifact" "artifact is not a readable tar"
  log_counts post
  log_event backup_verified "artifact=${artifact}" "bytes=$(wc -c <"$artifact" | tr -d ' ')"
  printf '%s\n' "$artifact"
}

release_revision() {
  helm status "$RELEASE_NAME" -o json | jq -r '.version'
}

provision_job_name() {
  printf '%s-ps-state-provision-r%s' "$RELEASE_NAME" "$1"
}

fail_provision() {
  local reason="$1"
  log_event provision_failed "context=${KUBE_CONTEXT}" "reason=${reason}"
  print_error 'Provisioning check failed: %s\n' "$reason"
  exit "$EXIT_FAILURE"
}

verify_provision() {
  local revision job complete postgres_logs
  revision="$(release_revision)"
  job="$(provision_job_name "$revision")"
  complete="$(kubectl get job "$job" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}')" ||
    fail_provision "Job ${job} was not found"
  [[ "$complete" == "True" ]] || fail_provision "Job ${job} is not Complete"
  postgres_logs="$(kubectl logs "$POSTGRES_DEPLOYMENT")" ||
    fail_provision "the PS Postgres logs could not be read"
  if [[ "$postgres_logs" == *"$INIT_LINE"* ]]; then
    fail_provision "the PS Postgres logs hold '${INIT_LINE}': the init script ran on an empty data directory"
  fi
  log_event provision_verified "job=${job}" "revision=${revision}"
}

run_verify_provision() {
  require_command kubectl
  require_command helm
  require_command jq
  require_allowed_context
  verify_provision
}

# ready_probe prints "<http code> <body>" from /ready inside the ps-service pod, or nothing while
# the pod cannot be reached (starting). Exec is used because a pod that is not Ready is not in the
# Service endpoints, and that is exactly the state that is observed.
# PS_READY_TOKEN, when set, reaches the pod on stdin (never as an argument or in a log line) and is
# sent as a Bearer header, which is what makes PS Service include `gated_graphs` in the reply.
ready_probe() {
  printf '%s' "${PS_READY_TOKEN:-}" | kubectl exec -i "$SERVICE_DEPLOYMENT" -- python -c '
import sys, urllib.request, urllib.error
token = sys.stdin.read().strip()
headers = {"Authorization": "Bearer " + token} if token else {}
request = urllib.request.Request("http://127.0.0.1:8000/ready", headers=headers)
try:
    response = urllib.request.urlopen(request, timeout=5)
    print(response.status, response.read().decode())
except urllib.error.HTTPError as error:
    print(error.code, error.read().decode())
' 2>/dev/null || true
}

# ready_body_acceptable succeeds for a 200 body that reports ready. With PS_READY_TOKEN set the body
# must also show an empty `gated_graphs`; without it the reply is status only and that check is skipped.
ready_body_acceptable() {
  local body="$1"
  if [[ -n "${PS_READY_TOKEN:-}" ]]; then
    printf '%s' "$body" | jq -e '.status == "ready" and .gated_graphs == []' >/dev/null 2>&1
  else
    printf '%s' "$body" | jq -e '.status == "ready"' >/dev/null 2>&1
  fi
}

wait_ready() {
  local deadline probe code body
  deadline=$((SECONDS + READY_TIMEOUT))
  while ((SECONDS < deadline)); do
    probe="$(ready_probe | tail -n 1)"
    code="${probe%% *}"
    body="${probe#* }"
    if [[ -z "$probe" ]]; then
      log_event ready_poll http=none "body=pod-not-reachable"
    elif [[ "$code" == "200" ]] && ready_body_acceptable "$body"; then
      if [[ -n "${PS_READY_TOKEN:-}" ]]; then
        log_event ready http=200 "body=${body}"
      else
        log_event ready http=200 "body=${body}" gated_graphs=unchecked
      fi
      return 0
    else
      log_event ready_poll "http=${code}" "body=${body}"
    fi
    sleep "$POLL_INTERVAL"
  done
  log_event ready_timeout "seconds=${READY_TIMEOUT}"
  print_error '/ready did not answer 200 and ready (with empty gated_graphs when PS_READY_TOKEN is set) within %s seconds\n' "$READY_TIMEOUT"
  return "$EXIT_FAILURE"
}

run_wait_ready() {
  require_command kubectl
  require_command jq
  require_allowed_context
  wait_ready || exit "$EXIT_FAILURE"
}

fail_restore() {
  local step="$1" reason="$2"
  kubectl delete pod "$HELPER_POD" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  log_event restore_failed "context=${KUBE_CONTEXT}" "step=${step}" "reason=${reason}"
  print_error 'Restore failed at %s: %s\nps-service stays stopped; fix the cause and run restore again.\n' \
    "$step" "$reason"
  exit "$EXIT_FAILURE"
}

require_retained_secrets() {
  local values role secret
  values="$(helm get values "$RELEASE_NAME" -o json)" || {
    print_error 'Could not read the values of release "%s"\n' "$RELEASE_NAME"
    exit "$EXIT_FAILURE"
  }
  for role in admin state signing; do
    secret="$(printf '%s' "$values" | jq -r --arg role "$role" '.psPostgres[$role].existingSecret // ""')"
    if [[ -z "$secret" ]]; then
      print_error 'Refusing to restore: psPostgres.%s.existingSecret is not set on the release (run "scripts/verify-backup-restore.sh retain-secrets" first)\n' "$role"
      exit "$EXIT_FAILURE"
    fi
    if ! kubectl get secret "$secret" -o name >/dev/null 2>&1; then
      print_error 'Refusing to restore: Secret "%s" (psPostgres.%s.existingSecret) does not exist\n' \
        "$secret" "$role"
      exit "$EXIT_FAILURE"
    fi
  done
}

# secrets_are_retained: true when the release has all three existingSecret values set and each
# Secret exists. Silent, unlike require_retained_secrets, which refuses with a message.
secrets_are_retained() {
  local values role secret
  values="$(helm get values "$RELEASE_NAME" -o json)" || return 1
  for role in admin state signing; do
    secret="$(printf '%s' "$values" | jq -r --arg role "$role" '.psPostgres[$role].existingSecret // ""')"
    [[ -n "$secret" ]] || return 1
    kubectl get secret "$secret" -o name >/dev/null 2>&1 || return 1
  done
}

fail_retain() {
  log_event retain_secrets_failed "context=${KUBE_CONTEXT}" "step=$1" "reason=$2"
  print_error 'retain-secrets failed at %s: %s\n' "$1" "$2"
  exit "$EXIT_FAILURE"
}

run_retain_secrets() {
  require_command kubectl
  require_command helm
  require_command jq
  require_allowed_context
  if secrets_are_retained; then
    log_event retain_secrets_skipped "context=${KUBE_CONTEXT}" "reason=already_retained"
    return 0
  fi

  local role source
  log_event retain_secrets_start "context=${KUBE_CONTEXT}"
  for role in admin state signing; do
    source="${RELEASE_NAME}-ps-postgres-${role}-credentials"
    kubectl get secret "$source" -o json |
      jq --arg name "ps-${role}-retained" \
        '{apiVersion, kind, type, metadata: {name: $name}, data}' |
      kubectl apply -f - >/dev/null || fail_retain copy "could not copy Secret ${source}"
  done

  kubectl scale "$SERVICE_DEPLOYMENT" "$POSTGRES_DEPLOYMENT" --replicas=0 ||
    fail_retain stop "could not scale ps-service and ps-postgres to 0"
  kubectl wait --for=delete pod -l "app.kubernetes.io/component=ps-postgres" \
    "--timeout=${STOP_TIMEOUT}" || fail_retain stop "pods of ps-postgres did not stop"

  local -a chart_args=("$CHART")
  [[ -n "$CHART_VERSION" ]] && chart_args+=(--version "$CHART_VERSION")
  helm upgrade "$RELEASE_NAME" "${chart_args[@]}" --reset-then-reuse-values \
    --set psPostgres.admin.existingSecret=ps-admin-retained \
    --set psPostgres.state.existingSecret=ps-state-retained \
    --set psPostgres.signing.existingSecret=ps-signing-retained \
    --wait --wait-for-jobs || fail_retain upgrade "helm upgrade --wait --wait-for-jobs failed"
  log_event retain_secrets_complete "context=${KUBE_CONTEXT}"
}

confirm_restore() {
  local answer=""
  [[ "$ASSUME_YES" == "true" ]] && return 0
  printf 'This replaces ALL PS Postgres data and empties FalkorDB in context "%s".\n' "$KUBE_CONTEXT" >&2
  printf 'Type "yes" to continue: ' >&2
  read -r answer || true
  if [[ "$answer" != "yes" ]]; then
    print_error 'Aborted: confirmation was not "yes"\n'
    exit "$EXIT_FAILURE"
  fi
}

stop_workloads() {
  local deployment component
  for deployment in "$SERVICE_DEPLOYMENT" "$FALKORDB_DEPLOYMENT" "$POSTGRES_DEPLOYMENT"; do
    kubectl scale "$deployment" --replicas=0 || fail_restore stop "could not scale ${deployment} to 0"
  done
  for component in ps-service falkordb ps-postgres; do
    kubectl wait --for=delete pod -l "app.kubernetes.io/component=${component}" \
      "--timeout=${STOP_TIMEOUT}" || fail_restore stop "pods of ${component} did not stop"
  done
}

apply_helper_pod() {
  local image="$1"
  kubectl apply -f - <<MANIFEST || fail_restore helper "could not create the helper pod"
apiVersion: v1
kind: Pod
metadata:
  name: ${HELPER_POD}
spec:
  restartPolicy: Never
  containers:
    - name: helper
      image: ${image}
      command: ["sleep", "3600"]
      securityContext:
        runAsUser: 0
      volumeMounts:
        - name: pg
          mountPath: /pg
        - name: fdb
          mountPath: /fdb
  volumes:
    - name: pg
      persistentVolumeClaim:
        claimName: ${POSTGRES_PVC}
    - name: fdb
      persistentVolumeClaim:
        claimName: ${FALKORDB_PVC}
MANIFEST
}

replace_data() {
  local image
  image="$(kubectl get "$POSTGRES_DEPLOYMENT" -o jsonpath='{.spec.template.spec.containers[0].image}')" ||
    fail_restore helper "could not read the PS Postgres image"
  apply_helper_pod "$image"
  kubectl wait --for=condition=Ready "pod/${HELPER_POD}" "--timeout=${STOP_TIMEOUT}" ||
    fail_restore helper "the helper pod did not become Ready"
  kubectl exec -i "$HELPER_POD" -- \
    sh -c 'set -e; rm -rf /pg/pgdata; mkdir -p /pg/pgdata; chmod 700 /pg/pgdata; tar -xf - -C /pg/pgdata' \
    <"$ARTIFACT" || fail_restore extract "the artifact could not be extracted into pgdata"
  kubectl exec "$HELPER_POD" -- sh -c 'rm -rf /fdb/* /fdb/.[!.]* /fdb/..?*' ||
    fail_restore falkordb "FalkorDB's volume could not be emptied"
  kubectl delete pod "$HELPER_POD" --wait=true || fail_restore helper "the helper pod was not deleted"
  log_event data_replaced "artifact=${ARTIFACT}"
}

start_data_stores() {
  kubectl scale "$POSTGRES_DEPLOYMENT" --replicas=1 || fail_restore start "could not scale PS Postgres to 1"
  kubectl rollout status "$POSTGRES_DEPLOYMENT" "--timeout=${START_TIMEOUT}" ||
    fail_restore start "PS Postgres did not become Ready on the restored data"
  kubectl scale "$FALKORDB_DEPLOYMENT" --replicas=1 || fail_restore start "could not scale FalkorDB to 1"
  kubectl rollout status "$FALKORDB_DEPLOYMENT" "--timeout=${START_TIMEOUT}" ||
    fail_restore start "FalkorDB did not become Ready"
}

run_provisioning_upgrade() {
  local -a chart_args=("$CHART")
  [[ -n "$CHART_VERSION" ]] && chart_args+=(--version "$CHART_VERSION")
  helm upgrade "$RELEASE_NAME" "${chart_args[@]}" \
    --reset-then-reuse-values --wait --wait-for-jobs ||
    fail_restore provision "helm upgrade --wait --wait-for-jobs failed"
}

# restore_sequence replaces the data directory and empties FalkorDB, then brings the release up in
# the documented order. The workloads must already be stopped.
restore_sequence() {
  replace_data
  start_data_stores
  run_provisioning_upgrade
  (verify_provision) || fail_restore provision "the provisioning check failed"
  kubectl scale "$SERVICE_DEPLOYMENT" --replicas=1 || fail_restore start "could not scale ps-service to 1"
  wait_ready || fail_restore ready "/ready did not reach 200 and ready"
}

run_restore() {
  [[ -n "$ARTIFACT" ]] || {
    print_error 'restore needs --artifact FILE\n%s\n' "$USAGE"
    exit "$EXIT_FAILURE"
  }
  require_command kubectl
  require_command helm
  require_command jq
  require_command tar
  require_allowed_context
  if [[ ! -s "$ARTIFACT" ]] || ! tar tf "$ARTIFACT" >/dev/null 2>&1; then
    print_error 'Refusing to restore: "%s" is missing, empty or not a readable tar\n' "$ARTIFACT"
    exit "$EXIT_FAILURE"
  fi
  require_retained_secrets
  confirm_restore

  log_event restore_start "context=${KUBE_CONTEXT}" "artifact=${ARTIFACT}"
  stop_workloads
  restore_sequence
  log_counts restored
  log_event restore_complete "context=${KUBE_CONTEXT}"
}

# pipe_tool runs one tools/backup-restore-verify script inside the ps-service pod, with its own
# arguments, reading the script from stdin; the pod carries the service's configuration and code.
pipe_tool() {
  local tool="$1"
  shift
  kubectl exec -i "$SERVICE_DEPLOYMENT" -- python - "$@" <"${TOOLS_DIR}/${tool}"
}

seed_content() {
  local -a args=()
  local instrument
  for instrument in ${INSTRUMENTS[@]+"${INSTRUMENTS[@]}"}; do
    args+=(--instrument "$instrument")
  done
  log_event seed_start "context=${KUBE_CONTEXT}"
  pipe_tool seed_content.py ${args[@]+"${args[@]}"} || {
    print_error 'Seeding the curated content failed\n'
    exit "$EXIT_FAILURE"
  }
  pipe_tool seed_grants.py || {
    print_error 'Seeding the access-role grants failed\n'
    exit "$EXIT_FAILURE"
  }
  log_counts seeded
}

run_seed() {
  require_command kubectl
  require_allowed_context
  seed_content
}

logged_graphs() {
  kubectl exec "$POSTGRES_DEPLOYMENT" -- \
    psql -U "$ADMIN_USER" -d "$STATE_DATABASE" -tA -c "SELECT DISTINCT graph FROM graph_log.entries ORDER BY 1"
}

# digest_snapshot writes the canonical digest of each named graph to FILE (JSON).
digest_snapshot() {
  local file="$1"
  shift
  local -a args=()
  local graph
  for graph in "$@"; do
    args+=(--graph "$graph")
  done
  pipe_tool graph_digests.py snapshot "${args[@]}" --out /dev/stdout >"$file" ||
    fail_e2e "the graph digests could not be read"
}

compare_digests() {
  uv run --project "$REPO_ROOT" python "${TOOLS_DIR}/graph_digests.py" compare "$1" "$2"
}

fail_e2e() {
  log_event e2e_failed "context=${KUBE_CONTEXT}" "reason=$1"
  print_error 'e2e failed: %s\n' "$1"
  exit "$EXIT_FAILURE"
}

# wipe_volumes stops the workloads and empties both volumes from one helper pod.
wipe_volumes() {
  local image
  stop_workloads
  image="$(kubectl get "$POSTGRES_DEPLOYMENT" -o jsonpath='{.spec.template.spec.containers[0].image}')" ||
    fail_restore helper "could not read the PS Postgres image"
  apply_helper_pod "$image"
  kubectl wait --for=condition=Ready "pod/${HELPER_POD}" "--timeout=${STOP_TIMEOUT}" ||
    fail_restore helper "the helper pod did not become Ready"
  kubectl exec "$HELPER_POD" -- \
    sh -c "$WIPE_COMMAND" ||
    fail_restore wipe "the volumes could not be emptied"
  kubectl delete pod "$HELPER_POD" --wait=true || fail_restore helper "the helper pod was not deleted"
  log_event volumes_wiped
}

run_e2e() {
  local artifact before_digests after_digests before_counts after_counts
  local -a graphs=()
  require_command kubectl
  require_command helm
  require_command jq
  require_command tar
  require_command uv
  require_allowed_context
  require_retained_secrets
  confirm_restore
  mkdir -p -- "$OUT_DIR"
  before_digests="${OUT_DIR}/digests-before.json"
  after_digests="${OUT_DIR}/digests-after.json"

  seed_content
  local graph
  while IFS= read -r graph; do
    [[ -n "$graph" ]] && graphs+=("$graph")
  done < <(logged_graphs)
  [[ ${#graphs[@]} -gt 0 ]] || fail_e2e "graph_log holds no graph, so there is nothing to compare"
  log_event e2e_graphs "graphs=${graphs[*]}"
  digest_snapshot "$before_digests" "${graphs[@]}"
  before_counts="$(counts_line)"
  log_event counts phase=before "$before_counts"

  artifact="$(run_backup "$OUT_DIR")" || fail_e2e "the backup failed"
  ARTIFACT="$artifact"
  wipe_volumes
  if [[ "$SKIP_RESTORE" == "true" ]]; then
    log_event restore_skipped
    start_data_stores
    run_provisioning_upgrade
    kubectl scale "$SERVICE_DEPLOYMENT" --replicas=1 || fail_e2e "could not scale ps-service to 1"
    wait_ready || fail_e2e "/ready did not reach 200 and ready"
  else
    restore_sequence
  fi

  digest_snapshot "$after_digests" "${graphs[@]}"
  after_counts="$(counts_line)"
  log_event counts phase=after "$after_counts"
  local failed="false"
  if ! compare_digests "$before_digests" "$after_digests"; then
    failed="true"
  fi
  if [[ "$before_counts" != "$after_counts" ]]; then
    print_error 'Counts differ: before %s, after %s\n' "$before_counts" "$after_counts"
    failed="true"
  fi
  [[ "$failed" == "false" ]] || fail_e2e "a graph digest or a count differs after the restore"
  log_event e2e_verified "artifact=${artifact}" "graphs=${#graphs[@]}"
}

main() {
  local subcommand="${1:-}"
  [[ $# -gt 0 ]] && shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --out-dir)
        OUT_DIR="${2:?--out-dir needs a value}"
        shift 2
        ;;
      --artifact)
        ARTIFACT="${2:?--artifact needs a value}"
        shift 2
        ;;
      --chart)
        CHART="${2:?--chart needs a value}"
        shift 2
        ;;
      --chart-version)
        CHART_VERSION="${2:?--chart-version needs a value}"
        shift 2
        ;;
      --yes)
        ASSUME_YES="true"
        shift
        ;;
      --skip-restore)
        SKIP_RESTORE="true"
        shift
        ;;
      --instrument)
        INSTRUMENTS+=("${2:?--instrument needs a value}")
        shift 2
        ;;
      --interval)
        POLL_INTERVAL="${2:?--interval needs a value}"
        shift 2
        ;;
      --timeout)
        READY_TIMEOUT="${2:?--timeout needs a value}"
        shift 2
        ;;
      --allow-context)
        ALLOWED_CONTEXT="${2:?--allow-context needs a value}"
        shift 2
        ;;
      *)
        print_error 'Unknown argument "%s"\n%s\n' "$1" "$USAGE"
        exit "$EXIT_FAILURE"
        ;;
    esac
  done

  case "$subcommand" in
    backup) run_backup "$OUT_DIR" ;;
    seed) run_seed ;;
    e2e) run_e2e ;;
    counts) run_counts ;;
    retain-secrets) run_retain_secrets ;;
    restore) run_restore ;;
    verify-provision) run_verify_provision ;;
    wait-ready) run_wait_ready ;;
    *)
      print_error '%s\n' "$USAGE"
      exit "$EXIT_FAILURE"
      ;;
  esac
}

main "$@"
