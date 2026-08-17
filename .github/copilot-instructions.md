# Copilot Instructions

## Project Overview

This repository supports the TakT skill and is initialized with a devcontainer and minimal dependencies. It is designed to be extended with additional skills for specific tools stacks, such as Astro, Jekyll, GoLang, Python, etc.:

## Quality Gates

- Git hooks are configured via `.githooks`
- pre-commit runs `trunk-worthy` wave from `gh insitu`
- After file edits, run:
  - `gh insitu run fix-all`

This is required because AI edits bypass editor format-on-save.

## CI And Pipeline Notes

- Keep runner compatibility with ubuntu-latest
