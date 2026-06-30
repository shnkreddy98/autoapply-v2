# Pruning proxy MCP server.
FROM python:3.11-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
# build context is the repo root. The local `common` pkg must sit beside /app so
# the `../common` path in uv.lock resolves to /common.
COPY common /common
COPY auto-browse/pyproject.toml auto-browse/uv.lock auto-browse/README.md ./
COPY auto-browse/proxy ./proxy
COPY auto-browse/agent ./agent

RUN uv sync --frozen --no-dev

EXPOSE 8932

CMD ["uv", "run", "playwright-proxy"]
