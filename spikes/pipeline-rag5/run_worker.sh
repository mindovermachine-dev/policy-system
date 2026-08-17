#!/usr/bin/env bash
# Sub-agent worker primitive for the pipeline-rag3 spike.
#
# Fresh-context pi coding-agent, scoped + SHORT-lived. Leader delegates small
# tasks and verifies the on-disk artifact; never trusts a worker's summary.
#
#   run_worker.sh NAME "TASK" [TOOLS] [WD_SECS] [MODEL] [THINK] [ARTIFACT]
# - NAME:         short id for the log filename (non-alnum -> '-')
# - TASK:         instruction handed to the worker (keep it SMALL; point to files)
# - TOOLS:        optional comma list -> pi --tools (default: pi's own)
# - WD_SECS:      watchdog in seconds. DEFAULT 300. We do NOT run sub-agents long.
# - MODEL:        ollama model (default glm-4.7-flash:q8_0 — fast mechanical code).
#                 Use qwen3-coder-next:q8_0 for coding; qwen3.8:27b-mlx for design/RCA.
# - THINK:        pi --thinking level (default off — avoid reasoning-token burn).
# - ARTIFACT:     optional path the task is expected to produce/modify. Its
#                  (mtime,size) is snapshotted before/after and reported so the
#                 leader knows WHETHER the file changed — independent of the
#                 worker's (untrusted) summary.
#
# Env overrides (still honor): WORKER_MODEL, WORKER_THINK, OLLAMA_HOST.
# Semantic log: logs/<ts>-<name>.jsonl ; transcripts: <ts>-<name>.{out,err}.
#
# WATCHDOG (RCA 2026-08-16; system bash is 3.2: no `wait -n`, no GNU `timeout`):
#   * `wait` ONLY the explicit pi pid — NEVER a bare `wait` (blocks on detached child).
#   * the watchdog subshell's fds are DETACHED from the caller's stdout/stderr: a
#     leftover `sleep $WD` (orphan, harmless, auto-reaps) would otherwise hold an
#     open stdout PIPE and block any downstream `| grep`/reader.
# OUTCOME (previously a clean exit misprinted 'Terminated: 15'):
#   FINISHED          pi exited 0
#   WATCHDOG_KILLED   watchdog SIGTERM'd pi (status 143)
#   EXIT_N=<n>        pi exited nonzero n<128
set -uo pipefail

SPIKE_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SPIKE_DIR/logs"
mkdir -p "$LOG_DIR"

NAME_IN="${1:?NAME required}"; TASK="${2:?TASK required}"
TOOLS="${3:-}"; WD="${4:-300}"; MODEL_ARG="${5:-}"; THINK_ARG="${6:-}"; ARTIFACT="${7:-}"
MODEL="${MODEL_ARG:-${WORKER_MODEL:-glm-4.7-flash:q8_0}}"
THINK="${THINK_ARG:-${WORKER_THINK:-off}}"
export OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="$(printf '%s' "$NAME_IN" | tr ' /:.' '----' | cut -c1-50)"
BASE="$LOG_DIR/${TS}-${NAME}"
OUT="${BASE}.out"; ERR="${BASE}.err"; LOG="${BASE}.jsonl"

# artifact snapshot: "mtime_epoch size" or "absent" — valid before the file exists.
stat_now() {
     local f="$1"
     if [ -n "$f" ] && [ -e "$f" ]; then
        stat -f '%m %z' "$f" 2>/dev/null || echo "unknown"
     else
        echo "absent"
     fi
}
ART_BEFORE="$(stat_now "$ARTIFACT")"

ARGS=( -p "$TASK" --no-session --thinking "$THINK" --provider ollama --model "$MODEL" )
[ -n "$TOOLS" ] && ARGS+=( --tools "$TOOLS" )

echo "work $NAME_IN :: model=$MODEL think=$THINK wd=${WD}s artifact=${ARTIFACT:-<none>}" >&2
[ -n "$ARTIFACT" ] && echo "work $NAME_IN :: artifact_before=[$ART_BEFORE]" >&2

# Run pi. `wait` only the explicit pi pid; NEVER a bare `wait` in bash 3.2.
WD_PID=""
stop_wd() { [ -n "${WD_PID:-}" ] && kill "${WD_PID}" 2>/dev/null; WD_PID=""; }
trap stop_wd EXIT INT TERM

pi "${ARGS[@]}" >"$OUT" 2>"$ERR" & pid=$!
if [ "${WD:-0}" -gt 0 ]; then
     # Detach the watchdog subshell's fds from the caller so a leftover `sleep $WD`
     # cannot hold the pipe open. It orphans harmlessly and auto-reaps.
     ( sleep "${WD}"; kill -TERM "${pid}" 2>/dev/null ) >/dev/null 2>>"$ERR" & WD_PID=$!
fi

wait "${pid}" 2>/dev/null; status=$?
stop_wd    # terminate the (still-sleeping) watchdog subshell

if [ "${status}" -eq 0 ]; then
     OUTCOME="FINISHED"
elif [ "${status}" -eq 143 ]; then
     OUTCOME="WATCHDOG_KILLED"
else
     OUTCOME="EXIT_N=${status}"
fi

ART_AFTER="$(stat_now "$ARTIFACT")"
if [ -z "$ARTIFACT" ]; then
   ART_DELTA="n/a"
elif [ "$ART_BEFORE" = "absent" ] && [ "$ART_AFTER" != "absent" ]; then
   ART_DELTA="CREATED"
elif [ "$ART_BEFORE" != "absent" ] && [ "$ART_AFTER" = "absent" ]; then
   ART_DELTA="DELETED"
elif [ "$ART_BEFORE" = "$ART_AFTER" ]; then
   ART_DELTA="UNCHANGED"
else
   ART_DELTA="CHANGED"
fi

printf '{"ts":"%s","name":"%s","model":"%s","think":"%s","wd":%s,"status":%d,"outcome":"%s","artifact":"%s","artifact_before":"%s","artifact_after":"%s","artifact_delta":"%s","out":"%s","err":"%s"}\n' \
     "$TS" "$NAME_IN" "$MODEL" "$THINK" "${WD:-0}" "$status" "$OUTCOME" "$ARTIFACT" \
     "$ART_BEFORE" "$ART_AFTER" "$ART_DELTA" "$OUT" "$ERR" >> "$LOG"
echo "done $NAME_IN :: outcome=$OUTCOME status=$status wd=${WD}s artifact_delta=$ART_DELTA -> $OUT" >&2
exit "${status}"
