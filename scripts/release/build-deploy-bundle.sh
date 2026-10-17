#!/usr/bin/env bash
# Builds the deploy bundle: the few repo files the installation guide runs (evaluator steps 4-7 and
# the production path), so nobody has to clone the repo to install (issue #337 follow-up).
#
# Usage: build-deploy-bundle.sh <image_tag> <out_dir>
#
# Writes <out_dir>/ps-deploy-bundle.tgz. The archive unpacks into one `policy-system-deploy/` folder
# that keeps the repo-relative layout (`deploy/kind/cluster.yaml`, `scripts/...`, `charts/policy-system/values-prod.yaml`), so every command
# in the guide is identical to a run from a clone. A `VERSION` file holds <image_tag>; scripts/
# deploy-ps-eval.sh and deploy-ps-prod.sh read it to install the chart of the same release instead of the latest one.
# Must run from the repository root.
set -euo pipefail

readonly EXIT_USAGE=2
USAGE="usage: $(basename "$0") <image_tag> <out_dir>"
readonly USAGE
readonly BUNDLE_DIR="policy-system-deploy"
readonly BUNDLE_FILES=(
  deploy/kind/cluster.yaml
  scripts/deploy-llm.sh
  scripts/llm-defaults.conf
  scripts/sync-llm-secrets-to-kind.sh
  scripts/deploy-ps-eval.sh
  scripts/lib/deploy-llm-common.sh
  scripts/lib/authentik-owner.sh
  scripts/lib/local-tls.sh
  scripts/lib/chart-version.sh
  scripts/deploy-ps-prod.sh
  scripts/ps-defaults.conf
  scripts/ps-aks.sh
  scripts/ps-upgrade.sh
  scripts/ps-reset-data.sh
  scripts/grant-access-role.sh
  scripts/grant_access_role_client.py
  ps-cli/install.sh
  charts/policy-system/values-prod.yaml
)

if [[ $# -ne 2 || -z "$1" || -z "$2" ]]; then
  printf '%s\n' "$USAGE" >&2
  exit "$EXIT_USAGE"
fi
image_tag="$1"
out_dir="$2"

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT

for file in "${BUNDLE_FILES[@]}"; do
  mkdir -p "$staging/$BUNDLE_DIR/$(dirname "$file")"
  cp -p "$file" "$staging/$BUNDLE_DIR/$file"
done
printf '%s\n' "$image_tag" >"$staging/$BUNDLE_DIR/VERSION"

mkdir -p "$out_dir"
tar -czf "$out_dir/ps-deploy-bundle.tgz" -C "$staging" "$BUNDLE_DIR"
printf 'Wrote %s/ps-deploy-bundle.tgz (version %s)\n' "$out_dir" "$image_tag" >&2
