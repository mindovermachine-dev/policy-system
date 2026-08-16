#!/usr/bin/env bash
# Sub-agent worker primitive for the pipeline-rag2 spike.
#
# Fresh-context pi coding-agent, scoped + SHORT-lived. Leader delegates small
# tasks and verifies the on-disk artifact; never trusts a worker's summary.
#
#   run_worker.sh NAME "TASK" [TOOLS] [WD_SECS] [MODEL] [THINK]
# - NAME:         short id for the log filename (non-alnum -> '-')
# - TASK:         instruction handed to the worker (keep it SMALL; point to files)
# - TOOLS:        optional comma list -> pi --tools (default: pi's own)
# - WD_SECS:      watchdog in seconds. DEFAULT 300. We do NOT run sub-agents long.
#                 Pass a larger WD deliberately for design/RCA only.
# - MODEL:        ollama model (default qwen3:14b — fast mechanical code).
#                 Use qwen3.8:27b-mlx for design/RCA/reading.
# - THINK:        pi --thinking level (default off — avoid reasoning-token burn).
#
# Env overrides (still honor): WORKER_MODEL, WORKER_THINK, OLLAMA_HOST.
# Semantic log: logs/<ts>-<name>.jsonl ; transcripts: <ts>-<name>.{out,err}.
set -uo pipefail

SPIKE_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SPIKE_DIR/logs"
mkdir -p "$LOG_DIR"

NAME_IN="${1:?NAME required}"; TASK="${2:?TASK required}"
TOOLS="${3:-}"; WD="${4:-300}"; MODEL_ARG="${5:-}"; THINK_ARG="${6:-}"
MODEL="${MODEL_ARG:-${WORKER_MODEL:-glm-4.7-flash:q8_0}}"
THINK="${THINK_ARG:-${WORKER_THINK:-off}}"
export OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="$(printf '%s' "$NAME_IN" | tr ' /:.' '----' | cut -c1-50)"
BASE="$LOG_DIR/${TS}-${NAME}"
OUT="${BASE}.out"; ERR="${BASE}.err"; LOG="${BASE}.jsonl"

ARGS=( -p "$TASK" --no-session --thinking "$THINK" --provider ollama --model "$MODEL" )
[ -n "$TOOLS" ] && ARGS+=( --tools "$TOOLS" )

echo "work $NAME_IN :: model=$MODEL think=$THINK wd=${WD}s" >&2

pi "${ARGS[@]}" >"$OUT" 2>"$ERR" & pid=$!
if [ "$WD" -gt 0 ]; then ( sleep "$WD"; kill "$pid" 2>/dev/null ) & wd=$!; fi
wait "$pid" 2>/dev/null; status=$?
[ "${WD:-0}" -gt 0 ] && kill "$wd" 2>/dev/null

printf '{"ts":"%s","name":"%s","model":"%s","think":"%s","wd":%s,"status":%d,"out":"%s","err":"%s"}\n' \
  "$TS" "$NAME_IN" "$MODEL" "$THINK" "$WD" "$status" "$OUT" "$ERR" >> "$LOG"
echo "done $NAME_IN :: status=$status wd=${WD}s -> $OUT" >&2
exit "$status"
