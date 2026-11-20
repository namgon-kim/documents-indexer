FROM node:22-bookworm-slim AS web-build
WORKDIR /build/web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS pipeline-build
COPY --from=ghcr.io/astral-sh/uv:0.12.8 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/indexer/ packages/indexer/
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm AS pipeline
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-calc poppler-utils fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY --from=pipeline-build /app/.venv /app/.venv
COPY apps/api/ apps/api/
COPY --from=web-build /build/web/dist/ apps/web/dist/
CMD ["/app/.venv/bin/uvicorn", "apps.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
