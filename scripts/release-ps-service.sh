#!/usr/bin/env bash
# Cuts a ps-service release: checks out main, bumps the semver tag, and
# pushes it — the tag push is what triggers .github/workflows/on_semver.yml
# to build and publish the ps-service container to GHCR. See CONTRIBUTING.md
# "Releasing" for the manual flow this automates.
#
# Usage:
#   scripts/release-ps-service.sh              # bump minor, confirm, push
#   scripts/release-ps-service.sh --major       # bump major instead
#   scripts/release-ps-service.sh --patch       # bump patch instead
#   scripts/release-ps-service.sh --dry-run     # preview the tag, push nothing
#   scripts/release-ps-service.sh --yes         # skip the push confirmation
#
# Requires: gh, the gh-tt extension, a clean working tree, and push access
# to origin.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

LEVEL="minor"
DRY_RUN=0
ASSUME_YES=0

for arg in "$@"; do
  case "${arg}" in
    --major) LEVEL="major" ;;
    --minor) LEVEL="minor" ;;
    --patch) LEVEL="patch" ;;
    --dry-run) DRY_RUN=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    *)
      echo "Usage: $(basename "${BASH_SOURCE[0]}") [--major|--minor|--patch] [--dry-run] [--yes]" >&2
      exit 2
      ;;
  esac
done

cd "${REPO_ROOT}"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree is not clean — commit or stash changes before releasing." >&2
  exit 1
fi

echo "Checking out main and syncing tags..."
git checkout main
git pull --tags

echo "Current ps-service version: $(gh tt semver)"

if (( DRY_RUN )); then
  echo "Dry run — previewing ${LEVEL} bump, nothing will be created or pushed:"
  gh tt semver bump --"${LEVEL}" --no-run
  exit 0
fi

echo "Bumping ${LEVEL}..."
gh tt semver bump --"${LEVEL}"

NEW_TAG="$(git tag --points-at HEAD | grep -v '^ps-cli-v' | head -1)"
if [[ -z "${NEW_TAG}" ]]; then
  echo "Could not determine the new tag at HEAD after bump — aborting before push." >&2
  exit 1
fi

echo "Created tag ${NEW_TAG} locally (not yet pushed)."

if (( ! ASSUME_YES )); then
  read -r -p "Push ${NEW_TAG} to origin? This triggers the release build and is irreversible. [y/N] " reply
  if [[ ! "${reply}" =~ ^[Yy]$ ]]; then
    echo "Not pushing. Remove the local tag with: git tag -d ${NEW_TAG}"
    exit 0
  fi
fi

echo "Pushing ${NEW_TAG}..."
git push origin "${NEW_TAG}"
echo "Pushed. Track the build in on_semver.yml, then verify with:"
echo "  docker buildx imagetools inspect ghcr.io/mindovermachine-dev/ps-service:${NEW_TAG}"
