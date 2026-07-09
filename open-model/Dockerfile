# Ollama + bearer-token auth proxy, for running on a rented GPU (RunPod).
# Ollama itself has NO auth; exposing it raw hands your GPU to anyone who
# finds the hostname. Caddy fronts it: requests must carry
#   Authorization: Bearer $PROXY_TOKEN
# which is exactly what OpenAI-style clients send as the api_key — so
# auto-browse's OPENROUTER_API_KEY doubles as the access token.
#
# Layout: caddy listens on 11434 (the exposed port), ollama binds loopback.

FROM caddy:2 AS caddy

FROM ollama/ollama:latest

COPY --from=caddy /usr/bin/caddy /usr/bin/caddy
COPY Caddyfile /etc/caddy/Caddyfile
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Model blobs live on the mounted network volume so pod recreation never
# re-downloads. Override if your volume mounts elsewhere.
ENV OLLAMA_MODELS=/workspace/ollama
ENV MODEL=qwen3:32b
ENV OLLAMA_CONTEXT_LENGTH=32768
ENV OLLAMA_KEEP_ALIVE=30m

EXPOSE 11434
ENTRYPOINT ["/entrypoint.sh"]
