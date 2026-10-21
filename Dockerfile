# Multi-stage build:
#   runtime (default) - only what the service needs: app, data, evals, scripts. No tests, no pytest.
#   test              - runtime + test suite + pytest:  docker compose run --rm tests
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first (cached until the lockfile changes). --require-hashes: every package must match
# the sha256 pinned in requirements.lock, so a tampered or re-published package fails the build.
COPY requirements.lock .
RUN pip install --require-hashes -r requirements.lock

COPY app ./app
COPY data ./data
COPY evals ./evals
COPY scripts ./scripts


FROM base AS test
COPY requirements-dev.lock pytest.ini ./
RUN pip install --require-hashes -r requirements-dev.lock
COPY tests ./tests
CMD ["python", "-m", "pytest", "-o", "addopts="]


FROM base AS runtime
# Run as a non-root user; /app/storage holds the index, tickets, conversations (mounted as a volume).
RUN useradd --create-home appuser && mkdir -p /app/storage && chown -R appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
