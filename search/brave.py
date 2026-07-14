"""Thin Brave Search API client with offset pagination.

Every call to `search_page` is one billed request ($5 / 1,000). The caller is
responsible for budget accounting (see `usage.py`) — this module only talks HTTP.
"""

from __future__ import annotations

import os
import time

import requests

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
PAGE_SIZE = 20  # API max for `count`
MAX_OFFSET = 9  # API max for `offset` -> at most 10 pages per query


class BraveAuthError(RuntimeError):
    """401/403 from Brave — bad or missing API key, abort the whole run."""


def build_query(role: str, location: str, site: str) -> str:
    return f'{role} "{location}" site:{site}'


def search_page(
    query: str,
    offset: int = 0,
    freshness: str = "pd",
    count: int = PAGE_SIZE,
    api_key: str | None = None,
    session: requests.Session | None = None,
) -> list[dict]:
    """Fetch one result page. Returns the web results (possibly empty).

    `freshness="pd"` = past day, matching the 24h discovery window.
    Retries once on 429 (rate limit), returns [] on other transient failures.
    """
    key = api_key or os.getenv("BRAVE_API_KEY")
    if not key:
        raise BraveAuthError("BRAVE_API_KEY not set")

    params = {
        "q": query,
        "freshness": freshness,
        "result_filter": "web",
        "count": count,
        "offset": offset,
    }
    headers = {"X-Subscription-Token": key, "Accept": "application/json"}
    http = session or requests

    for attempt in (1, 2):
        res = http.get(BRAVE_URL, params=params, headers=headers, timeout=30)
        if res.status_code in (401, 403):
            raise BraveAuthError(f"Brave rejected the API key ({res.status_code}): {res.text}")
        if res.status_code == 429 and attempt == 1:
            time.sleep(1.5)
            continue
        if not res.ok:
            print(f"brave: {res.status_code} for offset={offset}: {res.text[:200]}")
            return []
        body = res.json()
        web = body.get("web") or {}
        results = web.get("results") or []
        if not results:
            info = body.get("query", {})
            print(f"brave: no web results (offset={offset}, query info: {info})")
        return results
    return []
