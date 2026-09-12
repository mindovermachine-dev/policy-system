#!/usr/bin/env bash
# Installs ps-cli from a GitHub Release via `uv tool install`.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/ps-cli/install.sh | bash
set -euo pipefail

REPO="mindovermachine-dev/policy-system"

# Developer path (AC-BI-007): installs directly from a git ref, entirely skipping release
# resolution, `curl`, and checksum verification below -- checked first, before anything else,
# so it never touches that machinery at all.
if [[ -n "${PS_CLI_REF:-}" ]]; then
  uv tool install "git+https://github.com/${REPO}@${PS_CLI_REF}#subdirectory=ps-cli"
  exit 0
fi

API_BASE="https://api.github.com/repos/${REPO}"

# Validated before anything else touches the network (AC-BI-005): an operator who typos
# PS_CLI_VERSION should never trigger a download attempt first.
if [[ -n "${PS_CLI_VERSION:-}" ]] && ! [[ "$PS_CLI_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "PS_CLI_VERSION must be in X.Y.Z format (e.g. 1.2.3); got: ${PS_CLI_VERSION}" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "ps-cli requires uv, which was not found on PATH." >&2
  echo "Install it first: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

if [[ -n "${PS_CLI_VERSION:-}" ]]; then
  release_url="${API_BASE}/releases/tags/${PS_CLI_VERSION}"
else
  release_url="${API_BASE}/releases/latest"
fi

echo "Resolving ps-cli release from ${release_url}..."
release_json="$(curl -fsSL "$release_url")"

wheel_url="$(printf '%s' "$release_json" \
  | grep -oE '"browser_download_url": *"[^"]*\.whl"' \
  | sed -E 's/^"browser_download_url": *"//; s/"$//')"
sha_url="$(printf '%s' "$release_json" \
  | grep -oE '"browser_download_url": *"[^"]*SHA256SUMS"' \
  | sed -E 's/^"browser_download_url": *"//; s/"$//')"

if [[ -z "$wheel_url" || -z "$sha_url" ]]; then
  echo "Could not find a wheel and SHA256SUMS asset in the release response from ${release_url}." >&2
  exit 1
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

wheel_filename="$(basename "$wheel_url")"
wheel_path="${workdir}/${wheel_filename}"
sha_path="${workdir}/SHA256SUMS"

echo "Downloading ${wheel_filename}..."
curl -fsSL -o "$wheel_path" "$wheel_url"
curl -fsSL -o "$sha_path" "$sha_url"

# AC-BI-008: no `jq`/`gh` required -- hashing below picks whichever of `sha256sum` (Linux) /
# `shasum -a 256` (macOS) this machine actually has.
if command -v sha256sum >/dev/null 2>&1; then
  sha256_command=(sha256sum)
else
  sha256_command=(shasum -a 256)
fi

expected_line="$(grep -F "$wheel_filename" "$sha_path" | head -n1)"
expected_hash="$(printf '%s' "$expected_line" | awk '{print $1}')"
actual_hash="$("${sha256_command[@]}" "$wheel_path" | awk '{print $1}')"

# AC-BI-006: this check runs, and fails, strictly before the `uv tool install` line below is
# ever reached -- straight-line script order, not a conditional that could be bypassed.
if [[ -z "$expected_hash" || "$expected_hash" != "$actual_hash" ]]; then
  echo "Checksum mismatch for ${wheel_filename}: expected ${expected_hash:-<none found in SHA256SUMS>}, got ${actual_hash}." >&2
  exit 1
fi

# Installs from the already-downloaded local wheel file, not the URL, so `uv` itself makes
# no network call of its own (CHANGES.md §1a).
echo "Installing ${wheel_filename}..."
uv tool install "$wheel_path"

echo
ps-cli --version
