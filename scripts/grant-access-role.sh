#!/usr/bin/env bash
# Grants an AccessRole to a principal via PS Service's `grant-access-role` MCP tool, calling its
# Streamable HTTP endpoint directly with the bearer token `ps-cli auth login` stored.
#
# Usage:
#   scripts/grant-access-role.sh [--principal <subject>] [--role <AccessRole>]
#
#   --principal  The principal_subject to grant to (default tete@cartman.dk -- the evaluator deploy
#                uses the owner's email as the OIDC `sub`).
#   --role       SystemOwner | SystemAdmin | PolicyManager | ComplianceOfficer (default
#                ComplianceOfficer).
#
# Targets ps-cli's current context (`ps-cli config get-contexts`; `eval` = local kind). Needs
# `ps-cli auth login` for it and a SystemOwner caller. GRANT_ACCESS_ROLE_TOKEN, when set, is the
# bearer token instead of the stored credential. Never available under the local-test bypass.
#
# Exit codes: 2 usage error, 1 tool/transport failure, 0 granted.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

principal="tete@cartman.dk"
role="ComplianceOfficer"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --principal) principal="${2:?--principal needs a value}"; shift 2 ;;
    --role) role="${2:?--role needs a value}"; shift 2 ;;
    -h | --help) sed -n '2,15p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

case "${role}" in
  SystemOwner | SystemAdmin | PolicyManager | ComplianceOfficer) ;;
  *) printf 'Invalid --role %s (SystemOwner|SystemAdmin|PolicyManager|ComplianceOfficer)\n' "${role}" >&2; exit 2 ;;
esac

command -v jq >/dev/null || { printf 'jq is required.\n' >&2; exit 1; }

client=(uv run --quiet --project "${REPO_ROOT}/ps-cli" python -I "${SCRIPT_DIR}/grant_access_role_client.py")
if ! reply="$("${client[@]}" --principal "${principal}" --role "${role}")"; then
  exit 1 # the client already wrote the reason to stderr
fi

if [[ -z "${reply}" ]]; then
  printf "No reply to grant-access-role (is PS Service reachable and are you logged in? run 'ps-cli auth login').\n" >&2
  exit 1
fi

# A tool error arrives either as a JSON-RPC error or as result.isError / an "error: ..." text.
text="$(jq -r '.error.message // (.result.content // [] | map(.text // "") | join("\n"))' <<<"${reply}")"
is_error="$(jq -r '(.error != null) or (.result.isError // false)' <<<"${reply}")"
if [[ "${is_error}" == "true" || "${text}" == error:* ]]; then
  printf 'Grant failed: %s\n' "${text}" >&2
  exit 1
fi

printf 'Granted %s to %s\n%s\n' "${role}" "${principal}" "${text}"
