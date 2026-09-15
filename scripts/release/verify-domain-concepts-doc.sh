#!/usr/bin/env bash
# Read back the packaged MCP copy of ps-domain-concepts.md and fail if it has drifted from the
# canonical doc -- defense-in-depth before any commit (AC-BI-003). Read-only: this script never
# writes.
#
# Usage: verify-domain-concepts-doc.sh
#
# Must be run with the repository working-tree root as the current directory (same convention
# as sync-domain-concepts-doc.sh). Exit 0 iff the packaged copy is byte-identical to the
# canonical doc; otherwise exit 1, logging the drift (event=doc_drift) and printing the diff.
# Exit 2 on a usage error (this script takes no arguments).
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/log.sh
source "$script_dir/lib/log.sh"

readonly EXIT_USAGE=2
readonly USAGE="usage: $(basename "$0")"

readonly CANONICAL_DOC_PATH="docs/artifacts/ps-domain-concepts.md"
readonly PACKAGED_DOC_PATH="ps-service/src/ps_service/mcp_interface/ps-domain-concepts.md"

report_doc_drift() {
  local doc_diff="$1"
  release_log error doc_drift outcome=drift_detected
  printf 'packaged MCP domain-concepts doc has drifted from the canonical doc:\n%s\n' \
    "$doc_diff" >&2
  summary_append "### verify-domain-concepts-doc: failed"
  summary_append "Packaged doc (\`$PACKAGED_DOC_PATH\`) has drifted from canonical (\`$CANONICAL_DOC_PATH\`):"
  summary_append '```diff'
  summary_append "$doc_diff"
  summary_append '```'
}

main() {
  if [[ $# -ne 0 ]]; then
    printf 'expected no arguments, got %s\n%s\n' "$#" "$USAGE" >&2
    exit "$EXIT_USAGE"
  fi

  local doc_diff
  if ! doc_diff="$(diff -u "$CANONICAL_DOC_PATH" "$PACKAGED_DOC_PATH")"; then
    report_doc_drift "$doc_diff"
    exit 1
  fi

  release_log info doc_verified outcome=verified
}

main "$@"
