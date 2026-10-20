import os
from dotenv import load_dotenv

load_dotenv("dev.env")

# LLM configuration — defaults to OpenRouter, override for local vLLM
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
MODEL = os.getenv("LLM_MODEL", "anthropic/claude-haiku-4-5")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

if "openrouter.ai" in LLM_BASE_URL and not OPENROUTER_API_KEY:
    raise ValueError(
        "OPENROUTER_API_KEY is not set. Get a key at https://openrouter.ai/keys"
    )

# Bullets the tailor rewrites per job, on top of the summary (agents.ResumeTailorAgent).
# Read per run: `autoapply tailor` starts the worker fresh, so
# `TAILOR_BULLETS=3 autoapply tailor` or a line in dev.env both take effect.
TAILOR_BULLETS = int(os.getenv("TAILOR_BULLETS", "5"))
if TAILOR_BULLETS < 1:
    raise ValueError(f"TAILOR_BULLETS must be at least 1, got {TAILOR_BULLETS}")
