"""Shared fixtures for the Azure LLM bootstrap scripts (`scripts/deploy-llm.sh`, GH issue #105).

Why this package lives under `ps-service/tests/deploy_llm/` rather than a root-level `tests/`
(mirrors `ps-service/tests/release/conftest.py`'s own documented deviation): the root
`pyproject.toml`'s `testpaths` covers only `ps-service/tests` and `ps-cli/tests`. Neither script
under test is `ps-service` source, but this is the only test-collected location available.

`scripts/deploy-llm.sh` takes no `--config` flag (CHANGES.md Row 4 drops it as unscoped design
work with no basis in the architecture doc or TASK.md), so a test wanting a malformed
`scripts/llm-defaults.conf` cannot point the script at an alternate path. Instead, this fixture
copies the real `scripts/deploy-llm.sh` (and, once later slices add them, `scripts/llm-
defaults.conf` / `scripts/lib/deploy-llm-common.sh`) into an isolated `tmp_path` copy of the
`scripts/` tree and runs *that* copy -- the script's own `$SCRIPT_DIR`-relative config lookup
then resolves inside the fixture, so a test edits `DeployLlmFixture.config_path` in place without
ever touching the real, checked-in defaults file or requiring a test-only flag on the script
itself. This mirrors `ps-service/tests/release/conftest.py`'s `_copy_real_version_files` pattern
of copying real repo files into a fixture working tree.

S1 (PLAN.md §5) needed no `az`/`kubectl` fakes; S2 adds the fake `az` (PLAN.md §2.2, copied
verbatim) and the naming-related `DeployLlmFixture` members. This grows further across S4-S13
as later slices need more of PLAN.md §2.4's full API (azure-state seeding helpers, the `kubectl`
fake, and so on).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# Mirrors scripts/llm-defaults.conf's shipped defaults (PLAN.md §1) -- hardcoded here rather than
# parsed from the config file, matching test_confirmation_table.py's own existing precedent of
# hardcoding these same literal values for its assertions.
DEFAULT_REGION_CANDIDATES = ("swedencentral", "francecentral", "westeurope", "germanywestcentral")
DEFAULT_CHAT_MODEL_NAME = "gpt-5.4-mini"
DEFAULT_CHAT_MODEL_SKU = "GlobalStandard"
DEFAULT_EMBED_MODEL_NAME = "text-embedding-3-large"
DEFAULT_EMBED_MODEL_SKU = "DataZoneStandard"
# Ample enough that the default seeded quota never binds against the default capacities
# (1000/350) -- tests that want quota to bind call seed_usage() themselves with tighter values.
AMPLE_QUOTA_LIMIT = 10_000
# Mirrors scripts/lib/deploy-llm-common.sh's LLM_RESOURCE_GROUP_NAME (PLAN.md §0.4) -- default
# for seed_existing_resource_group() below.
DEFAULT_RESOURCE_GROUP_NAME = "rg-policy-system-llm"


def _model_availability_entry(
    name: str, sku: str, *, is_generally_available: bool, capacity_range: tuple[int, int]
) -> dict[str, object]:
    """One `az cognitiveservices model list` response entry for a single model (PLAN.md §2.4's
    `seed_model_availability` "shape `deploy-llm.sh` is expected to parse").
    """
    minimum, maximum = capacity_range
    return {
        "model": {
            "name": name,
            "lifecycleStatus": "GenerallyAvailable" if is_generally_available else "Preview",
            "skus": [{"name": sku, "capacity": {"minimum": minimum, "maximum": maximum}}],
        }
    }


# Copied into each test's tmp_path so the script under test always finds its config/lib next to
# itself, exactly as it would in the real repo. Files land only once their owning slice has
# created them (S1: the script + config; S2 adds the shared naming lib).
DEPLOY_LLM_RELATIVE_FILES = (
    Path("scripts/deploy-llm.sh"),
    Path("scripts/llm-defaults.conf"),
    Path("scripts/lib/deploy-llm-common.sh"),
    Path("scripts/sync-llm-secrets-to-kind.sh"),
)

SUBPROCESS_TIMEOUT_SECONDS = 30.0

# Fake `az` (PLAN.md §2.2), copied verbatim -- load-bearing spec, not an implementer's paraphrase.
# Dispatches on "$1 $2" (three/four-token sub-dispatch inside `cognitiveservices account`).
# Every invocation is logged to $PS_TEST_AZ_LOG first, unconditionally, before any dispatch runs.
FAKE_AZ_SCRIPT = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$PS_TEST_AZ_LOG"

state="$PS_TEST_AZ_STATE_DIR"

# get_arg <flag> "$@" -- prints the single token following <flag>, or "".
# Only for single-value flags. A multi-value flag (--secret-permissions get list set,
# --query [].roleDefinitionName) must be asserted on the raw $PS_TEST_AZ_LOG line instead --
# this helper does not attempt to parse those.
get_arg() {
  local flag="$1"; shift
  local i
  for ((i = 1; i <= $#; i++)); do
    if [[ "${!i}" == "$flag" ]]; then
      local j=$((i + 1))
      printf '%s' "${!j}"
      return 0
    fi
  done
}

case "${1:-} ${2:-}" in
  "account show")
    if [[ "$*" == *"user.name"* ]]; then cat "$state/signed-in-user-upn"
    else cat "$state/subscription-id"
    fi
    ;;
  "ad signed-in-user") cat "$state/signed-in-user-id" ;;
  "role assignment") cat "$state/role-assignments" ;;
  "group show")
    name="$(get_arg --name "$@")"
    [[ -f "$state/resource-groups/$name" ]]
    ;;
  "group create")
    name="$(get_arg --name "$@")"; location="$(get_arg --location "$@")"
    mkdir -p "$state/resource-groups"
    printf '%s' "$location" > "$state/resource-groups/$name"
    ;;
  "cognitiveservices model")
    region="$(get_arg --location "$@")"
    cat "$state/model-availability/$region.json" 2>/dev/null || printf '[]'
    ;;
  "cognitiveservices usage")
    region="$(get_arg --location "$@")"
    cat "$state/usage/$region.json" 2>/dev/null || printf '[]'
    ;;
  "cognitiveservices account")
    verb="${3:-}"; name="$(get_arg --name "$@")"
    case "$verb" in
      show)
        [[ -f "$state/accounts/$name.json" ]]
        cat "$state/accounts/$name.json"
        ;;
      create)
        mkdir -p "$state/accounts"
        printf '{"properties":{"endpoint":"https://%s.cognitiveservices.azure.com/"}}' "$name" \
          > "$state/accounts/$name.json"
        [[ -f "$state/accounts/$name-keys.json" ]] || printf '{"key1":"%s","key2":"%s"}' \
          "${PS_TEST_AZ_INITIAL_KEY1:-FAKE-KEY-1-INITIAL}" \
          "${PS_TEST_AZ_INITIAL_KEY2:-FAKE-KEY-2-INITIAL}" > "$state/accounts/$name-keys.json"
        cat "$state/accounts/$name.json"
        ;;
      keys)
        case "${4:-}" in
          list) cat "$state/accounts/$name-keys.json" ;;
          regenerate)
            key_name="$(get_arg --key-name "$@")"
            new_value="FAKE-$(printf '%s' "$key_name" | tr '[:lower:]' '[:upper:]')-REGEN-$RANDOM"
            jq --arg k "$key_name" --arg v "$new_value" '.[$k] = $v' \
              "$state/accounts/$name-keys.json" > "$state/accounts/$name-keys.json.tmp"
            mv "$state/accounts/$name-keys.json.tmp" "$state/accounts/$name-keys.json"
            cat "$state/accounts/$name-keys.json"
            ;;
        esac
        ;;
      deployment)
        dep_name="$(get_arg --deployment-name "$@")"
        marker="$state/deployments/$name/$dep_name"
        case "${4:-}" in
          show) [[ -f "$marker" ]] ;;
          create) mkdir -p "$(dirname "$marker")"; touch "$marker" ;;
        esac
        ;;
    esac
    ;;
  "keyvault show")
    name="$(get_arg --name "$@")"
    [[ -f "$state/keyvaults/$name.json" ]]
    ;;
  "keyvault create")
    name="$(get_arg --name "$@")"
    mkdir -p "$state/keyvaults"; touch "$state/keyvaults/$name.json"
    ;;
  "keyvault set-policy")
    name="$(get_arg --name "$@")"; object_id="$(get_arg --object-id "$@")"
    mkdir -p "$state/keyvaults"
    printf '%s %s\n' "$object_id" "$*" >> "$state/keyvaults/$name-policies.log"
    ;;
  "keyvault secret")
    verb="${3:-}"; vault="$(get_arg --vault-name "$@")"
    secret_name="$(get_arg --name "$@")"
    secret_dir="$state/keyvaults/$vault-secrets"
    case "$verb" in
      show)
        [[ -f "$secret_dir/$secret_name" ]]
        jq -n --arg v "$(cat "$secret_dir/$secret_name")" '{value: $v}'
        ;;
      set)
        value="$(get_arg --value "$@")"
        mkdir -p "$secret_dir"
        printf '%s' "$value" > "$secret_dir/$secret_name"
        ;;
    esac
    ;;
  *)
    echo "fake az: unsupported invocation '$*'" >&2
    exit 2
    ;;
esac
"""

# Fake `kubectl` (S11, CHANGES.md Row 3 + Appendix A -- NOT PLAN.md §2.3's original), copied
# verbatim -- load-bearing spec, not an implementer's paraphrase. `"create secret"` is a pure,
# stateless `--dry-run=client` transform (no `$state` read/write) so it is deliberately NOT
# logged to $PS_TEST_KUBECTL_LOG: logging it would race against the concurrently-running
# "apply -f -" stage of the same pipe (both processes start at once; cross-process log order is
# not deterministic -- PLAN.md §2.3's original version reproduced this race 5/8 runs). Every
# other case still logs unconditionally before dispatching. Assert `create secret`'s output via
# `read_applied_manifest()` (the captured manifest `kubectl apply` received), not the call log.
FAKE_KUBECTL_SCRIPT = r"""#!/usr/bin/env bash
set -euo pipefail

state="$PS_TEST_KUBECTL_STATE_DIR"

case "${1:-} ${2:-}" in
  "create secret")
    # Pure, stateless transform (--dry-run=client): mutates nothing, so it
    # is deliberately NOT logged to $PS_TEST_KUBECTL_LOG -- logging it would
    # race against the concurrently-running "apply -f -" stage of the same
    # pipe (both processes start at once; log order between them is not
    # deterministic). Its output is verified via the captured manifest in
    # $PS_TEST_KUBECTL_APPLIED_DIR instead (see "apply -f" below).
    name="$4"
    printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\ntype: Opaque\nstringData:\n' \
      "$name"
    for arg in "$@"; do
      if [[ "$arg" == --from-literal=* ]]; then
        pair="${arg#--from-literal=}"
        printf '  %s: "%s"\n' "${pair%%=*}" "${pair#*=}"
      fi
    done
    ;;
  *)
    printf '%s\n' "$*" >> "$PS_TEST_KUBECTL_LOG"
    case "${1:-} ${2:-}" in
      "config current-context") cat "$state/current-context" ;;
      "config view") cat "$state/namespace" 2>/dev/null || true ;;
      "apply -f")
        manifest="$(cat)"
        secret_name="$(printf '%s\n' "$manifest" | sed -n 's/^  name: //p' | head -1)"
        namespace="$(cat "$state/namespace" 2>/dev/null || true)"
        namespace="${namespace:-default}"
        mkdir -p "$PS_TEST_KUBECTL_APPLIED_DIR"
        printf '%s\n' "$manifest" > "$PS_TEST_KUBECTL_APPLIED_DIR/${namespace}-${secret_name}.yaml"
        ;;
      *)
        echo "fake kubectl: unsupported invocation '$*'" >&2
        exit 2
        ;;
    esac
    ;;
esac
"""


@dataclass(frozen=True)
class ScriptRun:
    """Captured outcome of one script invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        """Stdout followed by stderr -- for assertions that do not care which stream."""
        return self.stdout + self.stderr


@dataclass
class DeployLlmFixture:
    """`scripts/deploy-llm.sh` copied into an isolated tree, plus the helpers PLAN.md §2.4 names.

    Grows across S1-S13 as later slices need more of it. S2 adds the fake `az` (`bin_dir`,
    `az_log`) and the `azure-state` seeding needed for naming/confirmation-table tests.
    """

    root: Path
    home: Path
    azure_state: Path
    bin_dir: Path
    az_log: Path
    kubectl_state: Path
    kubectl_applied: Path
    kubectl_log: Path

    @property
    def config_path(self) -> Path:
        """This fixture's own editable copy of `scripts/llm-defaults.conf`."""
        return self.root / "scripts" / "llm-defaults.conf"

    def _environment(self) -> dict[str, str]:
        """Environment for a script run: fake `az`/`kubectl` prepended to `PATH`, throwaway
        `HOME`, plus the `PS_TEST_AZ_*`/`PS_TEST_KUBECTL_*` variables both fakes read/write.
        Both scripts get all of these regardless of which fakes they actually call -- unused
        variables are harmless.
        """
        return {
            "PATH": os.pathsep.join([str(self.bin_dir), "/usr/bin", "/bin"]),
            "HOME": str(self.home),
            "PS_TEST_AZ_LOG": str(self.az_log),
            "PS_TEST_AZ_STATE_DIR": str(self.azure_state),
            "PS_TEST_KUBECTL_LOG": str(self.kubectl_log),
            "PS_TEST_KUBECTL_STATE_DIR": str(self.kubectl_state),
            "PS_TEST_KUBECTL_APPLIED_DIR": str(self.kubectl_applied),
        }

    def run_deploy(self, *args: str, stdin: str | None = None, expect: int | None = 0) -> ScriptRun:
        """Run this fixture's copy of `scripts/deploy-llm.sh`.

        `stdin=None` closes stdin (`/dev/null`) so an unguarded `read` fails fast instead of
        hanging; pass a string to answer a prompt. `expect=None` to inspect the code yourself
        (mirrors `ReleaseFixture.run_script`).
        """
        script = self.root / "scripts" / "deploy-llm.sh"
        argv = [str(script), *args]
        env = self._environment()
        if stdin is None:
            # No answer supplied: close stdin so an unguarded `read` fails fast (EOF) instead
            # of hanging or inheriting pytest's own stdin.
            completed = subprocess.run(  # noqa: S603 - script path is a fixture-owned copy; args are test literals
                argv,
                cwd=self.root,
                env=env,
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        else:
            completed = subprocess.run(  # noqa: S603 - script path is a fixture-owned copy; args are test literals
                argv,
                cwd=self.root,
                env=env,
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
                input=stdin,
            )
        run = ScriptRun(completed.returncode, completed.stdout, completed.stderr)
        if expect is not None:
            assert run.returncode == expect, (
                f"deploy-llm.sh {' '.join(args)} -> {run.returncode}, expected {expect}\n"
                f"--- stdout ---\n{run.stdout}--- stderr ---\n{run.stderr}"
            )
        return run

    def run_sync(self, *args: str, expect: int | None = 0) -> ScriptRun:
        """Run this fixture's copy of `scripts/sync-llm-secrets-to-kind.sh`.

        The sync script takes no interactive prompt, so stdin is always closed (mirrors
        `run_deploy`'s `stdin=None` branch). `expect=None` to inspect the code yourself.
        """
        script = self.root / "scripts" / "sync-llm-secrets-to-kind.sh"
        argv = [str(script), *args]
        env = self._environment()
        completed = subprocess.run(  # noqa: S603 - script path is a fixture-owned copy; args are test literals
            argv,
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        run = ScriptRun(completed.returncode, completed.stdout, completed.stderr)
        if expect is not None:
            assert run.returncode == expect, (
                f"sync-llm-secrets-to-kind.sh {' '.join(args)} -> {run.returncode}, "
                f"expected {expect}\n"
                f"--- stdout ---\n{run.stdout}--- stderr ---\n{run.stderr}"
            )
        return run

    def read_az_log(self) -> list[str]:
        """Every argv line the fake `az` recorded, in order."""
        return self.az_log.read_text(encoding="utf-8").splitlines() if self.az_log.exists() else []

    def read_kubectl_log(self) -> list[str]:
        """Every argv line the fake `kubectl` recorded, in order.

        Records only state-mutating invocations (`config current-context`, `config view`,
        `apply -f`) -- `create secret --dry-run=client` is deliberately unlogged (CHANGES.md
        Row 3/Appendix A); assert its output via `read_applied_manifest()` instead.
        """
        return (
            self.kubectl_log.read_text(encoding="utf-8").splitlines()
            if self.kubectl_log.exists()
            else []
        )

    def seed_subscription(
        self,
        id_: str = "11111111-2222-3333-4444-555555555555",
        upn: str = "evaluator@example.test",
        user_id: str = "22222222-3333-4444-5555-666666666666",
    ) -> None:
        """Write the three identity files the fake `az` reads for `account show`/`ad
        signed-in-user show`, plus a fully-authorized, fully-available default baseline (Owner
        role; both models Generally Available with ample capacity headroom; ample quota) across
        every configured candidate region (S4-S7, PLAN.md §5).

        Why bundle the baseline here rather than leaving every test to call
        `seed_role_assignments`/`seed_model_availability`/`seed_usage` individually: S2/S3's
        tests (`test_confirmation_table.py`, `test_decline_path.py`) already call only
        `seed_subscription()` and run the *entire* linear `main()` body -- S4-S7 each add an
        unconditional next step to that same body, so without a default happy baseline here,
        every earlier slice's test would need its own edit for every later slice added (S2's own
        precedent for this problem, IMPL_SLICE_2.md, was a one-off patch to a single test; doing
        that per-slice for every S2/S3 test here would multiply the same fix). A test exercising
        a negative/edge case overrides just the one piece it cares about by calling the specific
        `seed_role_assignments`/`seed_model_availability`/`seed_usage` method afterward (each
        fully replaces the file `seed_subscription` wrote).
        """
        self.azure_state.mkdir(parents=True, exist_ok=True)
        (self.azure_state / "subscription-id").write_text(id_, encoding="utf-8")
        (self.azure_state / "signed-in-user-upn").write_text(upn, encoding="utf-8")
        (self.azure_state / "signed-in-user-id").write_text(user_id, encoding="utf-8")
        self.seed_role_assignments("Owner")
        for region in DEFAULT_REGION_CANDIDATES:
            self.seed_model_availability(region, chat_ga=True, embed_ga=True)
            self.seed_usage(
                region,
                chat=(0, AMPLE_QUOTA_LIMIT),
                embed=(0, AMPLE_QUOTA_LIMIT),
            )

    def seed_role_assignments(self, *roles: str) -> None:
        """Write `azure-state/role-assignments` -- newline-separated role names the fake `az
        role assignment list` call returns verbatim. Empty call (`seed_role_assignments()`) means
        no roles at all (AC-BI-004's negative case) -- an empty file, per §2.1's "empty = none",
        not a missing one (the fake's `cat` would error on a genuinely absent file).
        """
        self.azure_state.mkdir(parents=True, exist_ok=True)
        content = "".join(f"{role}\n" for role in roles)
        (self.azure_state / "role-assignments").write_text(content, encoding="utf-8")

    def seed_model_availability(
        self,
        region: str,
        *,
        chat_ga: bool,
        embed_ga: bool,
        chat_capacity_range: tuple[int, int] = (1, 3000),
        embed_capacity_range: tuple[int, int] = (1, 700),
    ) -> None:
        """Write `azure-state/model-availability/<region>.json` -- the fake `az cognitiveservices
        model list --location <region>` response `deploy-llm.sh`'s region-selection probe (S5)
        and capacity-range check (S6) parse. One entry per configured model; `lifecycleStatus` is
        `GenerallyAvailable` when its `*_ga` flag is true, `Preview` otherwise.
        """
        payload = [
            _model_availability_entry(
                DEFAULT_CHAT_MODEL_NAME,
                DEFAULT_CHAT_MODEL_SKU,
                is_generally_available=chat_ga,
                capacity_range=chat_capacity_range,
            ),
            _model_availability_entry(
                DEFAULT_EMBED_MODEL_NAME,
                DEFAULT_EMBED_MODEL_SKU,
                is_generally_available=embed_ga,
                capacity_range=embed_capacity_range,
            ),
        ]
        directory = self.azure_state / "model-availability"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{region}.json").write_text(json.dumps(payload), encoding="utf-8")

    def seed_usage(self, region: str, *, chat: tuple[int, int], embed: tuple[int, int]) -> None:
        """Write `azure-state/usage/<region>.json` -- the fake `az cognitiveservices usage list
        --location <region>` response `deploy-llm.sh`'s quota check (S7) parses. `chat`/`embed`
        are each `(current_value, limit)`.
        """
        chat_current, chat_limit = chat
        embed_current, embed_limit = embed
        payload = [
            {"name": {"value": "chat"}, "currentValue": chat_current, "limit": chat_limit},
            {"name": {"value": "embed"}, "currentValue": embed_current, "limit": embed_limit},
        ]
        directory = self.azure_state / "usage"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{region}.json").write_text(json.dumps(payload), encoding="utf-8")

    def seed_existing_resource_group(
        self, name: str = DEFAULT_RESOURCE_GROUP_NAME, *, location: str = "swedencentral"
    ) -> None:
        """Pre-populate an already-existing resource group.

        Not one of PLAN.md §2.4's named `seed_existing_*` members verbatim, but a minimal,
        same-shaped extension of that table (§2.1 already specifies this exact state shape:
        `resource-groups/<name>`, content = location): proving "zero create calls" on a full
        rerun (S9's `test_rerun_makes_no_create_calls`) requires every create-if-absent target --
        the resource group included -- to already exist, not just the account/deployments/vault
        the table names.
        """
        directory = self.azure_state / "resource-groups"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(location, encoding="utf-8")

    def seed_existing_account(
        self,
        name: str,
        *,
        endpoint: str | None = None,
        key1: str = "FAKE-KEY-1-INITIAL",
        key2: str = "FAKE-KEY-2-INITIAL",
    ) -> None:
        """Pre-populate an already-existing AIServices account and its key pair (the fake `az
        cognitiveservices account keys list` source) -- for idempotency (S9) and rotation (S10)
        tests that need an account without going through a prior `deploy-llm.sh` run. Defaults
        match the fake `az`'s own `cognitiveservices account create` defaults (PLAN.md §2.2), so
        a seeded account looks like one this script itself would have just created.
        """
        resolved_endpoint = endpoint or f"https://{name}.cognitiveservices.azure.com/"
        directory = self.azure_state / "accounts"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").write_text(
            json.dumps({"properties": {"endpoint": resolved_endpoint}}), encoding="utf-8"
        )
        (directory / f"{name}-keys.json").write_text(
            json.dumps({"key1": key1, "key2": key2}), encoding="utf-8"
        )

    def seed_existing_deployment(self, account: str, name: str) -> None:
        """Pre-populate an already-existing model deployment marker (S9)."""
        directory = self.azure_state / "deployments" / account
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).touch()

    def seed_existing_keyvault(self, name: str) -> None:
        """Pre-populate an already-existing Key Vault marker (S9)."""
        directory = self.azure_state / "keyvaults"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").touch()

    def seed_existing_secret(self, vault: str, name: str, value: str) -> None:
        """Pre-populate an already-existing Key Vault secret value (S9/S10)."""
        directory = self.azure_state / "keyvaults" / f"{vault}-secrets"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(value, encoding="utf-8")

    def read_secret(self, vault: str, name: str) -> str | None:
        """Reads back `keyvaults/<vault>-secrets/<name>`, or None if never written."""
        path = self.azure_state / "keyvaults" / f"{vault}-secrets" / name
        return path.read_text(encoding="utf-8") if path.exists() else None

    def read_keyvault_policies(self, vault: str) -> list[str]:
        """Every `keyvault set-policy` line recorded for <vault>, in order."""
        path = self.azure_state / "keyvaults" / f"{vault}-policies.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def seed_kubectl_context(self, name: str) -> None:
        """Writes `kubectl-state/current-context` -- the fake `kubectl config current-context`
        response `sync-llm-secrets-to-kind.sh`'s context guard (S11) reads.
        """
        self.kubectl_state.mkdir(parents=True, exist_ok=True)
        (self.kubectl_state / "current-context").write_text(name, encoding="utf-8")

    def seed_kubectl_namespace(self, name: str) -> None:
        """Writes `kubectl-state/namespace` -- the fake `kubectl`'s resolved "active namespace"
        (S12/S13). Unseeded, the fake's `"apply -f"` case falls back to `"default"`.
        """
        self.kubectl_state.mkdir(parents=True, exist_ok=True)
        (self.kubectl_state / "namespace").write_text(name, encoding="utf-8")

    def read_applied_manifest(self, namespace: str, secret_name: str) -> str | None:
        """Reads back `kubectl-applied/<namespace>-<secret_name>.yaml` -- the manifest the fake
        `kubectl apply -f -` last captured, or None if `apply` was never called for that
        namespace/secret pair. Overwritten (not appended) on rerun -- this overwrite *is* the
        AC-BI-017 idempotent-update proof (S13).
        """
        path = self.kubectl_applied / f"{namespace}-{secret_name}.yaml"
        return path.read_text(encoding="utf-8") if path.exists() else None


def _copy_deploy_llm_files(root: Path) -> None:
    """Copy whichever of `DEPLOY_LLM_RELATIVE_FILES` currently exist into `root`.

    `copy2` preserves the executable bit, so the script/lib executable-vs-not distinction
    (scripts/deploy-llm.sh executable, scripts/lib/*.sh not) survives the copy unchanged.
    """
    for relative in DEPLOY_LLM_RELATIVE_FILES:
        source = REPO_ROOT / relative
        if not source.exists():
            continue
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


@pytest.fixture
def deploy_llm_fixture(tmp_path: Path) -> DeployLlmFixture:
    """Build an isolated copy of the `scripts/deploy-llm.sh` / `scripts/sync-llm-secrets-to-
    kind.sh` tree under `tmp_path`, plus the fake `az` (PLAN.md §2.2) and fake `kubectl`
    (CHANGES.md Row 3/Appendix A) on the same shared `bin/` directory.
    """
    _copy_deploy_llm_files(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_az = bin_dir / "az"
    fake_az.write_text(FAKE_AZ_SCRIPT, encoding="utf-8")
    fake_az.chmod(0o755)
    fake_kubectl = bin_dir / "kubectl"
    fake_kubectl.write_text(FAKE_KUBECTL_SCRIPT, encoding="utf-8")
    fake_kubectl.chmod(0o755)
    return DeployLlmFixture(
        root=tmp_path,
        home=home,
        azure_state=tmp_path / "azure-state",
        bin_dir=bin_dir,
        az_log=tmp_path / "az.log",
        kubectl_state=tmp_path / "kubectl-state",
        kubectl_applied=tmp_path / "kubectl-applied",
        kubectl_log=tmp_path / "kubectl.log",
    )
