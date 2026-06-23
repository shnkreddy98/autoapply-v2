# Pruning proxy MCP server.
FROM python:3.11-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY proxy ./proxy
COPY agent ./agent

RUN uv sync --frozen --no-dev

EXPOSE 8932

CMD ["uv", "run", "playwright-proxy"]
