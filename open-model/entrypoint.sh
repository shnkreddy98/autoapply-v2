#!/bin/sh
# Start ollama on loopback, pull $MODEL (cache-hit on the network volume),
# then run caddy in the foreground as the auth gate on :11434.
set -eu

if [ -z "${PROXY_TOKEN:-}" ]; then
    echo "FATAL: PROXY_TOKEN is not set — refusing to expose an unauthenticated ollama" >&2
    exit 1
fi

export OLLAMA_HOST=127.0.0.1:11435
mkdir -p "${OLLAMA_MODELS:-/workspace/ollama}"

ollama serve &

# wait for the server, then pull (no-op when the volume already has the blobs)
i=0
until ollama ls >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -gt 60 ] && echo "FATAL: ollama did not come up" >&2 && exit 1
    sleep 1
done
ollama pull "${MODEL}"
echo "ready: serving ${MODEL} behind bearer auth on :11434"

exec caddy run --config /etc/caddy/Caddyfile
