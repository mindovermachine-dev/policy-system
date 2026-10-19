#!/usr/bin/env bash
# Measures the resource footprint of the evaluator proxy pod on a running cluster.
#
# Takes one sample after an idle window and one while a fixed request load runs
# against the proxy, then prints a Markdown table. Read-only: it only runs
# `kubectl get`, `kubectl top` and (without metrics-server) `kubectl exec ... cat`.
# It never writes a number anywhere except its own output.
#
# Usage:
#   scripts/measure-proxy-footprint.sh --url https://ps.test:30443 --cacert ca.pem \
#     [--namespace policy-system] [--context <ctx>] [--idle-seconds 120] \
#     [--load-seconds 60] [--concurrency 8] [--output footprint.md]
set -euo pipefail

NAMESPACE="policy-system"
CONTEXT=""
URL=""
CACERT=""
IDLE_SECONDS=120
LOAD_SECONDS=60
CONCURRENCY=8
OUTPUT=""
PROXY_SELECTOR="app.kubernetes.io/component=proxy"

usage() {
  sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

die() {
  echo "measure-proxy-footprint: $*" >&2
  exit 1
}

require_value() {
  [ "$#" -ge 2 ] || die "$1 needs a value"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --namespace) require_value "$@"; NAMESPACE="$2"; shift 2 ;;
    --context) require_value "$@"; CONTEXT="$2"; shift 2 ;;
    --url) require_value "$@"; URL="$2"; shift 2 ;;
    --cacert) require_value "$@"; CACERT="$2"; shift 2 ;;
    --idle-seconds) require_value "$@"; IDLE_SECONDS="$2"; shift 2 ;;
    --load-seconds) require_value "$@"; LOAD_SECONDS="$2"; shift 2 ;;
    --concurrency) require_value "$@"; CONCURRENCY="$2"; shift 2 ;;
    --output) require_value "$@"; OUTPUT="$2"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[ -n "$URL" ] || die "--url is required (for example https://ps.test:30443)"
for number in "$IDLE_SECONDS" "$LOAD_SECONDS" "$CONCURRENCY"; do
  [[ "$number" =~ ^[0-9]+$ ]] || die "seconds and concurrency must be whole numbers, got: $number"
done
[ "$CONCURRENCY" -ge 1 ] || die "--concurrency must be at least 1"

KUBECTL_ARGS=(-n "$NAMESPACE")
if [ -n "$CONTEXT" ]; then
  KUBECTL_ARGS=(--context "$CONTEXT" "${KUBECTL_ARGS[@]}")
fi

kc() {
  kubectl "${KUBECTL_ARGS[@]}" "$@"
}

POD="$(kc get pods -l "$PROXY_SELECTOR" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
[ -n "$POD" ] || die "no proxy pod found in namespace $NAMESPACE (selector $PROXY_SELECTOR). Fix: run the evaluator deploy script first."

METRICS="top"
if ! kc top pod "$POD" --no-headers >/dev/null 2>&1; then
  METRICS="cgroup"
  echo "measure-proxy-footprint: metrics-server is not available; falling back to the pod's cgroup files." >&2
  echo "Fix: for kubectl top, install metrics-server (kind needs --kubelet-insecure-tls), then rerun." >&2
  kc exec "$POD" -- cat /sys/fs/cgroup/memory.current >/dev/null 2>&1 \
    || die "neither kubectl top nor the pod's cgroup files are readable; nothing was measured."
fi

cgroup_cpu_usec() {
  kc exec "$POD" -- cat /sys/fs/cgroup/cpu.stat | awk '$1 == "usage_usec" { print $2 }'
}

# sample <phase> <window-seconds>: waits for the window, then prints one table row.
# With kubectl top the figures are the metrics-server reading at the end of the window;
# from cgroup files, memory is memory.current and CPU is the mean over the window.
sample() {
  local phase="$1" window="$2" cpu mem
  if [ "$METRICS" = "top" ]; then
    sleep "$window"
    read -r _ cpu mem < <(kc top pod "$POD" --no-headers)
  else
    local usec_start usec_end bytes
    usec_start="$(cgroup_cpu_usec)"
    sleep "$window"
    usec_end="$(cgroup_cpu_usec)"
    bytes="$(kc exec "$POD" -- cat /sys/fs/cgroup/memory.current)"
    mem="$((bytes / 1048576))Mi"
    if [ "$window" -gt 0 ]; then
      cpu="$(((usec_end - usec_start) / 1000 / window))m"
    else
      cpu="n/a"
    fi
  fi
  printf '| %s | %s | %s | %s | %s |\n' "$phase" "$POD" "$cpu" "$mem" "$METRICS"
}

node_row() {
  if [ "$METRICS" = "top" ]; then
    local line
    line="$(kc top node --no-headers 2>/dev/null | head -n 1 || true)"
    if [ -n "$line" ]; then
      read -r name cpu _ mem _ <<<"$line"
      printf '| node %s (whole) | - | %s | %s | top |\n' "$name" "$cpu" "$mem"
    fi
  fi
}

CURL_ARGS=(-s -o /dev/null --max-time 10)
if [ -n "$CACERT" ]; then
  CURL_ARGS+=(--cacert "$CACERT")
fi

load_worker() {
  while true; do
    curl "${CURL_ARGS[@]}" "${URL%/}/health" || true
  done
}

echo "measure-proxy-footprint: idle window ${IDLE_SECONDS}s on pod $POD (source: $METRICS)" >&2
IDLE_ROW="$(sample "idle (${IDLE_SECONDS}s)" "$IDLE_SECONDS")"

echo "measure-proxy-footprint: load ${CONCURRENCY} workers for ${LOAD_SECONDS}s against ${URL%/}/health" >&2
WORKERS=()
for _ in $(seq 1 "$CONCURRENCY"); do
  load_worker &
  WORKERS+=("$!")
done
trap 'kill "${WORKERS[@]}" 2>/dev/null || true' EXIT
LOAD_ROW="$(sample "load (${CONCURRENCY} x GET /health, ${LOAD_SECONDS}s)" "$LOAD_SECONDS")"
kill "${WORKERS[@]}" 2>/dev/null || true
wait 2>/dev/null || true
NODE_ROW="$(node_row)"

TABLE="$(
  printf '%s\n' \
    "| Phase | Pod | CPU | Memory | Source |" \
    "| --- | --- | --- | --- | --- |" \
    "$IDLE_ROW" \
    "$LOAD_ROW"
  [ -z "$NODE_ROW" ] || printf '%s\n' "$NODE_ROW"
)"

printf '%s\n' "$TABLE"
if [ -n "$OUTPUT" ]; then
  printf '%s\n' "$TABLE" >"$OUTPUT"
fi
