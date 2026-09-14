"""Thin MCP client for the RAW playwright MCP (:8931 — full ARIA snapshots).

The pruning proxy (:8932) exists to save LLM tokens; the deterministic filler
wants everything, so it talks to the upstream directly.
"""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

DEFAULT_MCP = "http://localhost:8931/mcp"

# Large snapshots are written to a file inside the playwright container
# (~/.playwright-mcp/page-*.yml) and only linked from the tool result. That
# dir is bind-mounted from the auto-browse compose stack; override when the
# layout differs.
SNAPSHOT_DIR = Path(
    os.environ.get(
        "DET_AUTOFILL_SNAPSHOT_DIR",
        Path(__file__).parents[2] / "auto-browse/output/.playwright-mcp",
    )
)

_YAML_FENCE = re.compile(r"```yaml\n(.*?)```", re.S)
_FILE_LINK = re.compile(r"\[Snapshot\]\(([^)]+\.yml)\)")


def snapshot_body(text: str) -> str:
    """Extract the bare ARIA snapshot from a playwright MCP tool result.

    Small snapshots come inline in a ```yaml fence; large ones are saved to
    a .yml file and linked. Fenceless text (already bare) passes through."""
    m = _YAML_FENCE.search(text)
    if m:
        return m.group(1)
    m = _FILE_LINK.search(text)
    if m:
        path = SNAPSHOT_DIR / Path(m.group(1)).name
        if path.exists():
            return path.read_text()
        raise FileNotFoundError(
            f"snapshot file {m.group(1)} not found under {SNAPSHOT_DIR} "
            "(set DET_AUTOFILL_SNAPSHOT_DIR)"
        )
    return text


BEFOREUNLOAD_RE = re.compile(r'\["beforeunload" dialog')


class Browser:
    """await Browser(url).connect(); await b.call('browser_click', target='e7')"""

    def __init__(self, url: str = DEFAULT_MCP):
        self.url = url
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None

    async def connect(self) -> "Browser":
        self._stack = AsyncExitStack()
        read, write, _ = await self._stack.enter_async_context(
            streamable_http_client(self.url)
        )
        self.session = await self._stack.enter_async_context(
            ClientSession(read, write)
        )
        await self.session.initialize()
        return self

    async def close(self) -> None:
        if self._stack:
            await self._stack.aclose()

    async def call(self, name: str, **args) -> str:
        out = await self._call(name, **args)
        if name != "browser_handle_dialog" and BEFOREUNLOAD_RE.search(out):
            # a form page asks "leave site?" when navigated away from (Paylocity). The
            # dialog blocks every later tool call, so one open page wedged the whole
            # session; leaving is what the navigation already meant to do
            await self._call("browser_handle_dialog", accept=True)
            out = await self._call(name, **args)
        return out

    async def _call(self, name: str, **args) -> str:
        result = await self.session.call_tool(name, args)
        parts = [c.text for c in result.content if getattr(c, "text", None)]
        return "\n".join(parts)

    async def _snap_from(self, text: str) -> str:
        """Extract the snapshot from an action result; action results often
        omit it, so fall back to an explicit browser_snapshot call."""
        body = snapshot_body(text)
        if "[ref=" in body:
            return body
        return await self.snapshot()

    # convenience wrappers -------------------------------------------------
    async def navigate(self, url: str) -> str:
        # SPA boards (Workday) keep rendering long after load; poll until the
        # page actually has referenced elements.
        snap = await self._snap_from(await self.call("browser_navigate", url=url))
        for _ in range(10):
            if "[ref=" in snap:
                break
            await asyncio.sleep(1.5)
            snap = await self.snapshot()
        return snap

    async def snapshot(self) -> str:
        return snapshot_body(await self.call("browser_snapshot"))

    async def click(self, ref: str, element: str = "") -> str:
        return await self._snap_from(
            await self.call("browser_click", element=element or ref, target=ref)
        )

    async def type(
        self, ref: str, text: str, element: str = "", slowly: bool = False
    ) -> str:
        # slowly=True types per-keystroke — search boxes only react to key events
        return await self._snap_from(
            await self.call(
                "browser_type",
                element=element or ref,
                target=ref,
                text=text,
                slowly=slowly,
            )
        )

    async def press(self, key: str) -> str:
        return await self._snap_from(await self.call("browser_press_key", key=key))

    async def select_option(self, ref: str, values: list[str], element: str = "") -> str:
        return await self._snap_from(
            await self.call(
                "browser_select_option",
                element=element or ref,
                target=ref,
                values=values,
            )
        )

    async def upload(self, paths: list[str]) -> str:
        return await self._snap_from(await self.call("browser_file_upload", paths=paths))
