"""TypeSafe System One over HTTP: the `system_one(state, questions)` that
det_autofill.jev.JevResolver is built with.

Jev answers typed questions (choice / score / noul) about a state; it does not write
text or call tools. It can stand in for the det engine's resolver model, never for the
agent engine's (agent.loop needs a chat model that emits tool calls).

API: POST {TYPESAFE_BASE_URL}/v1/systemone with {state, model, questions}, bearer
TYPESAFE_API_KEY (https://docs.typesafe.ai/api). Billed per input token only.
"""

from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable

import httpx

BASE_URL = os.getenv("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
DEFAULT_JEV = "jev-latest"
RETRY_STATUS = {429, 500, 502, 503, 504}


def is_jev(model: str | None) -> bool:
    return bool(model) and model.lower().startswith("jev")


def jev_model(model: str) -> str:
    """`jev` alone means the current release alias."""
    return DEFAULT_JEV if model.lower() == "jev" else model


def make_system_one(model: str = DEFAULT_JEV, *, api_key: str | None = None, timeout: float = 60,
                    usage: dict | None = None) -> Callable[[object, dict], Awaitable[dict]]:
    """async (state, questions) -> answers. `usage`, when given, accumulates requests
    and input tokens (the only billed part)."""
    key = (api_key or os.getenv("TYPESAFE_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("TYPESAFE_API_KEY is not set: create a key at https://console.typesafe.ai and put it in auto-browse/.env")
    model = jev_model(model)

    async def system_one(state, questions: dict) -> dict:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(4):
                resp = await client.post(f"{BASE_URL}/v1/systemone",
                                         json={"state": state, "model": model, "questions": questions},
                                         headers={"Authorization": f"Bearer {key}"})
                if resp.status_code in RETRY_STATUS and attempt < 3:
                    # rate limits move with demand (docs: Models); honour retry-after
                    await asyncio.sleep(float(resp.headers.get("retry-after") or 2 ** attempt))
                    continue
                resp.raise_for_status()
                data = resp.json()
                if usage is not None:
                    usage["requests"] = usage.get("requests", 0) + 1
                    usage["input_tokens"] = usage.get("input_tokens", 0) + (data.get("usage") or {}).get("input_tokens", 0)
                return data.get("answers") or {}

    return system_one
