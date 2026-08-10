#!/bin/zsh
# © 2026 Cartman ApS. All rights reserved.
# Blind-set (held-out) runner for the skill-transfer spike.
# Runs each question from blind_questions.tsv (verbatim text copied from
# ps-questions/blind-questions.md) as a fresh headless Copilot CLI session
# (kimi-k3), scoped to redis-cli only, saving one transcript per question.
# Output: runs/blind-v1/<ID>.log
#
# DISCIPLINE: this is the single-use held-out run. The ps-domain skill is
# frozen; questions are asked verbatim with no hints; there is no
# fix-and-retry. See RUNBOOK.md for the procedure this implements.

set -u
cd "$(dirname "$0")/../.."   # repo root — required so the CLI auto-loads .github/skills/ps-domain

OUT_DIR="spikes/skill-transfer/runs/blind-v1"
QUESTIONS_FILE="spikes/skill-transfer/blind_questions.tsv"
mkdir -p "$OUT_DIR"

HARNESS_PREFIX='You have access to a FalkorDB graph database. Query it ONLY by running a single plain command of the form: redis-cli GRAPH.QUERY policy_system "<cypher>" — no pipes, no chained commands, no other shell tools. If that command fails, report the error; do not fall back to reading files. For reference, today is 2026-08-01. Question:'

COUNT=$(wc -l < "$QUESTIONS_FILE" | tr -d ' ')
echo "Running $COUNT blind questions into $OUT_DIR"
while IFS=$'\t' read -r ID Q; do
  [[ -z "$ID" ]] && continue
  LOG="$OUT_DIR/$ID.log"
  if [[ -s "$LOG" ]]; then
    echo "SKIP $ID (transcript exists)"
    continue
  fi
  echo "RUN  $ID ..."
  copilot -p "$HARNESS_PREFIX $Q" \
    --model kimi-k3 \
    --allow-tool "shell(redis-cli:*)" \
    > "$LOG" 2>&1
  echo "DONE $ID (exit $?)"
done < <(sort "$QUESTIONS_FILE")
echo "All runs complete."
