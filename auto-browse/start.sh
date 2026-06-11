#!/bin/bash
# /workspace/start.sh

set -e

echo "==> Setting up workspace environment..."

# ── uv ──────────────────────────────────────────────────────────────────────
if ! command -v uv &> /dev/null; then
    echo "==> Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    source $HOME/.local/bin/env
else
    echo "==> uv already installed"
fi

# ── Ollama ───────────────────────────────────────────────────────────────────
if ! command -v ollama &> /dev/null; then
    echo "==> Installing Ollama..."
    apt-get update && apt-get install -y zstd
    curl -fsSL https://ollama.com/install.sh | sh
else
    echo "==> Ollama already installed"
fi

# ── Start Ollama server ───────────────────────────────────────────────────────
if ! pgrep -x "ollama" > /dev/null; then
    echo "==> Starting Ollama server..."
    nohup ollama serve > /tmp/ollama.log 2>&1 &
    sleep 2
    echo "==> Ollama server started (log: /tmp/ollama.log)"
else
    echo "==> Ollama server already running"
fi

# ── Playwright MCP ───────────────────────────────────────────────────────────
if ! pgrep -f "playwright/mcp" > /dev/null; then
    echo "==> Starting Playwright MCP server..."
    cd /workspace/autoapply-v2/auto-browse
    nohup npx @playwright/mcp --headless --browser chromium --no-sandbox --port 8931 --host 0.0.0.0 > /tmp/playwright-mcp.log 2>&1 &
    sleep 2
    echo "==> Playwright MCP started (log: /tmp/playwright-mcp.log)"
else
    echo "==> Playwright MCP already running"
fi

echo ""
echo "==> Done."
echo "    uv:     $(uv --version)"
echo "    Ollama: $(ollama --version)"
