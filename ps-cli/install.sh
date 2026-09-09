#!/usr/bin/env bash
# Installs ps-cli from this repo's main branch via `uv tool install`.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/ps-cli/install.sh | bash
set -euo pipefail

REPO_URL="https://github.com/mindovermachine-dev/policy-system"

if ! command -v uv >/dev/null 2>&1; then
  echo "ps-cli requires uv, which was not found on PATH." >&2
  echo "Install it first: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

echo "Installing ps-cli from ${REPO_URL} (main)..."
uv tool install "git+${REPO_URL}#subdirectory=ps-cli"

echo
ps-cli --version
