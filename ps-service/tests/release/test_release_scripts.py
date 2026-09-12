"""File-mode invariants over `scripts/release/` (CHANGES X-10).

Every script directly invoked by a workflow step is executable; every `lib/*.sh` is sourced
only and must not be. `os.access(..., os.X_OK)` reads the working tree, so an untracked new
script is checked too (where `git ls-files -s` would pass vacuously).
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_SCRIPTS_DIR = REPO_ROOT / "scripts" / "release"
LIB_DIR = RELEASE_SCRIPTS_DIR / "lib"
BASH_SHEBANG = "#!/usr/bin/env bash"


def test_every_non_lib_release_script_is_executable_and_lib_files_are_not() -> None:
    scripts = sorted(RELEASE_SCRIPTS_DIR.glob("*.sh"))
    libraries = sorted(LIB_DIR.glob("*.sh"))
    assert scripts, f"no scripts found under {RELEASE_SCRIPTS_DIR}"
    assert libraries, f"no libraries found under {LIB_DIR}"

    not_executable = [path.name for path in scripts if not os.access(path, os.X_OK)]
    executable_libraries = [path.name for path in libraries if os.access(path, os.X_OK)]

    assert not not_executable, f"release scripts missing the executable bit: {not_executable}"
    assert not executable_libraries, (
        f"sourced libraries must not be executable: {executable_libraries}"
    )


def test_every_release_script_starts_with_the_bash_shebang_and_strict_mode() -> None:
    scripts = sorted(RELEASE_SCRIPTS_DIR.glob("*.sh"))
    assert scripts, f"no scripts found under {RELEASE_SCRIPTS_DIR}"

    for path in scripts:
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == BASH_SHEBANG, f"{path.name} does not start with `{BASH_SHEBANG}`"
        assert "set -euo pipefail" in lines, f"{path.name} does not enable strict mode"
