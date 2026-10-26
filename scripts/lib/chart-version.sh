# shellcheck shell=bash
# Chart version pinning for the deploy scripts, shared by scripts/deploy-ps-eval.sh and
# scripts/deploy-ps-prod.sh.
#
# Sourced, never executed (mode 100644):
#   source "$SCRIPT_DIR/lib/chart-version.sh"
#
# Inside the release bundle (scripts/release/build-deploy-bundle.sh) a VERSION file sits next to
# scripts/. Installing the chart of that same release keeps the scripts and the chart from drifting.
# A clone has no VERSION file and a local chart path has no versions to pick from; both keep the
# unpinned install of the newest published chart.
#
# Bash 3.2 compatible. Callers expand the array as ${chart_version_args[@]+"${chart_version_args[@]}"}
# so an empty array is safe under `set -u`.

# set_chart_version_args <script_dir> <chart_ref>: sets the global chart_version_args array to
# (--version <release>) or to ().
# shellcheck disable=SC2034 # chart_version_args is read by the sourcing script
set_chart_version_args() {
  local script_dir="$1" chart_ref="$2"
  local version_file="${script_dir}/../VERSION"
  chart_version_args=()
  if [[ -f "$version_file" && "$chart_ref" == oci://* ]]; then
    chart_version_args=(--version "$(tr -d '[:space:]' <"$version_file")")
  fi
}

# The functions below make the bundle's chart version part of the desired state of the release.
# They report failures through the caller's print_error and return 1; a sourced lib must not
# exit, so callers write `|| exit "$EXIT_FAILURE"`.

# shellcheck disable=SC2034 # read by the sourcing script
bundle_chart_version=""
# shellcheck disable=SC2034 # read by the sourcing script
chart_version_action="none"
readonly CHART_VERSION_PATTERN='^[0-9]+\.[0-9]+\.[0-9]+$'

# is_chart_version <text>: true when the text is exactly X.Y.Z (digits only, no prefix or suffix).
is_chart_version() {
  [[ "$1" =~ $CHART_VERSION_PATTERN ]]
}

# compare_chart_versions <a> <b>: prints newer, equal or older for <a> relative to <b>. Both must
# satisfy is_chart_version. Parts compare as decimal numbers, so 1.10.0 is newer than 1.9.0.
compare_chart_versions() {
  local -a a b
  local i
  IFS=. read -r -a a <<<"$1"
  IFS=. read -r -a b <<<"$2"
  for i in 0 1 2; do
    if ((10#${a[i]} > 10#${b[i]})); then
      printf 'newer\n'
      return 0
    fi
    if ((10#${a[i]} < 10#${b[i]})); then
      printf 'older\n'
      return 0
    fi
  done
  printf 'equal\n'
}

# read_bundle_version <script_dir> <chart_ref>: sets bundle_chart_version to the bundle's VERSION,
# or to "" when there is no VERSION file or the chart is not the published one (a clone or a local
# chart path). A VERSION file that does not hold X.Y.Z fails.
# shellcheck disable=SC2034 # bundle_chart_version is read by the sourcing script
read_bundle_version() {
  local script_dir="$1" chart_ref="$2"
  local version_file="${script_dir}/../VERSION" content
  bundle_chart_version=""
  if [[ ! -f "$version_file" || "$chart_ref" != oci://* ]]; then
    return 0
  fi
  content="$(tr -d '[:space:]' <"$version_file")"
  if ! is_chart_version "$content"; then
    print_error 'The bundle version file %s must hold a version like 12.1.0, but holds "%s". Fix: re-download the release bundle; nothing was changed.\n' \
      "$version_file" "$content"
    return 1
  fi
  bundle_chart_version="$content"
}

# read_deployed_chart_version <release>: sets deployed_chart_version to the chart version Helm
# reports for the release. Fails, naming the release and the command, when helm fails or the chart
# does not carry an X.Y.Z version.
# shellcheck disable=SC2034 # deployed_chart_version is read by the sourcing script
read_deployed_chart_version() {
  local release="$1" listing chart
  deployed_chart_version=""
  if ! listing="$(helm list -f "^${release}\$" -o json 2>/dev/null)"; then
    listing=""
  fi
  chart="$(jq -r --arg r "$release" '[.[] | select(.name == $r)][0].chart // empty' \
    <<<"$listing" 2>/dev/null)" || chart=""
  deployed_chart_version="$(printf '%s' "$chart" | sed -n 's/^.*-\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$/\1/p')"
  if ! is_chart_version "$deployed_chart_version"; then
    print_error 'Could not read the chart version of Helm release %s (chart "%s") with: helm list -f ^%s$ -o json. Fix: check helm access to the cluster; to continue, reinstall the matching release with scripts/ps-upgrade.sh. Nothing was changed.\n' \
      "$release" "$chart" "$release"
    deployed_chart_version=""
    return 1
  fi
}

# plan_chart_version_action <release>: sets chart_version_action to
#   none     no bundle version applies, or the release is not installed yet
#   upgrade  the bundle's chart version is newer than the installed one
#   same     both versions are equal
# An installed chart newer than the bundle fails: there is no silent downgrade.
# Reads only. Needs bundle_chart_version from read_bundle_version.
# shellcheck disable=SC2034 # chart_version_action is read by the sourcing script
plan_chart_version_action() {
  local release="$1" relation
  chart_version_action="none"
  [[ -n "$bundle_chart_version" ]] || return 0
  helm status "$release" >/dev/null 2>&1 || return 0
  read_deployed_chart_version "$release" || return 1
  relation="$(compare_chart_versions "$bundle_chart_version" "$deployed_chart_version")"
  case "$relation" in
    newer) chart_version_action="upgrade" ;;
    equal) chart_version_action="same" ;;
    older)
      print_error 'Helm release %s is at chart version %s, newer than this bundle (%s). Fix: run the deploy script from a bundle at least as new as the deployed chart; nothing was changed.\n' \
        "$release" "$deployed_chart_version" "$bundle_chart_version"
      return 1
      ;;
  esac
}
