# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY . .
# Editable install: the package runs from /app/orbit and finds the datasets in /app/data,
# exactly as in development.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev


FROM python:3.12-slim-bookworm
RUN useradd --system --uid 1000 --create-home orbit \
    && mkdir -p /data/school/files && chown -R orbit:orbit /data
WORKDIR /app
COPY --from=build --chown=orbit:orbit /app/.venv /app/.venv
COPY --chown=orbit:orbit orbit ./orbit
COPY --chown=orbit:orbit data ./data
COPY --chown=orbit:orbit alembic.ini ./
COPY --chown=orbit:orbit migrations ./migrations
COPY --chown=orbit:orbit docker/entrypoint.sh ./docker/entrypoint.sh
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/orbit.db \
    SCHOOL_FILES_DIR=/data/school/files
USER orbit
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]
ENTRYPOINT ["sh", "/app/docker/entrypoint.sh"]
