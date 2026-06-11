#!/bin/bash
# /workspace/start.sh

set -e

echo "==> Setting up workspace environment..."

# ── Docker ──────────────────────────────────────────────────────────────────
if ! command -v docker &> /dev/null; then
    echo "==> Installing Docker..."
    curl -fsSL https://get.docker.com | sh
fi

if ! pgrep -x "dockerd" > /dev/null; then
    echo "==> Starting Docker daemon..."
    dockerd > /tmp/dockerd.log 2>&1 &
    sleep 3
else
    echo "==> Docker daemon already running"
fi

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

echo ""
echo "==> Done."
echo "    Docker: $(docker --version)"
echo "    uv:     $(uv --version)"
echo "    Ollama: $(ollama --version)"
