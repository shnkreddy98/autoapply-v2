#!/bin/bash
# /workspace/start.sh

set -e

echo "==> Setting up workspace environment..."

# ── Docker ──────────────────────────────────────────────────────────────────
if ! command -v docker &> /dev/null; then
    echo "==> Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl start docker
    systemctl enable docker
else
    echo "==> Docker already installed: $(docker --version)"
fi

# ── uv ──────────────────────────────────────────────────────────────────────
if [ ! -f "/workspace/.uv/bin/uv" ]; then
    echo "==> Installing uv to /workspace..."
    curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/workspace/.uv/bin sh
else
    echo "==> uv already installed: $(/workspace/.uv/bin/uv --version)"
fi

# ── Ollama ───────────────────────────────────────────────────────────────────
if [ ! -f "/workspace/.ollama/bin/ollama" ]; then
    echo "==> Installing Ollama to /workspace..."
    mkdir -p /workspace/.ollama/bin
    curl -L https://ollama.com/download/ollama-linux-amd64 -o /workspace/.ollama/bin/ollama
    chmod +x /workspace/.ollama/bin/ollama
else
    echo "==> Ollama already installed: $(/workspace/.ollama/bin/ollama --version)"
fi

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
echo "    uv:      $(uv --version)"
echo "    Ollama:  $(/workspace/.ollama/bin/ollama --version)"
