#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

base_ref=origin/main
if ! git rev-parse --verify --quiet "$base_ref^{commit}" >/dev/null; then
  printf 'basedpyright: %s is unavailable; fetch origin/main before committing\n' "$base_ref" >&2
  exit 1
fi

files=()
while IFS= read -r -d '' file; do
  case "$file" in
    *.py|*.pyi)
      [[ -f "$file" ]] && files+=("$file")
      ;;
  esac
done < <(git diff --name-only --diff-filter=ACMRT -z "$base_ref" --)

while IFS= read -r -d '' file; do
  case "$file" in
    *.py|*.pyi) files+=("$file") ;;
  esac
done < <(git ls-files --others --exclude-standard -z)

if [[ ${#files[@]} -eq 0 ]]; then
  printf 'basedpyright: no changed Python files since %s; skipping\n' "$base_ref"
  exit 0
fi

uv run --no-sync basedpyright "${files[@]}"