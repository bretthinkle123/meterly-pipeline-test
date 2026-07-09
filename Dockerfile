# syntax=docker/dockerfile:1
#
# Multi-stage build: Poetry/build tooling never reaches the runtime image.
# Base pinned by digest (python:3.12-slim) for reproducible, scannable builds
# (`containerization-conventions`); update the digest deliberately, not floating.
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS builder

ENV POETRY_VERSION=2.4.1 \
    POETRY_HOME=/opt/poetry \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1

RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

WORKDIR /build
COPY pyproject.toml poetry.lock README.md ./
COPY src ./src

# Deterministic install from the committed lockfile — no dependency resolution
# happens at build time, so this build is reproducible from poetry.lock alone.
RUN /opt/poetry/bin/poetry install --only main --no-root \
    && /opt/poetry/bin/poetry build --format wheel \
    && /opt/poetry/bin/poetry run pip install --no-deps dist/*.whl

FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS runtime

# Non-root runtime user — never run the service as root.
RUN groupadd --gid 10001 meterly && useradd --uid 10001 --gid meterly --no-create-home meterly

WORKDIR /app
COPY --from=builder /build/.venv /app/.venv
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY src ./src

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER meterly

EXPOSE 8000

# gunicorn manages uvicorn workers; graceful-timeout keeps SIGTERM draining
# within the ECS stopTimeout window (containerization-conventions R2).
CMD ["gunicorn", "src.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "4", \
     "--graceful-timeout", "25", \
     "--timeout", "30"]
