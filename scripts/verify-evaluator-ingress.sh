#!/usr/bin/env bash
# Read-only gate for the evaluator's kind cluster: checks that the ingress controller answers on
# host port 30443 and that no other port is published off the loopback.
#
#   scripts/verify-evaluator-ingress.sh
#
# Checks, one PASS or FAIL line each (all run; exit 1 when any fails, else 0):
#   1. the ingress-nginx controller Deployment has a ready replica;
#   2. its admission webhook Service has an endpoint (without one, Ingress creation is rejected);
#   3. https://127.0.0.1:30443/ answers 404 (the controller's default backend: no route matches);
#   4. the kind node container publishes 30443 and is otherwise open off the loopback on no port,
#      and publishes 3001 (FalkorDB Browser) on the loopback only. The kind API server port is
#      published on the loopback and is allowed.
# Needs kubectl pointing at a kind-* context, curl, and podman or docker. Changes nothing.
set -euo pipefail

readonly EXIT_FAILURE=1
readonly INGRESS_NAMESPACE="ingress-nginx"
readonly INGRESS_PORT=30443
readonly BROWSER_PORT=3001

failures=0

pass() {
  printf 'PASS %s\n' "$1"
}

fail() {
  printf 'FAIL %s\n' "$1"
  failures=$((failures + 1))
}

# check_controller_ready: the controller Deployment reports at least one ready replica.
check_controller_ready() {
  local ready
  ready="$(kubectl -n "$INGRESS_NAMESPACE" get deployment ingress-nginx-controller \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
  if [[ "${ready:-0}" =~ ^[1-9][0-9]*$ ]]; then
    pass "ingress controller Deployment is ready (${ready} replica)"
  else
    fail "ingress controller Deployment is not ready; apply deploy/kind/ingress-nginx.yaml and wait for it"
  fi
}

# check_admission_endpoint: the admission webhook Service has an address behind it.
check_admission_endpoint() {
  local addresses
  addresses="$(kubectl -n "$INGRESS_NAMESPACE" get endpointslices \
    -l kubernetes.io/service-name=ingress-nginx-controller-admission \
    -o jsonpath='{.items[*].endpoints[*].addresses[*]}' 2>/dev/null || true)"
  if [[ -n "$addresses" ]]; then
    pass "ingress admission webhook has an endpoint"
  else
    fail "ingress admission webhook has no endpoint yet"
  fi
}

# check_default_backend: a request that matches no Ingress rule is answered 404 on the host port.
check_default_backend() {
  local code
  code="$(curl -ksS --max-time 10 -o /dev/null -w '%{http_code}' \
    "https://127.0.0.1:${INGRESS_PORT}/" 2>/dev/null || true)"
  if [[ "$code" == 404 ]]; then
    pass "https://127.0.0.1:${INGRESS_PORT}/ answers 404"
  else
    fail "https://127.0.0.1:${INGRESS_PORT}/ answered ${code:-nothing}, expected 404"
  fi
}

# check_published_ports <node-container>: reads the node's published host ports. Each line of
# `port` output is `<container-port>/tcp -> <address>:<host-port>`.
check_published_ports() {
  local node="$1" engine="" candidate ports line address host_port
  local open_ports=() browser_loopback=false ingress_open=false extra=()
  for candidate in podman docker; do
    if command -v "$candidate" >/dev/null 2>&1; then
      engine="$candidate"
      break
    fi
  done
  if [[ -z "$engine" ]]; then
    fail "neither podman nor docker is available to list the published ports of ${node}"
    return 0
  fi
  if ! ports="$("$engine" port "$node" 2>/dev/null)"; then
    fail "${engine} port ${node} failed; check the cluster is running"
    return 0
  fi
  while IFS= read -r line; do
    [[ "$line" == *" -> "* ]] || continue
    line="${line##* -> }"
    host_port="${line##*:}"
    address="${line%:*}"
    case "$address" in
      127.0.0.1 | "[::1]" | ::1)
        [[ "$host_port" == "$BROWSER_PORT" ]] && browser_loopback=true
        ;;
      *)
        open_ports+=("$host_port")
        ;;
    esac
  done <<<"$ports"
  for host_port in ${open_ports[@]+"${open_ports[@]}"}; do
    if [[ "$host_port" == "$INGRESS_PORT" ]]; then
      ingress_open=true
    else
      extra+=("$host_port")
    fi
  done
  if [[ ${#extra[@]} -gt 0 ]]; then
    fail "host port(s) ${extra[*]} are published beyond the loopback; only ${INGRESS_PORT} may be"
  elif [[ "$ingress_open" != true ]]; then
    fail "host port ${INGRESS_PORT} is not published"
  elif [[ "$browser_loopback" != true ]]; then
    fail "host port ${BROWSER_PORT} is not published on the loopback only"
  else
    pass "only ${INGRESS_PORT} is published beyond the loopback; ${BROWSER_PORT} is loopback-only"
  fi
}

main() {
  local context node
  context="$(kubectl config current-context)"
  if [[ "$context" != kind-* ]]; then
    printf 'Current kubectl context "%s" is not a kind-* context. Fix: kubectl config use-context kind-<name>.\n' \
      "$context" >&2
    exit "$EXIT_FAILURE"
  fi
  node="${context#kind-}-control-plane"
  check_controller_ready
  check_admission_endpoint
  check_default_backend
  check_published_ports "$node"
  [[ "$failures" -eq 0 ]] || exit "$EXIT_FAILURE"
}

main "$@"
