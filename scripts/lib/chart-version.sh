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
