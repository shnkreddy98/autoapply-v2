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

echo "==> Docker: $(docker --version)"

# ── uv ──────────────────────────────────────────────────────────────────────
curl -LsSf https://astral.sh/uv/install.sh | sh

# ── Ollama ───────────────────────────────────────────────────────────────────
apt-get update && apt-get upgrade -y
apt-get install zstd -y
curl -fsSL https://ollama.com/install.sh | sh


# ── Start Ollama server ───────────────────────────────────────────────────────
if ! pgrep -x "ollama" > /dev/null; then
    echo "==> Starting Ollama server..."
    OLLAMA_HOME=/workspace/.ollama \
    OLLAMA_MODELS=/workspace/.ollama/models \
    nohup /workspace/.ollama/bin/ollama serve > /workspace/.ollama/ollama.log 2>&1 &
    sleep 2
    echo "==> Ollama server started (log: /workspace/.ollama/ollama.log)"
else
    echo "==> Ollama server already running"
fi

echo ""
echo "==> Done."
echo "    Docker:  $(docker --version)"
echo "    uv:      $(/workspace/.uv/bin/uv --version)"
echo "    Ollama:  $(/workspace/.ollama/bin/ollama --version)"
