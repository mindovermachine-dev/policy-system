#!/usr/bin/env bash
# Resolve every third-party `uses:` reference in .github against the GitHub API.
# actionlint cannot do this — it makes no network calls, so a ref like
# `astral-sh/setup-uv@v10` (a major tag that was never published) passes it and
# then fails the workflow at action-resolution time.
set -euo pipefail

status=0

while IFS= read -r ref; do
  case "$ref" in
    ./* | docker://*) continue ;;  # local composite action / container image
  esac

  spec="${ref%%@*}"
  rev="${ref#*@}"
  repo="$(printf '%s\n' "$spec" | cut -d/ -f1,2)"

  if [ "$spec" = "$rev" ] || [ -z "$rev" ]; then
    echo "✗ ${ref} — no @ref pinned"
    status=1
    continue
  fi

  if gh api "repos/${repo}/commits/${rev}" --jq .sha >/dev/null 2>&1; then
    echo "✓ ${ref}"
  else
    echo "✗ ${ref} — ref '${rev}' does not exist in ${repo}"
    status=1
  fi
done < <(
  grep -rhoE '^[[:space:]]*-?[[:space:]]*uses:[[:space:]]*[^[:space:]#]+' \
    .github/workflows .github/actions 2>/dev/null \
  | sed -E 's/.*uses:[[:space:]]*//' \
  | sort -u
)

exit "$status"
