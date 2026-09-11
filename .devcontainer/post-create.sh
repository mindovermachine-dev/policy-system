#!/usr/bin/env bash
# Devcontainer postCreateCommand.
# Installs the gh-insitu extension and runs the project's post-create setup.
# Requires GitHub auth (GH_TOKEN forwarded from the host via remoteEnv).
# If auth is missing we warn and exit 0 so the container still comes up usable.
set -uo pipefail

# Optional per-developer setup, kept outside the repo on the host home mount
# (/localhome). No-op for anyone who doesn't have the file.
if [ -x /localhome/.devcontainer-local/post-create.sh ]; then
  bash /localhome/.devcontainer-local/post-create.sh
fi

if ! gh auth status >/dev/null 2>&1; then
  cat >&2 <<'MSG'

==============================================================================
 post-create: GitHub CLI is not authenticated — skipping gh-insitu setup.

 GH_TOKEN is forwarded from your host shell (see devcontainer.json remoteEnv),
 so it was probably not exported when VS Code was launched.

 To finish setup inside the container, run:
   gh auth login
   gh ext install devx-cafe/gh-insitu
   gh insitu run post-create

 Or export GH_TOKEN on the host (e.g. `export GH_TOKEN=$(gh auth token)`),
 restart VS Code from that shell, and rebuild the container.
==============================================================================

MSG
  exit 0
fi

set -e
if gh ext list 2>/dev/null | grep -q 'devx-cafe/gh-insitu'; then
  gh ext upgrade devx-cafe/gh-insitu
else
  gh ext install devx-cafe/gh-insitu
fi
gh insitu run post-create
