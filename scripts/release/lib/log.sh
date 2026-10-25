# shellcheck shell=bash
# Structured logging and $GITHUB_STEP_SUMMARY helpers for scripts/release/*.sh (PLAN B.5).
#
# Sourced, never executed (mode 100644):
#   source "$script_dir/lib/log.sh"
#
#   release_log <level> <event> [key=value ...]
#     One line on stderr, greppable by field:
#       ts=2026-09-11T10:00:00Z level=warn component=release action=<script> event=<event> key=value ...
#     Values containing whitespace, quotes or '=' are double-quoted with '\' and '"' escaped.
#     Never pass tokens, remote URLs or environment dumps (L1 Security by Design).
#
#   summary_append "<markdown line>"
#     Appends to $GITHUB_STEP_SUMMARY when it is set; a no-op in local runs.
#
#   capture_step <var_name> <step> <command> [args ...]
#     Use instead of `var="$(command ...)"`. Runs <command> and stores its stdout in <var_name>.
#     On failure it logs `event=step_failed step=<step> exit_code=<rc>`, writes a human-readable
#     line plus the command's captured stdout to stderr, and exits with the command's status --
#     so a failing sub-command is never hidden by `set -e` inside a command substitution.
#     The command's stderr is passed through untouched. Errexit does not apply inside <command>
#     (it runs in a condition context), so a function passed here must return its status from
#     its last command, e.g. a single command or a pipeline under `pipefail`.
#
# `action` defaults to the sourcing script's basename (without .sh); a script may set
# RELEASE_LOG_ACTION before sourcing to override it.

if [[ -n "${RELEASE_LOG_LIB_LOADED:-}" ]]; then
  return 0
fi
RELEASE_LOG_LIB_LOADED=1

RELEASE_LOG_COMPONENT="release"
RELEASE_LOG_ACTION="${RELEASE_LOG_ACTION:-$(basename "$0" .sh)}"
RELEASE_LOG_FIELD_NEEDS_QUOTING='[[:space:]"=]'

# quote_log_field key=value -> key=value, or key="escaped value" when the value needs it.
quote_log_field() {
  local field="$1"
  local key="${field%%=*}"
  local value="${field#*=}"
  if [[ "$value" =~ $RELEASE_LOG_FIELD_NEEDS_QUOTING ]]; then
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    printf '%s="%s"' "$key" "$value"
  else
    printf '%s=%s' "$key" "$value"
  fi
}

# release_log <level> <event> [key=value ...] -> one structured line on stderr.
release_log() {
  local level="$1"
  local event="$2"
  shift 2
  local timestamp
  timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  local line="ts=$timestamp level=$level component=$RELEASE_LOG_COMPONENT"
  line+=" action=$RELEASE_LOG_ACTION event=$event"
  local field
  for field in "$@"; do
    line+=" $(quote_log_field "$field")"
  done
  printf '%s\n' "$line" >&2
}

# summary_append "<markdown line>" -> appended to $GITHUB_STEP_SUMMARY when set.
summary_append() {
  local markdown_line="$1"
  if [[ -z "${GITHUB_STEP_SUMMARY:-}" ]]; then
    return 0
  fi
  printf '%s\n' "$markdown_line" >>"$GITHUB_STEP_SUMMARY"
}

# capture_step <var_name> <step> <command> [args ...] -> stdout into <var_name>, or exit loudly.
capture_step() {
  local capture_step_var="$1"
  local capture_step_name="$2"
  shift 2
  local capture_step_output
  local capture_step_status=0
  capture_step_output="$("$@")" || capture_step_status=$?
  if ((capture_step_status != 0)); then
    release_log error step_failed step="$capture_step_name" exit_code="$capture_step_status"
    printf '%s failed (exit %s)\n' "$capture_step_name" "$capture_step_status" >&2
    if [[ -n "$capture_step_output" ]]; then
      printf '%s\n' "$capture_step_output" >&2
    fi
    exit "$capture_step_status"
  fi
  printf -v "$capture_step_var" '%s' "$capture_step_output"
}
