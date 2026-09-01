#!/usr/bin/env bash
# Starts and stops PS Service (uv run python -m ps_service) as a background
# process. Requires FalkorDB already running (see CONTRIBUTING.md) and, for
# real ingestion runs, .env configured (LLM Interface + Company Merge) —
# this script only manages the PS Service process itself.
#
# Usage:
#   scripts/ps-service.sh start   # start in the background, wait for /health
#   scripts/ps-service.sh stop    # SIGTERM, wait for graceful shutdown
#   scripts/ps-service.sh status  # report running/not-running + /health, /ready
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

HOST="${PS_SERVICE_HOST:-127.0.0.1}"
PORT="${PS_SERVICE_PORT:-8000}"
PID_FILE="${REPO_ROOT}/logs/ps-service.pid"
STDOUT_LOG="${REPO_ROOT}/logs/ps-service-stdout.log"
STARTUP_TIMEOUT_SECONDS="${PS_SERVICE_STARTUP_TIMEOUT_SECONDS:-30}"
SHUTDOWN_TIMEOUT_SECONDS="${PS_SERVICE_SHUTDOWN_TIMEOUT_SECONDS:-15}"

running_pid() {
  # Prints the PID if $PID_FILE names a live process, otherwise nothing.
  if [[ -f "${PID_FILE}" ]]; then
    local pid
    pid="$(cat "${PID_FILE}")"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      echo "${pid}"
      return
    fi
  fi
}

cmd_start() {
  local existing_pid
  existing_pid="$(running_pid)"
  if [[ -n "${existing_pid}" ]]; then
    echo "PS Service already running (pid ${existing_pid})."
    exit 0
  fi

  mkdir -p "${REPO_ROOT}/logs"

  if [[ -f "${REPO_ROOT}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/.env"
    set +a
  fi

  echo "Starting PS Service (uv run python -m ps_service) on ${HOST}:${PORT}..."
  (
    cd "${REPO_ROOT}"
    nohup uv run python -m ps_service >"${STDOUT_LOG}" 2>&1 &
    echo $! >"${PID_FILE}"
  )

  local waited=0
  until curl -sf -m 2 "http://${HOST}:${PORT}/health" >/dev/null 2>&1; do
    if ! kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
      echo "PS Service exited during startup — see ${STDOUT_LOG}" >&2
      rm -f "${PID_FILE}"
      exit 1
    fi
    if (( waited >= STARTUP_TIMEOUT_SECONDS )); then
      echo "PS Service did not become healthy within ${STARTUP_TIMEOUT_SECONDS}s — see ${STDOUT_LOG}" >&2
      exit 1
    fi
    sleep 1
    waited=$((waited + 1))
  done

  echo "PS Service is alive (pid $(cat "${PID_FILE}"))."
  local ready_status
  ready_status="$(curl -sf -m 2 "http://${HOST}:${PORT}/ready" 2>/dev/null | grep -o '"status":"[^"]*"' || echo 'unreachable')"
  echo "Readiness: ${ready_status} (check ${STDOUT_LOG} or logs/ps-service.jsonl if not ready)"
}

cmd_stop() {
  local pid
  pid="$(running_pid)"
  if [[ -z "${pid}" ]]; then
    echo "PS Service is not running."
    rm -f "${PID_FILE}"
    exit 0
  fi

  echo "Stopping PS Service (pid ${pid})..."
  kill -TERM "${pid}"

  local waited=0
  while kill -0 "${pid}" 2>/dev/null; do
    if (( waited >= SHUTDOWN_TIMEOUT_SECONDS )); then
      echo "PS Service did not exit within ${SHUTDOWN_TIMEOUT_SECONDS}s after SIGTERM — leaving it running. Investigate before sending SIGKILL yourself." >&2
      exit 1
    fi
    sleep 1
    waited=$((waited + 1))
  done

  rm -f "${PID_FILE}"
  echo "PS Service stopped."
}

cmd_status() {
  local pid
  pid="$(running_pid)"
  if [[ -z "${pid}" ]]; then
    echo "PS Service is not running."
    exit 1
  fi
  echo "PS Service is running (pid ${pid})."
  curl -sf -m 2 "http://${HOST}:${PORT}/health" >/dev/null 2>&1 \
    && echo "/health: alive" \
    || echo "/health: unreachable"
  local ready_status
  ready_status="$(curl -sf -m 2 "http://${HOST}:${PORT}/ready" 2>/dev/null | grep -o '"status":"[^"]*"' || echo 'unreachable')"
  echo "/ready: ${ready_status}"
}

case "${1:-}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  *)
    echo "Usage: $(basename "${BASH_SOURCE[0]}") {start|stop|status}" >&2
    exit 2
    ;;
esac
