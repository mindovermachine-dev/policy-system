#!/usr/bin/env bash
# Proves a FalkorDB image change keeps the data on a real PersistentVolumeClaim.
#
#   tools/falkordb-upgrade-proof/run_on_kind.sh --from-tag <tag> [--context kind-policy-system]
#                                               [--out-dir DIR]
#
# Renders the chart's FalkorDB PVC, Deployment and Service (and nothing else) into a scratch
# namespace of a kind cluster and walks one volume through three images:
#
#   before    the previous image (--from-tag) seeds a known graph
#   after     the chart's own image tag (charts/policy-system/values.yaml) opens the same volume
#   rollback  the previous image opens the volume the new image has written
#
# Each leg waits for the Deployment to be Ready through the chart's own probes, then records per
# label node counts, per type edge counts, the graph list and the index definitions through
# `redis-cli GRAPH.RO_QUERY` into DIR/<leg>.txt. The three files must be identical: any
# difference exits 1 and prints the diff. The numeric ownership of the data directory is printed
# per leg and saved to DIR/<leg>-ownership.txt for inspection; it may legitimately change
# between images and is not compared. Between legs only the Deployment is deleted, never the PVC.
#
# FALKORDB_PROOF_EXTRA_NODE=1 adds one node between the before and after legs. The run must then
# exit 1, which shows the comparison can fail.
#
# Only a kind-* context is accepted, and the scratch namespace is deleted on exit.

set -euo pipefail

readonly USAGE="Usage: run_on_kind.sh --from-tag <tag> [--context kind-policy-system] [--out-dir DIR]"
readonly CHART_DIR="charts/policy-system"
readonly RELEASE="r"
readonly GRAPH="proof"
readonly DATA_DIR="/var/lib/falkordb/data"
readonly ROLLOUT_TIMEOUT="300s"

FROM_TAG=""
CONTEXT="kind-policy-system"
OUT_DIR=""

die() {
  printf 'error: %s\n' "$1" >&2
  exit "${2:-1}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-tag)
      [[ $# -ge 2 ]] || die "--from-tag needs a value. ${USAGE}" 2
      FROM_TAG="$2"
      shift 2
      ;;
    --context)
      [[ $# -ge 2 ]] || die "--context needs a value. ${USAGE}" 2
      CONTEXT="$2"
      shift 2
      ;;
    --out-dir)
      [[ $# -ge 2 ]] || die "--out-dir needs a value. ${USAGE}" 2
      OUT_DIR="$2"
      shift 2
      ;;
    -h | --help)
      printf '%s\n' "${USAGE}"
      exit 0
      ;;
    *)
      die "unknown argument: $1. ${USAGE}" 2
      ;;
  esac
done

[[ -n "${FROM_TAG}" ]] || die "--from-tag is required. ${USAGE}" 2
[[ "${CONTEXT}" == kind-* ]] || die "refusing context '${CONTEXT}': only kind-* clusters are accepted" 2

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly REPO_ROOT
cd "${REPO_ROOT}"

if [[ -z "${OUT_DIR}" ]]; then
  OUT_DIR="$(mktemp -d)"
fi
mkdir -p "${OUT_DIR}"

NAMESPACE="falkordb-proof-$$"
readonly NAMESPACE
DEPLOYMENT="${RELEASE}-policy-system-falkordb"
readonly DEPLOYMENT

kc() {
  kubectl --context "${CONTEXT}" --namespace "${NAMESPACE}" "$@"
}

cleanup() {
  kubectl --context "${CONTEXT}" delete namespace "${NAMESPACE}" --wait=false --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Render only the FalkorDB manifests; the dummy LLM and auth values satisfy the chart's
# whole-release validation and never reach a rendered FalkorDB object.
render() {
  local tag_args=()
  if [[ -n "${1:-}" ]]; then
    tag_args=(--set "falkordb.image.tag=$1")
  fi
  helm template "${RELEASE}" "${CHART_DIR}" --namespace "${NAMESPACE}" \
    --set falkordb.persistence.enabled=true \
    --set llm.azure.apiKey=unused --set llm.azure.apiBase=https://unused.invalid \
    --set psService.localTestBypass.enabled=true \
    ${tag_args[@]+"${tag_args[@]}"} \
    -s templates/falkordb-pvc.yaml -s templates/falkordb-deployment.yaml -s templates/falkordb-service.yaml
}

cypher() {
  kc exec "deploy/${DEPLOYMENT}" -- redis-cli "$@"
}

# Strips the per-call timing lines, which differ between otherwise identical reads.
ro() {
  cypher GRAPH.RO_QUERY "${GRAPH}" "$1" | grep -v -E '^(Cached execution|Query internal execution time|Graph version)'
}

deploy_leg() {
  local tag="$1"
  render "${tag}" | kc apply -f - >/dev/null
  kc rollout status "deployment/${DEPLOYMENT}" --timeout="${ROLLOUT_TIMEOUT}" >/dev/null
  printf 'image in use: %s\n' "$(kc get "deployment/${DEPLOYMENT}" -o jsonpath='{.spec.template.spec.containers[0].image}')"
}

stop_leg() {
  cypher SAVE >/dev/null
  kc delete "deployment/${DEPLOYMENT}" --wait=true >/dev/null
}

measure() {
  local leg="$1"
  {
    printf '== labels\n'
    ro 'MATCH (n) RETURN labels(n), count(n) ORDER BY labels(n)'
    printf '== edges\n'
    ro 'MATCH ()-[r]->() RETURN type(r), count(r) ORDER BY type(r)'
    printf '== graphs\n'
    cypher GRAPH.LIST
    printf '== indexes\n'
    ro 'CALL db.indexes() YIELD label, properties, types, entitytype, status RETURN label, properties, types, entitytype, status'
  } >"${OUT_DIR}/${leg}.txt"
  kc exec "deploy/${DEPLOYMENT}" -- ls -ln "${DATA_DIR}" |
    awk 'NR > 1 {print $1, $3, $4, $9}' >"${OUT_DIR}/${leg}-ownership.txt"
}

seed() {
  cypher GRAPH.QUERY "${GRAPH}" 'CREATE INDEX FOR (n:A) ON (n.id)' >/dev/null
  cypher GRAPH.QUERY "${GRAPH}" "UNWIND range(1,50) AS i CREATE (:A {id: i, name: 'a' + toString(i), v: [0.1, 0.2]})" >/dev/null
  cypher GRAPH.QUERY "${GRAPH}" 'UNWIND range(1,30) AS i CREATE (:B {id: i})' >/dev/null
  cypher GRAPH.QUERY "${GRAPH}" 'MATCH (a:A), (b:B) WHERE a.id = b.id CREATE (a)-[:R]->(b)' >/dev/null
  cypher SAVE >/dev/null
}

printf 'context: %s  namespace: %s  out: %s\n' "${CONTEXT}" "${NAMESPACE}" "${OUT_DIR}"
kubectl --context "${CONTEXT}" create namespace "${NAMESPACE}" >/dev/null

printf '\n-- leg before (previous image)\n'
deploy_leg "${FROM_TAG}"
seed
measure before
if [[ "${FALKORDB_PROOF_EXTRA_NODE:-0}" == "1" ]]; then
  cypher GRAPH.QUERY "${GRAPH}" 'CREATE (:Extra)' >/dev/null
fi
stop_leg

printf '\n-- leg after (chart image)\n'
deploy_leg ""
measure after
stop_leg

printf '\n-- leg rollback (previous image over the volume the new image wrote)\n'
deploy_leg "${FROM_TAG}"
measure rollback

printf '\n-- per label node counts\n'
printf '%-12s %-10s %-10s %-10s\n' label before after rollback
labels() {
  awk '/^== labels/ {on = 1; next} /^== / {on = 0} on' "${OUT_DIR}/$1.txt" | paste - - | grep -v '^labels(n)' | sort
}
join -t $'\t' -j 1 <(labels before) <(labels after) |
  join -t $'\t' -j 1 - <(labels rollback) |
  awk -F'\t' '{printf "%-12s %-10s %-10s %-10s\n", $1, $2, $3, $4}'

printf '\n-- data directory ownership (mode uid gid name)\n'
for leg in before after rollback; do
  printf '%s:\n' "${leg}"
  sed 's/^/  /' "${OUT_DIR}/${leg}-ownership.txt"
done

status=0
for pair in "before after" "before rollback"; do
  read -r left right <<<"${pair}"
  if ! diff -u "${OUT_DIR}/${left}.txt" "${OUT_DIR}/${right}.txt"; then
    status=1
  fi
done

if [[ "${status}" -ne 0 ]]; then
  printf '\nFAIL: a measurement differs between legs\n' >&2
  exit 1
fi
printf '\nOK: counts, graph list and index definitions are identical across all three legs\n'
