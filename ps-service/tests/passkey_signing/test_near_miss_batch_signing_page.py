"""The signing page for a near-miss merge batch.

The summary route returns every merge the officer is about to approve. The static page lists
them, waits for an explicit sign click, and never headlines "Approved" for a batch with a failed
merge. The behavioural cases run the page's own script under `node` with a minimal fake DOM.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient
from graph_cleanup._fakes import FakeApprovalStore
from graph_cleanup._gateway_fakes import CleanupRig
from graph_cleanup._near_miss_batch_worlds import (
    seed_capability_pair_with_review,
    seed_role_pair_with_review,
)
from graph_gateway._fakes import close_all_rigs

from passkey_signing._fakes import FakeSigningCredentialStore
from passkey_signing._harness import ACTOR_ISSUER, ACTOR_SUBJECT
from ps_service.api.dependencies import provide_pending_approval_store
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.near_miss_batch import create_near_miss_merge_batch
from ps_service.logging import configure
from ps_service.main import create_app
from ps_service.passkey_signing.router import provide_signing_credential_store

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_ACTOR = (ACTOR_SUBJECT, ACTOR_ISSUER)
_HOSTILE = "<img src=x onerror=alert(1)>"
_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=True,
    authentik_api_token="t",
    authentik_base_url="https://authentik.example.com",
)
_NODE = shutil.which("node")

_NODE_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const [scriptPath, scenarioPath] = process.argv.slice(2);
const scenario = JSON.parse(fs.readFileSync(scenarioPath, "utf8"));
const htmlWrites = [];

function strip(html) { return String(html).replace(/<[^>]*>/g, ""); }

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.listeners = {};
    this.own = "";
    this.attrs = {};
  }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  set textContent(value) { this.own = String(value); this.children = []; }
  get textContent() { return this.own + this.children.map((c) => c.textContent).join(" "); }
  set innerHTML(html) { htmlWrites.push(String(html)); this.own = strip(html); this.children = []; }
  find(tag, found) {
    if (this.tag === tag) { found.push(this); }
    this.children.forEach((c) => c.find(tag, found));
    return found;
  }
}

const app = new Element("main");
const calls = [];
const sandbox = {
  document: {
    getElementById: () => app,
    createElement: (tag) => new Element(tag),
    createTextNode: (text) => { const e = new Element("#text"); e.own = String(text); return e; },
  },
  window: { location: { pathname: "/approvals/abc", hash: "#thecode" } },
  navigator: { credentials: { get: async () => ({
    id: "cred", type: "public-key", rawId: new ArrayBuffer(4),
    response: { clientDataJSON: new ArrayBuffer(2), authenticatorData: new ArrayBuffer(2),
      signature: new ArrayBuffer(2) } }) } },
  fetch: async (url) => {
    const suffix = String(url).replace("/approvals/abc", "");
    calls.push(suffix);
    const body = scenario.responses[suffix];
    return { ok: body !== undefined, json: async () => body };
  },
  atob, btoa, Uint8Array, ArrayBuffer, Error, JSON, console,
};
sandbox.window.document = sandbox.document;
const tick = () => new Promise((resolve) => setTimeout(resolve, 40));

(async () => {
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(scriptPath, "utf8"), sandbox);
  await tick();
  const buttons = app.find("button", []);
  const before = { calls: calls.slice(), buttons: buttons.length, text: app.textContent };
  if (scenario.click && buttons.length > 0) {
    buttons[0].listeners.click();
    await tick();
  }
  process.stdout.write(JSON.stringify({
    before, calls, buttons: buttons.length, text: app.textContent, htmlWrites,
  }));
})();
"""


class _Page:
    def __init__(self, summary: dict[str, object], sign_result: dict[str, object]) -> None:
        self.responses: dict[str, object] = {
            "/summary": summary,
            "/sign/options": {"publicKey": {"challenge": "AAAA", "allowCredentials": []}},
            "/sign/verify": sign_result,
        }


def _script_of(html: str) -> str:
    found = re.search(r"<script>(.*)</script>", html, re.DOTALL)
    assert found is not None
    return found.group(1)


def _run_page(
    tmp_path: Path,
    *,
    summary: dict[str, object],
    sign_result: dict[str, object],
    click: bool = True,
) -> dict[str, object]:
    assert _NODE is not None
    html = TestClient(create_app(_CONFIG)).get("/approvals/anything").text
    (tmp_path / "page.js").write_text(_script_of(html), encoding="utf-8")
    (tmp_path / "harness.js").write_text(_NODE_HARNESS, encoding="utf-8")
    page = _Page(summary, sign_result)
    (tmp_path / "scenario.json").write_text(
        json.dumps({"responses": page.responses, "click": click}), encoding="utf-8"
    )
    completed = subprocess.run(  # noqa: S603
        [
            _NODE,
            str(tmp_path / "harness.js"),
            str(tmp_path / "page.js"),
            str(tmp_path / "scenario.json"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return cast("dict[str, object]", json.loads(completed.stdout))


def _batch_summary(*, hostile_name: bool = False) -> dict[str, object]:
    merges = [
        {
            "review_id": "review_cap_1",
            "kind": "Capability",
            "acknowledged": False,
            "survivor_id": "cap_s1",
            "survivor_name": _HOSTILE if hostile_name else "Survivor 1",
            "absorbed_id": "cap_a1",
            "absorbed_name": "Absorbed 1",
            "policy_case": 1,
        },
        {
            "review_id": "review_role_2",
            "kind": "Role",
            "survivor_id": "role_in2",
            "survivor_name": "Operator 2",
            "absorbed_id": "role_ex2",
            "absorbed_name": "Operators 2",
        },
    ]
    return {
        "status": "pending",
        "needs_enrollment": False,
        "display_summary": {"kind": "near-miss-merge-batch", "merges": merges},
    }


def _entry(review_id: str, state: str, error: str | None = None) -> dict[str, object]:
    return {
        "review_id": review_id,
        "kind": "Capability",
        "state": state,
        "winner_id": "w" if state == "merged" else None,
        "loser_id": "l" if state == "merged" else None,
        "error": error,
    }


def _signed(*entries: dict[str, object]) -> dict[str, object]:
    failed = sum(1 for e in entries if e["state"] != "merged")
    return {
        "status": "signed",
        "merges": list(entries),
        "merged_count": len(entries) - failed,
        "failed_count": failed,
    }


@pytest.fixture
def batch_client() -> Iterator[tuple[TestClient, str, str]]:
    configure()
    rig = CleanupRig()
    pair = seed_capability_pair_with_review(rig, 1)
    role_pair = seed_role_pair_with_review(rig, 2)
    pending = FakeApprovalStore()
    approval = create_near_miss_merge_batch(
        rig.read,
        review_ids=[pair.review_id, role_pair.review_id],
        acknowledged_review_ids=[],
        actor=_ACTOR,
        base_url="https://ps.example.com",
        store=pending,
    )
    credentials = FakeSigningCredentialStore()
    app = create_app(_CONFIG)
    app.dependency_overrides[provide_pending_approval_store] = lambda: pending
    app.dependency_overrides[provide_signing_credential_store] = lambda: credentials
    code = approval.approval_url.rsplit("#", 1)[1]
    yield TestClient(app), approval.pending_approval_id, code
    close_all_rigs()


def test_the_summary_of_a_batch_approval_lists_every_merge(
    batch_client: tuple[TestClient, str, str],
) -> None:
    client, approval_id, code = batch_client

    body = client.post(f"/approvals/{approval_id}/summary", json={"code": code}).json()

    summary = cast("dict[str, object]", body["display_summary"])
    merges = cast("list[dict[str, object]]", summary["merges"])
    assert summary["kind"] == "near-miss-merge-batch"
    assert [m["review_id"] for m in merges] == ["review_cap_1", "review_role_2"]
    assert merges[0]["survivor_name"] == "Survivor 1"
    assert merges[0]["absorbed_name"] == "Absorbed 1"
    assert merges[1]["kind"] == "Role"


def test_the_shell_is_byte_identical_for_a_batch_and_for_an_unknown_approval(
    batch_client: tuple[TestClient, str, str],
) -> None:
    client, approval_id, _code = batch_client

    assert client.get(f"/approvals/{approval_id}").text == client.get("/approvals/unknown").text


def test_the_shell_renders_batch_entries_with_text_content_and_not_inner_html() -> None:
    html = TestClient(create_app(_CONFIG)).get("/approvals/anything").text
    script = _script_of(html)

    assert "near-miss-merge-batch" in script
    assert "textContent" in script
    assert "createElement" in script
    assert "survivor_name" in script
    for line in script.splitlines():
        if "survivor_name" in line or "absorbed_name" in line or "merge.error" in line:
            assert "innerHTML" not in line
            assert "render(" not in line


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_the_shell_gates_the_ceremony_behind_a_button_for_a_batch(tmp_path: Path) -> None:
    result = _run_page(
        tmp_path, summary=_batch_summary(), sign_result=_signed(_entry("review_cap_1", "merged"))
    )

    before = cast("dict[str, object]", result["before"])
    assert before["calls"] == ["/summary"]
    assert before["buttons"] == 1
    text = cast("str", before["text"])
    assert "review_cap_1" in text
    assert "review_role_2" in text
    assert "Survivor 1" in text
    assert "Absorbed 1" in text
    assert "/sign/verify" in cast("list[str]", result["calls"])


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_other_approval_kinds_still_start_the_ceremony_without_a_click(tmp_path: Path) -> None:
    summary: dict[str, object] = {
        "status": "pending",
        "needs_enrollment": False,
        "display_summary": {"kind": "capability-merge"},
    }

    result = _run_page(tmp_path, summary=summary, sign_result={"status": "signed"}, click=False)

    assert "/sign/verify" in cast("list[str]", result["calls"])
    assert "Approved" in cast("str", result["text"])


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_a_fully_merged_batch_is_headlined_approved(tmp_path: Path) -> None:
    result = _run_page(
        tmp_path,
        summary=_batch_summary(),
        sign_result=_signed(_entry("review_cap_1", "merged"), _entry("review_role_2", "merged")),
    )

    assert "Approved" in cast("str", result["text"])
    assert "were not carried out" not in cast("str", result["text"])


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_a_batch_with_a_failed_item_is_never_headlined_approved(tmp_path: Path) -> None:
    result = _run_page(
        tmp_path,
        summary=_batch_summary(),
        sign_result=_signed(
            _entry("review_cap_1", "merged"), _entry("review_role_2", "failed", "stale")
        ),
    )

    text = cast("str", result["text"])
    assert "Approved" not in text
    assert "1 of 2 merges were not carried out" in text


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_each_failed_merge_is_named_with_its_review_id(tmp_path: Path) -> None:
    result = _run_page(
        tmp_path,
        summary=_batch_summary(),
        sign_result=_signed(
            _entry("review_cap_1", "failed", "gone"), _entry("review_role_2", "merged")
        ),
    )

    text = cast("str", result["text"])
    assert "review_cap_1" in text
    assert "failed" in text.lower()


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_a_graph_name_with_markup_is_shown_as_text_and_never_written_as_html(
    tmp_path: Path,
) -> None:
    result = _run_page(
        tmp_path,
        summary=_batch_summary(hostile_name=True),
        sign_result=_signed(_entry("review_cap_1", "merged"), _entry("review_role_2", "merged")),
    )

    before = cast("dict[str, object]", result["before"])
    assert _HOSTILE in cast("str", before["text"])
    assert not any("onerror" in w for w in cast("list[str]", result["htmlWrites"]))
