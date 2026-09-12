# shellcheck shell=bash
# The one definition of "conventional commit header" (PLAN B.6 as amended by CHANGES X-04),
# shared by lint-commit-header.sh and bump-from-commits.sh.
#
# Sourced, never executed (mode 100644):
#   source "$script_dir/lib/conventional-header.sh"
#
#   parse_conventional_header "<header>"
#     Returns 0 and sets HEADER_TYPE (one of the ten allowed types) and HEADER_BREAKING
#     ("true" when the header carries '!' after type/scope, else "false").
#     Returns 1 -- with both variables reset to "" / "false" -- when the header does not parse.
#
# Rule: `<type>(<scope>)!: <description>` where
#   - type is exactly one of the ten types issue #79 names (DECISIONS F-06; `revert` and any
#     other word do not parse),
#   - scope is optional, parenthesised, any characters except parentheses (Conventional
#     Commits spec; allows `fix(ps-service, scripts): ...`),
#   - '!' is optional and marks a breaking change,
#   - the description is anything non-empty, so the `gh tt deliver` suffix ` - resolves #N`
#     is part of it (AC-BI-026).
# Type is read from the header only; `BREAKING CHANGE:` footers live in the body and are the
# classifier's concern (PLAN A-32).

if [[ -n "${CONVENTIONAL_HEADER_LIB_LOADED:-}" ]]; then
  return 0
fi
CONVENTIONAL_HEADER_LIB_LOADED=1

CONVENTIONAL_HEADER_TYPES="feat|fix|perf|docs|chore|ci|test|refactor|style|build"
CONVENTIONAL_HEADER_REGEX="^($CONVENTIONAL_HEADER_TYPES)(\([^()]+\))?(!)?: .+$"
CONVENTIONAL_HEADER_EXPECTED_FORM='type(scope)!: description'

HEADER_TYPE=""
HEADER_BREAKING="false"

parse_conventional_header() {
  local header="$1"
  HEADER_TYPE=""
  HEADER_BREAKING="false"
  if [[ ! "$header" =~ $CONVENTIONAL_HEADER_REGEX ]]; then
    return 1
  fi
  HEADER_TYPE="${BASH_REMATCH[1]}"
  if [[ -n "${BASH_REMATCH[3]}" ]]; then
    HEADER_BREAKING="true"
  fi
  return 0
}
