#!/usr/bin/env bash
# Loads CRA, GDPR, NIS2, and Engineering Practices into FalkorDB via load_graph.py.
# Requires a running FalkorDB instance, e.g.:
#   podman run --rm -d --name falkordb -p 6379:6379 falkordb/falkordb:latest
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DATA_DIR="${REPO_ROOT}/test-data"

GRAPH_NAME="${GRAPH_NAME:-policy_system}"
HOST="${FALKORDB_HOST:-localhost}"
PORT="${FALKORDB_PORT:-6379}"

load() {
  local file="$1"
  shift
  python3 "${SCRIPT_DIR}/load_graph.py" \
    --file "${file}" \
    --graph-name "${GRAPH_NAME}" \
    --host "${HOST}" \
    --port "${PORT}" \
    "$@"
}

echo "Loading CRA, GDPR, NIS2, and Engineering Practices into FalkorDB graph '${GRAPH_NAME}' at ${HOST}:${PORT}..."

load "${DATA_DIR}/eu-regulations/cra.json" --reset
load "${DATA_DIR}/eu-regulations/gdpr.json"
load "${DATA_DIR}/eu-regulations/nis2.json"
load "${DATA_DIR}/engineering-practices/engineering-practices-seed.json"

echo "Done."
