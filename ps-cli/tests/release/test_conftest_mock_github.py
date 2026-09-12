"""Slice 2 smoke test for the fake-`curl` test harness (GH issue #81, CHANGES.md X-01).

`ps-cli/tests/release/conftest.py`'s `fake_curl`/`wheel_builder` fixtures are the
infrastructure every later `install.sh` slice (3-9) trusts; per PLAN.md §0's own framing for
infra slices, an untested fixture every later slice trusts is exactly the kind of foundation
that must be provable on its own before anything is built on top of it.

This test drives the fake `curl` exactly the two ways the rewritten `install.sh` (Slice 3)
will: a metadata fetch with no `-o` (`curl -fsSL "<releases/latest URL>"`), then a file
download with `-o <dest> <url>` using the `browser_download_url` the metadata handed back --
and checks the downloaded bytes are the real, session-built wheel's bytes via a real
`sha256sum`/`shasum -a 256` comparison (never a plain byte-equality assert), per the task's
explicit ask that this test prove the fixture's plumbing the same way `install.sh` itself will
exercise it.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from conftest import FakeCurl, WheelBuilder

_CURL_TIMEOUT_SECONDS = 30.0
_FAKE_VERSION = "9.9.9"


def _run_fake_curl(*args: str, fake_curl: FakeCurl) -> subprocess.CompletedProcess[str]:
    """Invoke `curl <args>` with `fake_curl`'s bin dir prepended to `PATH`, as `install.sh` will."""
    environment = {
        **os.environ,
        "PATH": os.pathsep.join([str(fake_curl.bin_dir), os.environ.get("PATH", "")]),
        **fake_curl.environment,
    }
    return subprocess.run(  # noqa: S603 - args are test literals, never shell text
        ["curl", *args],  # noqa: S607 - intentional: PATH search must find the fixture's fake curl
        env=environment,
        capture_output=True,
        text=True,
        timeout=_CURL_TIMEOUT_SECONDS,
        check=False,
    )


def test_fake_curl_serves_latest_release_json_and_wheel_bytes(
    tmp_path: Path,
    fake_curl: FakeCurl,
    wheel_builder: WheelBuilder,
    sha256_hexdigest: Callable[[Path], str],
) -> None:
    artifact = wheel_builder.build(_FAKE_VERSION)
    fake_curl.set_latest(_FAKE_VERSION, artifact.wheel_bytes, artifact.sha256sums_bytes)

    metadata = _run_fake_curl(
        "-fsSL",
        "https://api.github.com/repos/mindovermachine-dev/policy-system/releases/latest",
        fake_curl=fake_curl,
    )
    assert metadata.returncode == 0, metadata.stderr
    body = json.loads(metadata.stdout)
    assert body["tag_name"] == _FAKE_VERSION

    wheel_asset = next(asset for asset in body["assets"] if asset["name"].endswith(".whl"))
    assert wheel_asset["name"] == artifact.wheel_filename

    dest = tmp_path / "downloaded.whl"
    download = _run_fake_curl(
        "-fsSL", "-o", str(dest), wheel_asset["browser_download_url"], fake_curl=fake_curl
    )
    assert download.returncode == 0, download.stderr
    assert dest.is_file()

    expected_hash = artifact.sha256sums_bytes.decode("utf-8").split()[0]
    assert sha256_hexdigest(dest) == expected_hash
