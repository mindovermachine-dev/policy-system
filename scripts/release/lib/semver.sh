# shellcheck shell=bash
# The one definition of "valid release version" (CHANGES X-01), shared by release.sh,
# next-version.sh, and (in later slices) sync-version-files.sh / publish-release.sh.
#
# Sourced, never executed (mode 100644):
#   source "$script_dir/lib/semver.sh"
#
#   validate_semver "<value>"
#     Returns 0 when <value> matches ^[0-9]+\.[0-9]+\.[0-9]+$ , 1 otherwise. No output either way
#     -- callers own their own error message and logging (this lib never logs or prints).

if [[ -n "${RELEASE_SEMVER_LIB_LOADED:-}" ]]; then
  return 0
fi
RELEASE_SEMVER_LIB_LOADED=1

readonly RELEASE_SEMVER_REGEX='^[0-9]+\.[0-9]+\.[0-9]+$'

validate_semver() {
  local candidate="$1"
  [[ "$candidate" =~ $RELEASE_SEMVER_REGEX ]]
}
