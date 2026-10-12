#!/usr/bin/env bash
# Grants an AccessRole to a principal via PS Service's `grant-access-role` MCP tool, by driving the
# ps-cli stdio MCP bridge (which reuses the credentials `ps-cli auth login` already stored).
#
# Usage:
#   scripts/grant-access-role.sh [--principal <subject>] [--role <AccessRole>]
#
#   --principal  The principal_subject to grant to (default tete@cartman.dk -- the evaluator deploy
#                uses the owner's email as the OIDC `sub`).
#   --role       SystemOwner | SystemAdmin | PolicyManager | ComplianceOfficer (default
#                ComplianceOfficer).
#
# Targets ps-cli's current context (`ps-cli config get-contexts`; `eval` = local kind).
# Prerequisites: `ps-cli auth login` done for that context, and the caller holds SystemOwner (a
# SystemOwner may grant themselves any role). Access-role management is never available under the
# local-test bypass.
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

bridge=(uv run --quiet --project "${REPO_ROOT}/ps-cli" ps-cli-mcp-bridge)

init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"grant-access-role.sh","version":"1"}}}'
initialized='{"jsonrpc":"2.0","method":"notifications/initialized"}'
call="$(jq -nc --arg p "${principal}" --arg r "${role}" \
  '{jsonrpc:"2.0",id:2,method:"tools/call",params:{name:"grant-access-role",arguments:{principal_subject:$p,access_role:$r}}}')"

reply="$(printf '%s\n%s\n%s\n' "${init}" "${initialized}" "${call}" | "${bridge[@]}" | jq -c 'select(.id == 2)')"

if [[ -z "${reply}" ]]; then
  printf 'No reply to grant-access-role (is PS Service reachable and are you logged in? see ~/.config/ps-cli/mcp-bridge.log).\n' >&2
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
