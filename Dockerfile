# One image, two processes: `uvicorn app.main:app` (api) and `python -m app.worker.main` (worker).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --all-extras

COPY alembic.ini schema.sql ./
COPY migrations migrations
COPY app app

# Optional, for LLM_PROVIDER=claude_code or codex_cli: the vendor's own CLI at the version named
# (docs/operations.md). Left empty, neither is installed. Each is one file in /usr/local/bin.
ARG CLAUDE_CODE_VERSION=""
ARG CODEX_VERSION=""
RUN set -eu; \
    [ -z "$CLAUDE_CODE_VERSION$CODEX_VERSION" ] && exit 0; \
    apt-get update && apt-get install -y --no-install-recommends curl ca-certificates; \
    if [ -n "$CLAUDE_CODE_VERSION" ]; then \
        export HOME=/tmp/claude-install; \
        curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_CODE_VERSION"; \
        install -m 0755 "$(readlink -f "$HOME/.local/bin/claude")" /usr/local/bin/claude; \
    fi; \
    if [ -n "$CODEX_VERSION" ]; then \
        build="codex-$(uname -m)-unknown-linux-musl"; \
        curl -fsSL "https://github.com/openai/codex/releases/download/rust-v$CODEX_VERSION/$build.tar.gz" \
            | tar -xz -C /tmp; \
        install -m 0755 "/tmp/$build" /usr/local/bin/codex; \
    fi; \
    apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* /tmp/* /root/.claude*

USER nobody
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
