#!/usr/bin/env bash
# Applies the chart's Authentik blueprint to a disposable Authentik of the pinned version.
#
#   scripts/verify-authentik-blueprint.sh [--twice] [--upgrade-from REV] [--print-image] [BLUEPRINT]
#   scripts/verify-authentik-blueprint.sh --start-environment
#   scripts/verify-authentik-blueprint.sh --use-environment AUTHENTIK_CONTAINER [--twice]
#                                         [--upgrade-from REV] [BLUEPRINT]
#   scripts/verify-authentik-blueprint.sh --stop-environment NETWORK
#
# BLUEPRINT  The blueprint file to apply (default: charts/policy-system/files/authentik-blueprint.yaml).
# --twice    Apply BLUEPRINT a second time into the same database and require identical row counts
#            for flows, stages, stage bindings and policy bindings.
# --upgrade-from REV
#            First apply the blueprint as it was at git revision REV, then apply BLUEPRINT on top,
#            the way an install upgraded from that revision sees it.
# --print-image
#            Print the Authentik image reference the harness would use and exit.
# --start-environment
#            Start one Postgres and one long-lived Authentik container on a private network,
#            migrate once into a template database, apply the shipped system and default
#            blueprints to it once, and print NETWORK=, POSTGRES= and AUTHENTIK= lines. The image
#            is pulled while Postgres starts. The environment stays up until --stop-environment.
# --use-environment AUTHENTIK_CONTAINER
#            Clone a fresh database from the template, run the guard against BLUEPRINT in the
#            running Authentik container, and drop the clone on every exit path. Each use starts
#            from a migrated database that holds the shipped blueprints and nothing of BLUEPRINT.
# --stop-environment NETWORK
#            Remove the environment's containers and network; safe to repeat.
#
# Without a phase option the script starts an environment, uses it once and stops it.
#
# The image tag is the authentik dependency version in charts/policy-system/Chart.yaml, so a chart
# bump moves the harness with it. The container CLI is PS_CONTAINER_CLI, else podman, else docker.
#
# The guard calls Authentik's blueprint Importer: the shipped system and default blueprints first
# (the chart blueprint patches objects they create; applied once into the template database), then
# the chart blueprint in one process per use. Both
# `validate()` and `apply()` must succeed and `validate()` must log no error, so a failure the
# worker would only log is a failure here. After the last pass the script asserts the ps-login flow
# chain, the brand pointers and the ps-cli application, and plans ps-login through Authentik's
# FlowPlanner to require that the validate stage is skipped only when the plan records a passkey
# identification. The passkey recovery and invite enrollment flows must each end, as their one
# binding after user login, on a static redirect stage to PS Service's done page /enrolled, and
# neither deny confirmation stage (ps-enrollment-confirmation, ps-recovery-confirmation) may
# exist. The brand's custom CSS must name the flow cancel path /flows/-/cancel/, which it
# hides. Every expression policy in the shipped blueprints and in BLUEPRINT must read only names
# that Authentik's policy evaluator provides.
# Exit 0 means the blueprint applies; any other exit status is a failure and the output names the
# entry. The one-shot run removes its containers and network on every exit path;
# --start-environment does so only when it fails. Each phase logs its elapsed seconds on stderr
# as "phase NAME: Ns". No credentials are needed and
# the image is a public pull.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHART_YAML="$REPO_ROOT/charts/policy-system/Chart.yaml"
DEFAULT_BLUEPRINT="$REPO_ROOT/charts/policy-system/files/authentik-blueprint.yaml"
BLUEPRINT_RELPATH="charts/policy-system/files/authentik-blueprint.yaml"
TEMPLATE_DB="authentik_template"

log() {
  echo "verify-authentik-blueprint: $*" >&2
}

die() {
  log "$*"
  exit 2
}

# Logs the seconds elapsed since the second argument (a value of $SECONDS) as the named phase.
log_phase() {
  log "phase $1: $((SECONDS - $2))s"
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
MODE="oneshot"
HANDLE=""

select_mode() {
  [ "$MODE" = "oneshot" ] || die "only one of --start-environment, --use-environment, --stop-environment may be given"
  MODE="$1"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --twice) TWICE=1 ;;
    --upgrade-from)
      [ "$#" -ge 2 ] || die "--upgrade-from needs a git revision"
      UPGRADE_FROM="$2"
      shift
      ;;
    --print-image) PRINT_IMAGE=1 ;;
    --start-environment) select_mode start ;;
    --use-environment)
      [ "$#" -ge 2 ] || die "--use-environment needs the Authentik container name"
      select_mode use
      HANDLE="$2"
      shift
      ;;
    --stop-environment)
      [ "$#" -ge 2 ] || die "--stop-environment needs the network name"
      select_mode stop
      HANDLE="$2"
      shift
      ;;
    -h | --help)
      sed -n '2,/^set -euo/{/^set -euo/!p;}' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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

if [ "$MODE" = "start" ] || [ "$MODE" = "stop" ]; then
  [ -z "$BLUEPRINT" ] || die "--$MODE-environment takes no BLUEPRINT"
  [ "$TWICE" -eq 0 ] && [ -z "$UPGRADE_FROM" ] || die "--$MODE-environment takes no --twice or --upgrade-from"
else
  BLUEPRINT="${BLUEPRINT:-$DEFAULT_BLUEPRINT}"
  [ -f "$BLUEPRINT" ] || die "blueprint not found: $BLUEPRINT"
  BLUEPRINT="$(cd "$(dirname "$BLUEPRINT")" && pwd)/$(basename "$BLUEPRINT")"
fi

CLI="${PS_CONTAINER_CLI:-$(command -v podman || command -v docker || true)}"
[ -n "$CLI" ] || die "no container CLI found (set PS_CONTAINER_CLI, or install podman or docker)"

NETWORK=""
PG=""
AK=""
OWNS_ENVIRONMENT=0
CLONE_DB=""
PULL_PID=""
WORKDIR="$(mktemp -d)"

name_environment() {
  NETWORK="$1"
  PG="$NETWORK-pg"
  AK="$NETWORK-server"
}

remove_environment() {
  log "removing environment $NETWORK"
  "$CLI" rm -f "$AK" >/dev/null 2>&1 || true
  "$CLI" rm -f "$PG" >/dev/null 2>&1 || true
  "$CLI" network rm "$NETWORK" >/dev/null 2>&1 || true
}

drop_clone() {
  log "dropping database $CLONE_DB"
  "$CLI" exec "$PG" psql -U authentik -d postgres -c "DROP DATABASE IF EXISTS $CLONE_DB WITH (FORCE)" >/dev/null 2>&1 || true
  "$CLI" exec "$AK" sh -c "rm -f /tmp/$CLONE_DB-*.yaml" >/dev/null 2>&1 || true
}

cleanup() {
  if [ -n "$PULL_PID" ]; then
    kill "$PULL_PID" >/dev/null 2>&1 || true
  fi
  if [ -n "$CLONE_DB" ]; then
    drop_clone
  fi
  if [ "$OWNS_ENVIRONMENT" -eq 1 ]; then
    remove_environment
  fi
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

write_guard_script() {
  cat >"$WORKDIR/guard.py" <<'PY'
import builtins
import os
import symtable
import sys

from django.contrib.messages import get_messages
from django.contrib.messages.storage.cookie import CookieStorage
from django.db.models.signals import post_save, pre_delete
from django.test import RequestFactory

from authentik.blueprints.v1.common import BlueprintEntryDesiredState
from authentik.blueprints.v1.importer import Importer
from authentik.brands.models import Brand
from authentik.core.management.commands.shell import Command as ShellCommand
from authentik.core.models import Application, User
from authentik.flows.markers import ReevaluateMarker
from authentik.flows.models import Flow, FlowStageBinding, Stage
from authentik.flows.planner import PLAN_CONTEXT_PENDING_USER, FlowPlanner
from authentik.lib.expression.evaluator import sanitize_arg
from authentik.policies.expression.evaluator import PolicyEvaluator
from authentik.policies.expression.models import ExpressionPolicy
from authentik.policies.models import PolicyBinding
from authentik.policies.types import PolicyRequest
from authentik.providers.oauth2.models import OAuth2Provider
from authentik.stages.deny.models import DenyStage
from authentik.stages.redirect.models import RedirectStage
from authentik.stages.password.stage import PLAN_CONTEXT_METHOD

# The shell command records an audit event for every row it saves, which nothing here reads and
# which costs about half the apply time; the importer does not depend on those events.
post_save.disconnect(ShellCommand.post_save_handler)
pre_delete.disconnect(ShellCommand.pre_delete_handler)

SHIPPED_ONLY = os.environ["GUARD_MODE"] == "shipped"
TWICE = os.environ.get("GUARD_TWICE") == "1"
SEED = os.environ.get("GUARD_SEED") or None
TARGET = os.environ.get("GUARD_TARGET")

VALIDATE_ORDER = 30
DONE_PATH = "/enrolled"
PASSKEY_AUTH_METHOD = "auth_webauthn_pwl"
NO_AUTH_METHOD = object()
# Every value here must leave the validate stage in the plan: no value at all, empty values, the
# "auth_password" literal, every other value authentik's backends, stages and token endpoints
# write, and a near miss of the passkey value.
AUTH_METHODS_THAT_KEEP_VALIDATION = (
    NO_AUTH_METHOD,
    None,
    "",
    "auth_password",
    "password",
    "auth_mfa",
    "ldap",
    "kerberos",
    "mtls",
    "trusted_endpoint",
    "token",
    "jwt",
    "oauth_client_secret",
    "token_exchange",
    "AUTH_WEBAUTHN_PWL",
)
EXPRESSION_POLICY_MODEL = "authentik_policies_expression.expressionpolicy"


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


def http_request_for(user):
    request = RequestFactory().get("/if/flow/ps-login/")
    request.user = user
    # Policy results are reported as django messages; without a message storage on the request,
    # add_message raises as soon as a policy emits one. The cookie backend needs no session.
    request._messages = CookieStorage(request)
    return request


def plan_from_validate_stage(login_flow, user, request):
    planner = FlowPlanner(login_flow)
    planner.use_cache = False
    plan = planner.plan(request, {PLAN_CONTEXT_PENDING_USER: user})
    while plan.bindings and plan.bindings[0].order < VALIDATE_ORDER:
        plan.pop()
    if not plan.bindings or plan.bindings[0].order != VALIDATE_ORDER:
        fail(f"the ps-login plan has no validate stage at order {VALIDATE_ORDER}")
    if not isinstance(plan.markers[0], ReevaluateMarker):
        fail("the ps-login validate stage is planned without re-evaluation, so its skip policy never runs")
    return plan


def describe_auth_method(auth_method):
    return "absent" if auth_method is NO_AUTH_METHOD else repr(auth_method)


def describe_validate_stage(shown):
    return "shown" if shown else "skipped"


def validate_stage_after_identification(login_flow, user, auth_method):
    request = http_request_for(user)
    plan = plan_from_validate_stage(login_flow, user, request)
    if auth_method is not NO_AUTH_METHOD:
        plan.context[PLAN_CONTEXT_METHOD] = auth_method
    next_binding = plan.next(request)
    shown = next_binding is not None and next_binding.order == VALIDATE_ORDER
    return shown, [str(message) for message in get_messages(request)]


def check_validate_skip(login_flow, user):
    cases = [(PASSKEY_AUTH_METHOD, False)] + [(method, True) for method in AUTH_METHODS_THAT_KEEP_VALIDATION]
    for auth_method, expected_shown in cases:
        shown, messages = validate_stage_after_identification(login_flow, user, auth_method)
        method = describe_auth_method(auth_method)
        if shown != expected_shown:
            fail(
                f"ps-login validate stage {describe_validate_stage(shown)} with auth_method={method}, "
                f"expected {describe_validate_stage(expected_shown)}; policy messages: {messages}"
            )
        if messages:
            fail(f"ps-login validate-stage policy emitted messages with auth_method={method}: {messages}")
    print("PASS ps-login validate stage is skipped only after a passkey identification")


def expression_policies_defined_by(path):
    with open(path, encoding="utf-8") as handle:
        blueprint = Importer.from_string(handle.read()).blueprint
    policies = []
    for entry in blueprint.iter_entries():
        if entry.get_model(blueprint) != EXPRESSION_POLICY_MODEL:
            continue
        if entry.get_state(blueprint) == BlueprintEntryDesiredState.ABSENT:
            continue
        if not entry.check_all_conditions_match(blueprint):
            continue
        identifiers = entry.get_identifiers(blueprint)
        policy = ExpressionPolicy.objects.filter(**identifiers).first()
        if policy is None:
            fail(f"{path} defines expression policy {identifiers} but no applied row matches it")
        policies.append(policy)
    return policies


def evaluator_for(policy, user):
    # Built the way ExpressionPolicy.passes builds it, for a policy evaluated inside a flow: the
    # HTTP request is set, which adds http_request and ak_client_ip. Flow and stage bindings
    # always carry it, and ak_call_policy copies the caller's request; a policy evaluated with no
    # HTTP request (for example from an event rule) lacks those two names.
    evaluator = PolicyEvaluator(policy.name)
    evaluator.policy = policy
    request = PolicyRequest(user)
    request.set_http_request(http_request_for(user))
    evaluator.set_policy_request(request)
    return evaluator


def names_the_evaluator_provides(evaluator):
    # _globals and _context are the evaluator's own name tables; reading them keeps the check in
    # step with the pinned authentik instead of a hand-written list.
    context_names = {sanitize_arg(key) for key in evaluator._context}
    return set(evaluator._globals) | context_names | set(vars(builtins))


def global_names_referenced_in(table):
    names = {symbol.get_name() for symbol in table.get_symbols() if symbol.is_global() and symbol.is_referenced()}
    for child in table.get_children():
        names |= global_names_referenced_in(child)
    return names


def names_read_by(evaluator, policy):
    # Free names are read from the compiled scopes rather than found by evaluating the policy,
    # because a name in a branch that does not run never raises.
    module = symtable.symtable(evaluator.wrap_expression(policy.expression), policy.name, "exec")
    (handler_scope,) = [child for child in module.get_children() if child.get_name() == "handler"]
    return global_names_referenced_in(handler_scope)


def undefined_names_by_policy(policies, user):
    found = []
    for policy in policies:
        evaluator = evaluator_for(policy, user)
        undefined = sorted(names_read_by(evaluator, policy) - names_the_evaluator_provides(evaluator))
        if undefined:
            found.append((policy.name, undefined))
    return found


def describe_undefined_names(found):
    return "; ".join(f"{name}: {', '.join(names)}" for name, names in found)


def check_shipped_expression_policy_names(paths, user):
    policies = [policy for path in paths for policy in expression_policies_defined_by(path)]
    if not policies:
        fail("no shipped expression policies to check the name check against")
    flagged = undefined_names_by_policy(policies, user)
    if flagged:
        fail(f"the name check flags shipped expression policies: {describe_undefined_names(flagged)}")
    print(f"PASS shipped expression policies read only names authentik provides ({len(policies)} checked)")


def check_expression_policy_names(path, user):
    policies = expression_policies_defined_by(path)
    flagged = undefined_names_by_policy(policies, user)
    if flagged:
        fail(
            "; ".join(
                f"expression policy {name} reads names authentik does not provide: {', '.join(names)}"
                for name, names in flagged
            )
        )
    print(f"PASS expression policies read only names authentik provides ({len(policies)} checked)")


shipped = []
for directory in ("/blueprints/system", "/blueprints/default"):
    shipped += [os.path.join(directory, name) for name in sorted(os.listdir(directory)) if name.endswith(".yaml")]
if SHIPPED_ONLY:
    for path in shipped:
        apply_file(path)
    sys.exit(0)
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
pending_user = User.objects.create(username="guard-pending-user")
check_shipped_expression_policy_names(shipped, pending_user)
check_expression_policy_names(TARGET, pending_user)

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
if "/flows/-/cancel/" not in (brand.branding_custom_css or ""):
    fail("brand branding_custom_css does not hide links to /flows/-/cancel/")
application = Application.objects.filter(slug="ps-cli").first()
if application is None or not OAuth2Provider.objects.filter(client_id="ps-cli").exists():
    fail("the ps-cli application or its provider is missing")
print("PASS ps-login chain, brand pointers and ps-cli application")
print("PASS brand css hides flow cancel links")
check_validate_skip(flow, pending_user)
retired = ["ps-recovery-confirmation", "ps-enrollment-confirmation"]
if DenyStage.objects.filter(name__in=retired).exists():
    fail(f"a retired deny confirmation stage still exists: {retired}")
for slug, orders, login_order in (
    ("ps-passkey-recovery", [10, 100, 110], 100),
    ("ps-invite-enrollment", [5, 10, 20, 30, 100, 110], 100),
):
    target = Flow.objects.filter(slug=slug).first()
    if target is None:
        fail(f"flow {slug} is missing")
    chain = list(FlowStageBinding.objects.filter(target=target).order_by("order"))
    if [b.order for b in chain] != orders:
        fail(f"{slug} binding orders are {[b.order for b in chain]}, expected {orders}")
    after_login = [b for b in chain if b.order > login_order]
    if len(after_login) != 1:
        fail(f"{slug} has {len(after_login)} bindings after user login, expected exactly 1")
    done = RedirectStage.objects.filter(pk=after_login[0].stage_id).first()
    if done is None or done.mode != "static" or done.target_static != DONE_PATH:
        fail(f"{slug} does not end on a static redirect to {DONE_PATH}")
    print(f"PASS {slug} ends on the done-page redirect")
PY
}

wait_for_postgres() {
  local _
  for _ in $(seq 1 60); do
    if "$CLI" exec "$PG" pg_isready -h 127.0.0.1 -U authentik -d "$TEMPLATE_DB" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  die "postgres did not become ready"
}

start_environment() {
  local began=$SECONDS phase_began
  name_environment "ps-ak-$$-$RANDOM"
  OWNS_ENVIRONMENT=1
  log "starting environment $NETWORK"
  write_guard_script
  "$CLI" pull --quiet "$IMAGE" >/dev/null &
  PULL_PID=$!
  "$CLI" network create "$NETWORK" >/dev/null
  "$CLI" run -d --name "$PG" --network "$NETWORK" \
    -e POSTGRES_USER=authentik -e POSTGRES_PASSWORD=x -e "POSTGRES_DB=$TEMPLATE_DB" \
    postgres:17-alpine >/dev/null
  wait_for_postgres
  log_phase "postgres ready" "$began"
  phase_began=$SECONDS
  wait "$PULL_PID" || die "pulling $IMAGE failed"
  PULL_PID=""
  log_phase "image pull (after postgres ready)" "$phase_began"
  "$CLI" run -d --name "$AK" --network "$NETWORK" \
    -e AUTHENTIK_SECRET_KEY=x \
    -e "AUTHENTIK_POSTGRESQL__HOST=$PG" \
    -e AUTHENTIK_POSTGRESQL__USER=authentik \
    -e AUTHENTIK_POSTGRESQL__PASSWORD=x \
    --entrypoint sleep "$IMAGE" infinity >/dev/null
  "$CLI" cp "$WORKDIR/guard.py" "$AK:/guard.py" >/dev/null
  log "migrating $IMAGE"
  phase_began=$SECONDS
  "$CLI" exec -e "AUTHENTIK_POSTGRESQL__NAME=$TEMPLATE_DB" "$AK" python -m lifecycle.migrate \
    >"$WORKDIR/migrate.log" 2>&1 \
    || {
      tail -n 40 "$WORKDIR/migrate.log" >&2
      die "database migration failed"
    }
  log_phase "migrate" "$phase_began"
  phase_began=$SECONDS
  log "applying the shipped blueprints to $TEMPLATE_DB"
  "$CLI" exec -e "AUTHENTIK_POSTGRESQL__NAME=$TEMPLATE_DB" -e GUARD_MODE=shipped "$AK" \
    python -m manage shell -c "exec(open('/guard.py').read(), {})" \
    >"$WORKDIR/shipped.log" 2>&1 \
    || {
      tail -n 40 "$WORKDIR/shipped.log" >&2
      die "applying the shipped blueprints failed"
    }
  log_phase "shipped blueprints" "$phase_began"
  log_phase "environment total" "$began"
}

stage_blueprints() {
  local seed=""
  "$CLI" cp "$BLUEPRINT" "$AK:/tmp/$CLONE_DB-target.yaml" >/dev/null
  if [ -n "$UPGRADE_FROM" ]; then
    git -C "$REPO_ROOT" show "$UPGRADE_FROM:$BLUEPRINT_RELPATH" >"$WORKDIR/seed.yaml" \
      || die "cannot read $BLUEPRINT_RELPATH at $UPGRADE_FROM"
    "$CLI" cp "$WORKDIR/seed.yaml" "$AK:/tmp/$CLONE_DB-seed.yaml" >/dev/null
    seed="/tmp/$CLONE_DB-seed.yaml"
  fi
  GUARD_SEED_PATH="$seed"
}

use_environment() {
  local network="${HANDLE%-server}"
  [ "$network" != "$HANDLE" ] || die "not an Authentik environment container name: $HANDLE"
  name_environment "$network"
  local began=$SECONDS phase_began
  CLONE_DB="guard_$$_$RANDOM"
  log "cloning database $CLONE_DB from $TEMPLATE_DB"
  "$CLI" exec "$PG" psql -U authentik -d postgres -v ON_ERROR_STOP=1 \
    -c "CREATE DATABASE $CLONE_DB TEMPLATE $TEMPLATE_DB" >/dev/null
  stage_blueprints
  log_phase "clone and stage" "$began"
  phase_began=$SECONDS
  log "applying $BLUEPRINT"
  "$CLI" exec \
    -e "AUTHENTIK_POSTGRESQL__NAME=$CLONE_DB" \
    -e GUARD_MODE=verify -e "GUARD_TWICE=$TWICE" -e "GUARD_SEED=$GUARD_SEED_PATH" \
    -e "GUARD_TARGET=/tmp/$CLONE_DB-target.yaml" \
    "$AK" python -m manage shell -c "exec(open('/guard.py').read(), {})" \
    || {
      local status=$?
      log_phase "guard (failed)" "$phase_began"
      return "$status"
    }
  log_phase "guard" "$phase_began"
}

print_environment() {
  echo "NETWORK=$NETWORK"
  echo "POSTGRES=$PG"
  echo "AUTHENTIK=$AK"
}

case "$MODE" in
  start)
    start_environment
    OWNS_ENVIRONMENT=0
    print_environment
    ;;
  use) use_environment ;;
  stop)
    name_environment "$HANDLE"
    remove_environment
    ;;
  oneshot)
    start_environment
    HANDLE="$AK"
    use_environment
    ;;
esac
