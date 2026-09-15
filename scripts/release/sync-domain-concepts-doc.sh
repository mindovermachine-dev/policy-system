#!/usr/bin/env bash
# Regenerate the packaged MCP copy of ps-domain-concepts.md from the canonical doc (AC-BI-002).
#
# Usage: sync-domain-concepts-doc.sh
#
# Must be run with the repository working-tree root as the current directory -- matches how
# sync-version-files.sh is invoked. Writes:
#   ps-service/src/ps_service/mcp_interface/ps-domain-concepts.md   (byte-identical copy of
#                                                                    docs/artifacts/ps-domain-concepts.md)
#
# No templating: the packaged copy is byte-identical to the canonical doc by design, so
# regeneration is a straight `cp`, not a transform.
#
# Exit 1 (error log + message on stderr) when the canonical doc does not exist. Exit 2 on a
# usage error (this script takes no arguments).
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/log.sh
source "$script_dir/lib/log.sh"

readonly EXIT_USAGE=2
readonly USAGE="usage: $(basename "$0")"

readonly CANONICAL_DOC_PATH="docs/artifacts/ps-domain-concepts.md"
readonly PACKAGED_DOC_PATH="ps-service/src/ps_service/mcp_interface/ps-domain-concepts.md"

# assert_canonical_doc_exists: fail fast, naming the missing path, before the cp sink runs.
assert_canonical_doc_exists() {
  if [[ ! -f "$CANONICAL_DOC_PATH" ]]; then
    release_log error file_missing path="$CANONICAL_DOC_PATH"
    printf 'expected "%s" to exist relative to the current directory (run from the repo root)\n' \
      "$CANONICAL_DOC_PATH" >&2
    exit 1
  fi
}

main() {
  if [[ $# -ne 0 ]]; then
    printf 'expected no arguments, got %s\n%s\n' "$#" "$USAGE" >&2
    exit "$EXIT_USAGE"
  fi

  assert_canonical_doc_exists

  cp "$CANONICAL_DOC_PATH" "$PACKAGED_DOC_PATH"

  release_log info doc_synced outcome=doc_synced
  summary_append "Synced packaged MCP domain-concepts doc from canonical."
}

main "$@"
