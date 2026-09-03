#!/usr/bin/env bash
# Static verification for AC-BI-006 (independent upgrade, no cross-restart)
# plus CHANGES.md M1's credential-leak check (folded in here as a second
# function, per S14's "Implement's choice" — named explicitly below).
#
# AC-BI-006's real claim ("the *other* component doesn't restart") needs a
# live cluster this environment doesn't have. What this script proves
# instead is the structural precondition: overriding only falkordb.* values
# leaves the rendered ps-service-deployment.yaml document byte-for-byte
# identical to the baseline, and vice versa — so `helm upgrade` targeting
# one component's values has literally nothing to change in the other's
# manifest, which is what would cause Kubernetes to leave it alone.
#
# M1's check proves a value only ever renders inside its own Secret
# document, never leaks into any other rendered document.
set -euo pipefail

CHART_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/charts/policy-system"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

HELM="${HELM_BIN:-helm}"
if ! command -v "$HELM" >/dev/null 2>&1 && [ -x "$HOME/.local/bin/helm" ]; then
  HELM="$HOME/.local/bin/helm"
fi

# extract_doc <rendered-file> <template-basename> <out-file>
# Pulls one Helm-rendered document out of a multi-document `helm template`
# output by matching Helm's own `# Source: policy-system/templates/<file>`
# comment, up to (not including) the next `---` document separator.
extract_doc() {
  local rendered_file="$1" template_name="$2" out_file="$3"
  awk -v src="# Source: policy-system/templates/${template_name}" '
    $0 == src { capture=1; next }
    /^---/ { if (capture) exit }
    capture { print }
  ' "$rendered_file" > "$out_file"
  if [ ! -s "$out_file" ]; then
    echo "verify-chart-independence: FAILED — could not find document for templates/${template_name} in ${rendered_file}" >&2
    exit 1
  fi
}

# check_independence: the AC-BI-006 structural-decoupling proof.
check_independence() {
  local baseline="$WORKDIR/baseline.yaml"
  local falkordb_changed="$WORKDIR/falkordb-changed.yaml"
  local psservice_changed="$WORKDIR/psservice-changed.yaml"

  "$HELM" template "$CHART_DIR" > "$baseline"
  "$HELM" template "$CHART_DIR" \
    --set falkordb.image.tag=9.9.9 \
    --set falkordb.persistence.enabled=true \
    > "$falkordb_changed"
  "$HELM" template "$CHART_DIR" \
    --set psService.image.tag=9.9.9 \
    > "$psservice_changed"

  local base_ps_service="$WORKDIR/base-ps-service-deployment.yaml"
  local changed_ps_service="$WORKDIR/changed-ps-service-deployment.yaml"
  extract_doc "$baseline" "ps-service-deployment.yaml" "$base_ps_service"
  extract_doc "$falkordb_changed" "ps-service-deployment.yaml" "$changed_ps_service"

  if ! diff -u "$base_ps_service" "$changed_ps_service"; then
    echo "verify-chart-independence: FAILED — overriding only falkordb.* values changed ps-service-deployment.yaml's rendered output (AC-BI-006 violated)" >&2
    exit 1
  fi

  local base_falkordb_deploy="$WORKDIR/base-falkordb-deployment.yaml"
  local changed_falkordb_deploy="$WORKDIR/changed-falkordb-deployment.yaml"
  extract_doc "$baseline" "falkordb-deployment.yaml" "$base_falkordb_deploy"
  extract_doc "$psservice_changed" "falkordb-deployment.yaml" "$changed_falkordb_deploy"

  if ! diff -u "$base_falkordb_deploy" "$changed_falkordb_deploy"; then
    echo "verify-chart-independence: FAILED — overriding only psService.* values changed falkordb-deployment.yaml's rendered output (AC-BI-006 violated)" >&2
    exit 1
  fi

  local base_falkordb_svc="$WORKDIR/base-falkordb-service.yaml"
  local changed_falkordb_svc="$WORKDIR/changed-falkordb-service.yaml"
  extract_doc "$baseline" "falkordb-service.yaml" "$base_falkordb_svc"
  extract_doc "$psservice_changed" "falkordb-service.yaml" "$changed_falkordb_svc"

  if ! diff -u "$base_falkordb_svc" "$changed_falkordb_svc"; then
    echo "verify-chart-independence: FAILED — overriding only psService.* values changed falkordb-service.yaml's rendered output (AC-BI-006 violated)" >&2
    exit 1
  fi

  echo "verify-chart-independence: PASS — falkordb.* and psService.* value overrides are structurally decoupled"
}

# check_no_credential_leak: CHANGES.md M1 — no helm-unittest assertion can
# scan a full multi-document render for a credential leak, so this shells
# out to grep: a distinctive sentinel value passed as the Azure API key must
# appear ONLY inside the rendered secret.yaml document, nowhere else in the
# full multi-document output.
check_no_credential_leak() {
  local sentinel="ZZ_TEST_SENTINEL_AZURE_KEY_ZZ"
  local out
  out="$("$HELM" template "$CHART_DIR" \
    --set llm.provider=azure \
    --set llm.azure.apiKey="$sentinel" \
    --set llm.azure.apiBase=https://example.test/)"

  local total in_secret
  total="$(grep -c "$sentinel" <<<"$out")"
  in_secret="$(awk '/^# Source: policy-system\/templates\/secret.yaml$/{f=1} /^---/{f=0} f' <<<"$out" | grep -c "$sentinel")"

  if [ "$total" -ne "$in_secret" ]; then
    echo "verify-chart-independence: FAILED — credential sentinel appears outside the Secret document ($total total occurrences, $in_secret inside secret.yaml)" >&2
    exit 1
  fi

  echo "verify-chart-independence: PASS — credential value renders only inside templates/secret.yaml ($total occurrence(s))"
}

check_independence
check_no_credential_leak
