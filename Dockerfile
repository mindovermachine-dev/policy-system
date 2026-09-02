# PS Service runtime image (issue #60).
#
# Multi-stage on purpose: uv, the wheel-build scratch and the workspace manifests all live in
# the builder and never reach the runtime layer, which receives only the resolved virtualenv
# (AC-BI-003 -- no dev dependencies, no test fixtures, no source tree in the shipped image).
#
# Build context is the repository root: `uv.lock` and `[tool.uv.workspace]` live there, so the
# whole workspace must be visible to `uv sync`. `.dockerignore` reduces that context to the
# five files/directories the build actually consumes.

# ---- builder ---------------------------------------------------------------------------
FROM python:3.14-slim-trixie AS builder

# uv is copied in rather than installed, so the builder and the runtime share one interpreter
# provenance. 0.12.4 is the version pinned in CI (`.github/actions/prep-runner/action.yml`).
COPY --from=ghcr.io/astral-sh/uv:0.12.4 /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Layer 1 -- the dependency closure only, so it is invalidated by a manifest/lock change and
# never by a source edit. `ps-cli/pyproject.toml` is copied because uv loads every declared
# workspace member. `--frozen` makes a stale lock fail the build instead of re-resolving.
COPY pyproject.toml uv.lock ./
COPY ps-service/pyproject.toml ps-service/pyproject.toml
COPY ps-cli/pyproject.toml ps-cli/pyproject.toml
RUN uv sync --package ps-service --frozen --no-dev --no-install-workspace

# Layer 2 -- the ps-service source, installed as a real built wheel. `--no-editable` is what
# makes AC-BI-003 structural rather than arranged: the runtime stage copies the virtualenv and
# nothing else, so no source tree or test module can exist in the image at all.
COPY ps-service/src ps-service/src
RUN uv sync --package ps-service --frozen --no-dev --no-editable

# ---- runtime ---------------------------------------------------------------------------
FROM python:3.14-slim-trixie

# A static high uid/gid avoids collision with distro users and is stable for a Kubernetes
# `runAsUser` (#59). `/var/log/ps-service` is not optional: `logging/facade.py`'s
# `_find_repo_root` walks upward for a `.git` directory and raises when it finds none, so
# without a writable `PS_LOGGING_DIR` the container would crash on startup.
RUN groupadd --system --gid 10001 ps \
 && useradd --system --uid 10001 --gid ps --no-create-home --shell /usr/sbin/nologin ps \
 && mkdir -p /var/log/ps-service \
 && chown ps:ps /var/log/ps-service

COPY --from=builder --chown=ps:ps /app/.venv /app/.venv

# `PS_SERVICE_HOST=0.0.0.0` lives here and nowhere else (AC-BI-008): the source default stays
# loopback-only and `config._parse_host` still refuses to widen on a bad value (D-6). The
# container is the one context where binding every interface is correct and deliberate.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PS_SERVICE_HOST=0.0.0.0 \
    PS_SERVICE_PORT=8000 \
    PS_LOGGING_DIR=/var/log/ps-service

USER ps
WORKDIR /app
EXPOSE 8000

# Exec form, and `python -m ps_service` because `ps-service/pyproject.toml` declares no
# `[project.scripts]` entry point. PID 1 is then the Python process itself, so uvicorn's own
# SIGTERM handling receives the signal and no init shim is needed.
CMD ["python", "-m", "ps_service"]
