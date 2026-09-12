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
