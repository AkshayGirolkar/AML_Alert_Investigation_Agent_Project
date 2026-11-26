# One image for the API and the worker; the command decides which one runs.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# Dependencies first so this layer is cached until the lock file changes.
COPY requirements.lock .
# The cache mount keeps downloaded wheels between builds, so a flaky network only
# costs the packages that did not finish.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --retries 10 --timeout 120 -r requirements.lock

# PostgreSQL 17 client (pg_restore) for the one-off seed task (F17). Added before the
# app code so this layer is cached.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo $VERSION_CODENAME)-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17 \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
# Datasets for running the load test from inside the VPC (F17); manifests only, no results.
COPY loadtest/datasets ./loadtest/datasets
COPY eval/datasets ./eval/datasets

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
