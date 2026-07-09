# RunPod setup — step by step

One-time setup to run the authed Ollama endpoint on a rented GPU with
auto-deploy from GitHub. Local-only use needs none of this — just
`docker compose up -d` (stock image, no auth, loopback).

## 0. What you'll end up with

```
git push (open-model/**) ──> GH Actions ──> ghcr.io/<you>/open-model:sha ──> RunPod pod restart
auto-browse ── OPENROUTER_BASE_URL=https://<pod-id>-11434.proxy.runpod.net/v1 ──> caddy (bearer) ──> ollama
```

## 1. Generate the access token

```bash
openssl rand -hex 32   # this is PROXY_TOKEN — save it, you'll paste it twice
```

Anyone with this token can use your GPU. Treat like a password.

## 2. First image push

The workflow (`.github/workflows/open-model.yml`) builds on any push to
`open-model/**` on main. Kick it once:

```bash
git add open-model .github/workflows/open-model.yml
git commit -m "open-model: authed ollama image + deploy workflow"
git push
```

Then make the package pullable by RunPod: GitHub → your profile → Packages →
`open-model` → Package settings → **Change visibility → Public**. (Private
works too, but then RunPod needs a registry credential: RunPod console →
Settings → Container Registry Auth → add `ghcr.io` + a GitHub PAT with
`read:packages`, and select it on the pod.)

## 3. RunPod: network volume

Console → Storage → New Network Volume:

- **Size: 100 GB** (~$7/mo; qwen3:32b ≈ 20 GB, leaves room to experiment)
- Datacenter: pick one that lists your target GPU — the volume locks you to it

## 4. RunPod: the pod

Console → Pods → Deploy:

- **GPU**: RTX 5090 32GB (target: `qwen3:32b`) or RTX 4000 Ada 20GB (`qwen3:14b`)
- Spot is fine — the volume makes interruptions cheap
- **Template → Edit**:
  - Container image: `ghcr.io/<you>/open-model:latest`
  - Container disk: 20 GB
  - Network volume: the one from step 3, mount path **`/workspace`**
  - **Expose HTTP port: 11434**
  - Environment variables:
    | name | value |
    |---|---|
    | `PROXY_TOKEN` | token from step 1 |
    | `MODEL` | `qwen3:32b` |
    | `OLLAMA_CONTEXT_LENGTH` | `32768` |

Deploy. First boot pulls ~20 GB of weights onto the volume (watch pod logs);
every later boot is instant. The entrypoint refuses to start without
`PROXY_TOKEN` — an unauthenticated pod never goes live.

## 5. Verify

Pod page shows the proxy URL: `https://<pod-id>-11434.proxy.runpod.net`.

```bash
TOKEN=<step-1 token>
URL=https://<pod-id>-11434.proxy.runpod.net

curl -s $URL/v1/models                                    # -> 401 unauthorized (good)
curl -s $URL/v1/models -H "Authorization: Bearer $TOKEN"  # -> model list

curl -s $URL/v1/chat/completions -H "Authorization: Bearer $TOKEN" -d '{
  "model": "qwen3:32b",
  "messages": [{"role": "user", "content": "hi"}]}' | jq '.choices[0].message'
```

## 6. Wire auto-browse

`auto-browse/.env`:

```bash
OPENROUTER_BASE_URL=https://<pod-id>-11434.proxy.runpod.net/v1
OPENROUTER_MODEL=qwen3:32b
OPENROUTER_API_KEY=<step-1 token>   # the SDK sends it as the Bearer header caddy checks
```

Revert to OpenRouter = delete/comment `OPENROUTER_BASE_URL` and restore the
old key/model.

## 7. CI/CD: auto-deploy on push

Repo → Settings → Secrets and variables → Actions → New repository secret:

| secret | where to get it |
|---|---|
| `RUNPOD_API_KEY` | RunPod console → Settings → API Keys (needs pod read/write) |
| `RUNPOD_POD_ID` | the pod's id from its page (e.g. `abc123xyz`) |

From then on any push touching `open-model/**` builds the image and restarts
the pod on it (stop → image swap → start; the deploy step no-ops until the
secrets exist). Model weights sit on the volume, so a roll takes ~a minute,
not a re-download.

## 8. Stopping the meter

- Pause: stop the pod (GPU billing stops; volume keeps billing ~$7/mo)
- Done for the day: `runpodctl stop pod <id>` or the console button
- Resume: start pod — same URL, same weights, ~1 min

## Gotchas

- **Never expose ollama without the proxy** — port 11434 raw = free GPU for
  the internet, and RunPod proxy URLs are guessable-ish.
- Volume ↔ GPU datacenter lock: if you switch GPU type later, the volume only
  attaches in its own datacenter.
- glm-5.2 does NOT fit any of this (756B ≈ 400GB+ weights). For that, use it
  per-token: OpenRouter, or `glm-5.2:cloud` + `OLLAMA_API_KEY` in the local
  compose.
- `MAX_COST_USD` in auto-browse never trips on a self-hosted endpoint (cost
  reports $0) — `MAX_ITERATIONS` is the only runaway brake.
