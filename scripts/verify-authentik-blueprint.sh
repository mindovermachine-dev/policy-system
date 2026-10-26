#!/usr/bin/env bash
# Applies the chart's Authentik blueprint to a disposable Authentik of the pinned version.
#
#   scripts/verify-authentik-blueprint.sh [--twice] [--upgrade-from REV] [--print-image] [BLUEPRINT]
#
# BLUEPRINT  The blueprint file to apply (default: charts/policy-system/files/authentik-blueprint.yaml).
# --twice    Apply BLUEPRINT a second time into the same database and require identical row counts
#            for flows, stages, stage bindings and policy bindings.
# --upgrade-from REV
#            First apply the blueprint as it was at git revision REV, then apply BLUEPRINT on top,
#            the way an install upgraded from that revision sees it.
# --print-image
#            Print the Authentik image reference the harness would use and exit.
#
# The image tag is the authentik dependency version in charts/policy-system/Chart.yaml, so a chart
# bump moves the harness with it. The container CLI is PS_CONTAINER_CLI, else podman, else docker.
#
# It starts a throwaway Postgres on a private network, migrates a fresh Authentik database, then
# calls Authentik's blueprint Importer in one process: the shipped system and default blueprints
# first (the chart blueprint patches objects they create), then the chart blueprint. Both
# `validate()` and `apply()` must succeed and `validate()` must log no error, so a failure the
# worker would only log is a failure here. After the last pass the script asserts the ps-login flow
# chain, the brand pointers and the ps-cli application. Exit 0 means the blueprint applies; any
# other exit status is a failure and the output names the entry. Containers and the network are
# removed on every exit path. No credentials are needed and the image is a public pull.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHART_YAML="$REPO_ROOT/charts/policy-system/Chart.yaml"
DEFAULT_BLUEPRINT="$REPO_ROOT/charts/policy-system/files/authentik-blueprint.yaml"
BLUEPRINT_RELPATH="charts/policy-system/files/authentik-blueprint.yaml"

die() {
  echo "verify-authentik-blueprint: $*" >&2
  exit 2
}

authentik_version() {
  awk '
    /^dependencies:/ { in_deps = 1; next }
    in_deps && /^  - name:/ { name = $3 }
    in_deps && name == "authentik" && /^    version:/ { print $2; exit }
  ' "$CHART_YAML"
}

TWICE=0
UPGRADE_FROM=""
PRINT_IMAGE=0
BLUEPRINT=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --twice) TWICE=1 ;;
    --upgrade-from)
      [ "$#" -ge 2 ] || die "--upgrade-from needs a git revision"
      UPGRADE_FROM="$2"
      shift
      ;;
    --print-image) PRINT_IMAGE=1 ;;
    -h | --help)
      sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    -*) die "unknown option: $1" ;;
    *)
      [ -z "$BLUEPRINT" ] || die "only one BLUEPRINT may be given"
      BLUEPRINT="$1"
      ;;
  esac
  shift
done

VERSION="$(authentik_version)"
[ -n "$VERSION" ] || die "no authentik dependency version found in $CHART_YAML"
IMAGE="ghcr.io/goauthentik/server:$VERSION"
if [ "$PRINT_IMAGE" -eq 1 ]; then
  echo "$IMAGE"
  exit 0
fi

BLUEPRINT="${BLUEPRINT:-$DEFAULT_BLUEPRINT}"
[ -f "$BLUEPRINT" ] || die "blueprint not found: $BLUEPRINT"
BLUEPRINT="$(cd "$(dirname "$BLUEPRINT")" && pwd)/$(basename "$BLUEPRINT")"

CLI="${PS_CONTAINER_CLI:-$(command -v podman || command -v docker || true)}"
[ -n "$CLI" ] || die "no container CLI found (set PS_CONTAINER_CLI, or install podman or docker)"

WORKDIR="$(mktemp -d)"
NETWORK="ps-ak-$$"
PG="ps-ak-pg-$$"
cleanup() {
  "$CLI" rm -f "$PG" >/dev/null 2>&1 || true
  "$CLI" network rm "$NETWORK" >/dev/null 2>&1 || true
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

SEED_MOUNT=()
SEED_PRESENT=0
if [ -n "$UPGRADE_FROM" ]; then
  git -C "$REPO_ROOT" show "$UPGRADE_FROM:$BLUEPRINT_RELPATH" >"$WORKDIR/seed.yaml" \
    || die "cannot read $BLUEPRINT_RELPATH at $UPGRADE_FROM"
  SEED_MOUNT=(-v "$WORKDIR/seed.yaml:/blueprints/custom/seed.yaml:ro,Z")
  SEED_PRESENT=1
fi

cat >"$WORKDIR/guard.py" <<'PY'
import os
import sys

from authentik.blueprints.v1.importer import Importer
from authentik.brands.models import Brand
from authentik.core.models import Application
from authentik.flows.models import Flow, FlowStageBinding, Stage
from authentik.stages.deny.models import DenyStage
from authentik.policies.models import PolicyBinding
from authentik.providers.oauth2.models import OAuth2Provider

TWICE = os.environ["GUARD_TWICE"] == "1"
SEED = "/blueprints/custom/seed.yaml" if os.environ["GUARD_SEED"] == "1" else None
TARGET = "/blueprints/custom/target.yaml"


def fail(message):
    print(f"FAIL {message}")
    sys.exit(1)


def apply_file(path):
    with open(path, encoding="utf-8") as handle:
        importer = Importer.from_string(handle.read())
    ok, logs = importer.validate()
    errors = [log for log in logs if log.log_level == "error"]
    if not ok or errors:
        detail = "; ".join(f"{log.event} {log.attributes}" for log in errors) or "validate() returned False"
        fail(f"{path} validate: {detail}")
    if not importer.apply():
        fail(f"{path} apply() returned False")
    print(f"OK {path}")


def counts():
    return {
        "flows": Flow.objects.count(),
        "stages": Stage.objects.count(),
        "stage_bindings": FlowStageBinding.objects.count(),
        "policy_bindings": PolicyBinding.objects.count(),
    }


shipped = []
for directory in ("/blueprints/system", "/blueprints/default"):
    shipped += [os.path.join(directory, name) for name in sorted(os.listdir(directory)) if name.endswith(".yaml")]
for path in shipped:
    apply_file(path)
if SEED:
    apply_file(SEED)
apply_file(TARGET)
first = counts()
print(f"COUNTS {first}")
if TWICE:
    apply_file(TARGET)
    second = counts()
    print(f"COUNTS {second}")
    if second != first:
        fail(f"row counts changed on re-apply: {first} -> {second}")

flow = Flow.objects.filter(slug="ps-login").first()
if flow is None:
    fail("flow ps-login is missing")
bindings = list(FlowStageBinding.objects.filter(target=flow).order_by("order"))
if [b.order for b in bindings] != [10, 25, 30, 100]:
    fail(f"ps-login binding orders are {[b.order for b in bindings]}, expected [10, 25, 30, 100]")
deny = PolicyBinding.objects.filter(target=bindings[1])
if deny.count() != 1 or deny.first().negate or deny.first().policy.name != "ps-login-reputation-policy":
    fail("the order-25 binding does not hold the non-negated reputation policy binding")
brand = Brand.objects.get(domain="authentik-default")
if brand.flow_authentication_id != flow.pk or brand.flow_device_code_id != flow.pk:
    fail("brand flow_authentication and flow_device_code are not ps-login")
application = Application.objects.filter(slug="ps-cli").first()
if application is None or not OAuth2Provider.objects.filter(client_id="ps-cli").exists():
    fail("the ps-cli application or its provider is missing")
print("PASS ps-login chain, brand pointers and ps-cli application")
for slug, orders in (
    ("ps-passkey-recovery", [10, 100, 110]),
    ("ps-invite-enrollment", [5, 10, 20, 30, 100, 110]),
):
    target = Flow.objects.filter(slug=slug).first()
    if target is None:
        fail(f"flow {slug} is missing")
    chain = list(FlowStageBinding.objects.filter(target=target).order_by("order"))
    if [b.order for b in chain] != orders:
        fail(f"{slug} binding orders are {[b.order for b in chain]}, expected {orders}")
    confirmation = DenyStage.objects.filter(pk=chain[-1].stage_id).first()
    if confirmation is None or "ps-cli login" not in confirmation.deny_message:
        fail(f"{slug} does not end on the confirmation deny stage")
print("PASS recovery and enrollment flows end on the confirmation stage")
PY

"$CLI" network create "$NETWORK" >/dev/null
"$CLI" run -d --name "$PG" --network "$NETWORK" \
  -e POSTGRES_USER=authentik -e POSTGRES_PASSWORD=x -e POSTGRES_DB=authentik \
  postgres:17-alpine >/dev/null
ready=0
for _ in $(seq 1 60); do
  if "$CLI" exec "$PG" pg_isready -U authentik -d authentik >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[ "$ready" -eq 1 ] || die "postgres did not become ready"

ak_run() {
  "$CLI" run --rm --network "$NETWORK" \
    -e AUTHENTIK_SECRET_KEY=x \
    -e "AUTHENTIK_POSTGRESQL__HOST=$PG" \
    -e AUTHENTIK_POSTGRESQL__USER=authentik \
    -e AUTHENTIK_POSTGRESQL__PASSWORD=x \
    -e AUTHENTIK_POSTGRESQL__NAME=authentik \
    -e "GUARD_TWICE=$TWICE" -e "GUARD_SEED=$SEED_PRESENT" \
    -v "$BLUEPRINT:/blueprints/custom/target.yaml:ro,Z" \
    -v "$WORKDIR/guard.py:/guard.py:ro,Z" \
    ${SEED_MOUNT[@]+"${SEED_MOUNT[@]}"} \
    --entrypoint python "$@"
}

echo "verify-authentik-blueprint: migrating $IMAGE" >&2
ak_run "$IMAGE" -m lifecycle.migrate >"$WORKDIR/migrate.log" 2>&1 \
  || {
    tail -n 40 "$WORKDIR/migrate.log" >&2
    die "database migration failed"
  }
echo "verify-authentik-blueprint: applying $BLUEPRINT" >&2
ak_run "$IMAGE" -m manage shell -c "exec(open('/guard.py').read(), {})"
