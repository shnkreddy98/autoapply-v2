# open-model

Self-hosted OpenAI-compatible LLM endpoint for autoapply, via Ollama. Ollama
natively serves the OpenAI chat-completions API (tools/function-calling
included) at `/v1` — no translation layer needed. OpenRouter speaks the same
schema, so auto-browse switches with two env vars.

## Run

```bash
cp .env.example .env         # pick MODEL + CONTEXT_LENGTH
docker compose up -d         # pulls the model on first start (watch: docker compose logs -f)
curl -s localhost:11434/v1/models | jq
```

Wire auto-browse at it (`auto-browse/.env`):

```bash
OPENROUTER_BASE_URL=http://localhost:11434/v1
OPENROUTER_MODEL=qwen3:4b
OPENROUTER_API_KEY=ollama        # any non-empty value
```

Smoke test with tool calls:

```bash
curl -s localhost:11434/v1/chat/completions -d '{
  "model": "qwen3:4b",
  "messages": [{"role": "user", "content": "What is the weather in Austin?"}],
  "tools": [{"type": "function", "function": {"name": "get_weather",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
}' | jq '.choices[0].message'
```

## GPU sizing (what actually fits, q4 quant)

| Hardware | VRAM | Model ceiling | Notes |
|---|---|---|---|
| RTX 3050 laptop (this box) | 4GB | `qwen3:4b` | 7-8B splits to CPU, crawls |
| RTX 4000 Ada (RunPod low tier) | 20GB | `qwen3:14b` | ~$0.30/hr |
| RTX 5090 (RunPod) | 32GB | `qwen3:32b` | ~$0.80/hr; real agentic competence |
| glm-5.2 (756B) | ~400GB+ | 8×H200 node | not a rental-tier model — use `glm-5.2:cloud` or OpenRouter per-token |

Remote box: run this same compose there, then point
`OPENROUTER_BASE_URL=http://<box-ip>:11434/v1` (tunnel/VPN it — Ollama has no
auth of its own; never expose the port raw).

Cloud-only tags (`glm-5.2:cloud`): set `OLLAMA_API_KEY` in `.env` and
uncomment it in the compose. Inference happens on ollama.com; the local
container is only a proxy — same API, zero VRAM used.

## Constraints that bite

- **Context length**: Ollama defaults to 4096 and silently truncates from the
  top — the agent's system prompt dies first. `OLLAMA_CONTEXT_LENGTH` is set
  from `.env`; keep ≥16k for agent runs, more if VRAM allows.
- **Tool-calling models only**: qwen3\*, llama3.1+, mistral-nemo, devstral.
  Others accept the request and never emit `tool_calls`.
- **Small-model reality**: 4B is fine for one-shot structured extraction
  (det-autofill P5 fallback, resume parse experiments) but markedly worse than
  gemini-flash at long apply-agent loops. Expect more handoffs and retries.
- **Cost guard**: local runs report `cost=0`, so auto-browse's MAX_COST_USD
  ceiling never trips — MAX_ITERATIONS is the only brake on a wedged run.
