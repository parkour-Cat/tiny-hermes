FROM ghcr.io/astral-sh/uv:0.11.26 AS uv
FROM python:3.12.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

RUN groupadd --system --gid 10001 tiny-hermes \
    && useradd --system --uid 10001 --gid tiny-hermes --create-home tiny-hermes

COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/backend/src ./packages/backend/src
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

USER 10001:10001
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "tiny-hermes-api"]
