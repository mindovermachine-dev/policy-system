#!/usr/bin/env bash
# Publish (or update) the GitHub Release for a resolved release tag (S10, PLAN B.1, CHANGES X-02).
#
# Usage: create-github-release.sh <release_version>
#
# <release_version> is the resolved git tag (`resolve-tag`'s `git-tag` output; may be bare or
# `v`-prefixed, X-05) -- the release is created/updated for this exact tag and titled with it.
# Must run with the repository working-tree root as the current directory, checked out with
# full history (`fetch-depth: 0`) so `gh tt semver note` can diff from the baseline.
#
# 1. previous-release-tag.sh <bare_release_version> -> baseline (the `v`-prefix, if any, is
#    stripped before this call since that script's contract takes a bare semver).
# 2. Writes release notes to a temp file via
#    `${PS_RELEASE_GH:-gh} tt semver note --from <baseline> --to <release_version> --filename
#    <notes_file>` (A-06/A-07) -- exactly once.
# 3. `${PS_RELEASE_GH:-gh} release view <release_version>`: if it already exists, `gh release
#    edit <release_version> --notes-file <notes_file>`; otherwise `gh release create
#    <release_version> --verify-tag --title <release_version> --notes-file <notes_file>`.
# 4. Logs + a $GITHUB_STEP_SUMMARY line either way.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/log.sh
source "$script_dir/lib/log.sh"

readonly EXIT_USAGE=2
readonly USAGE="usage: $(basename "$0") <release_version>"

# write_release_notes <baseline> <release_version> <notes_file>: gh-tt note, exactly once.
write_release_notes() {
  local baseline="$1"
  local release_version="$2"
  local notes_file="$3"
  "${PS_RELEASE_GH:-gh}" tt semver note \
    --from "$baseline" --to "$release_version" --filename "$notes_file" >/dev/null
}

# release_exists <release_version>: true iff a GitHub Release already exists for the tag.
release_exists() {
  local release_version="$1"
  "${PS_RELEASE_GH:-gh}" release view "$release_version" >/dev/null 2>&1
}

# publish_release <release_version> <notes_file>: create the release, verifying the tag exists.
publish_release() {
  local release_version="$1"
  local notes_file="$2"
  "${PS_RELEASE_GH:-gh}" release create "$release_version" \
    --verify-tag --title "$release_version" --notes-file "$notes_file" >/dev/null
  release_log info release_created outcome=created release_version="$release_version"
  summary_append "### create-github-release: created"
  summary_append "Created GitHub Release \`$release_version\`."
}

# update_release <release_version> <notes_file>: refresh an existing release's notes.
update_release() {
  local release_version="$1"
  local notes_file="$2"
  "${PS_RELEASE_GH:-gh}" release edit "$release_version" --notes-file "$notes_file" >/dev/null
  release_log info release_edited outcome=edited release_version="$release_version"
  summary_append "### create-github-release: updated"
  summary_append "Updated GitHub Release \`$release_version\`."
}

main() {
  if [[ $# -ne 1 ]]; then
    printf 'expected exactly one argument, got %s\n%s\n' "$#" "$USAGE" >&2
    exit "$EXIT_USAGE"
  fi
  local release_version="$1"
  local bare_release_version="${release_version#v}"

  local baseline
  baseline="$("$script_dir/previous-release-tag.sh" "$bare_release_version")"

  local notes_file
  notes_file="$(mktemp)"
  # Left in place, not cleaned up: the job container that runs this script is ephemeral (the
  # same convention as the rest of `on_semver.yml`'s `/tmp` artifacts).

  write_release_notes "$baseline" "$release_version" "$notes_file"
  release_log info note_written outcome=note_written release_version="$release_version" \
    baseline="$baseline"

  if release_exists "$release_version"; then
    update_release "$release_version" "$notes_file"
  else
    publish_release "$release_version" "$notes_file"
  fi
}

main "$@"
