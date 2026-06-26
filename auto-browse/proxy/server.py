"""Transparent proxy MCP server.

Sits in front of Microsoft's Playwright MCP and forwards everything through
unchanged: it exposes the upstream tool list verbatim and relays each call over a
single persistent session (so browser state survives across calls). The ONE thing
it does to the result is inline the accessibility snapshot — Playwright MCP writes
the tree to a `.yml` file and returns a `[Snapshot](path)` link, which an LLM can't
open, so we substitute the link with the full file contents. No pruning, no page
model, no curation — the agent sees the whole snapshot.
"""

import copy
import logging
import os
import re
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import mcp.types as types
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette
from starlette.routing import Mount

logger = logging.getLogger(__name__)

UPSTREAM_URL = os.getenv("PLAYWRIGHT_MCP_URL", "http://localhost:8931/mcp")
HOST = os.getenv("PROXY_HOST", "0.0.0.0")
PORT = int(os.getenv("PROXY_PORT", "8932"))

# Dir that holds the shared `.playwright-mcp/` output dir, so the snapshot files the
# upstream result links to can be read here and inlined. In compose this is /data.
SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", "data"))
_SNAPSHOT_LINK_RE = re.compile(r"(?:- )?\[Snapshot\]\(([^)]+\.ya?ml)\)")

# After any tool call that returns a snapshot, the upstream captures it the instant
# the action resolves — often before a JS/SPA page has finished rendering (Oracle
# Cloud / Workday return an unrendered shell). We wait this long, then re-snapshot,
# and hand the agent the settled tree instead. Set SETTLE_SECONDS=0 to disable.
SETTLE_SECONDS = float(os.getenv("SETTLE_SECONDS", "2"))


class _Upstream:
    """Holds the single persistent client session to the Playwright MCP, shared
    across all forwarded calls so every navigate/click acts on the same tab."""

    session: ClientSession | None = None


upstream = _Upstream()


# --- tool-schema clarification -------------------------------------------------
# The upstream Playwright MCP describes `target` as "Exact target element reference
# from the page snapshot, OR a unique element selector". Models read the "selector"
# half and send e.g. `button[ref="e76"]`, which fails to match because `ref` is a
# snapshot handle, not a DOM attribute. We rewrite each argument of the snapshot-
# returning interaction tools with one unambiguous description + a worked example,
# keyed by argument name (applied at the top level and inside nested field specs).

_ARG_DESCRIPTIONS: dict[str, str] = {
    "target": (
        "Element ref copied VERBATIM from the snapshot — the bare token inside "
        '`[ref=...]`. Example: for `- button "Apply Now" [ref=e76]` pass '
        '`target="e76"`. Pass the ref token ONLY: never wrap it in a CSS or text '
        'selector such as `button[ref=e76]`, `#e76`, or `button:has-text("Apply")`, '
        "and never invent a selector — anything but the literal ref fails to match."
    ),
    "element": (
        "Short human-readable name of the element, used only for logging/permission "
        '(not for matching). Example: `element="Apply Now button"`.'
    ),
    "text": (
        'Text to type into the element. Example: `text="shnkreddy98@gmail.com"`.'
    ),
    "values": (
        "Array of option LABEL(s) to select, exactly as shown in the dropdown. "
        'Example: `values=["United States of America (+1)"]`.'
    ),
    "key": (
        'Key name or single character to press. Example: `key="Enter"` or `key="a"`.'
    ),
    "url": (
        "Absolute URL to navigate to. "
        'Example: `url="https://jobs.lever.co/acme/123/apply"`.'
    ),
    "submit": (
        "Whether to press Enter after typing the text. Example: `submit=true`."
    ),
    "slowly": (
        "Whether to type one character at a time (triggers per-key handlers). "
        "Example: `slowly=false`."
    ),
    "paths": (
        "Array of absolute file path(s) to upload. "
        'Example: `paths=["/resume/shashank_reddy.pdf"]`.'
    ),
    "name": (
        'Human-readable field name. Example: `name="Email Address"`.'
    ),
    "type": (
        "Field type: one of textbox, checkbox, radio, combobox, slider. "
        'Example: `type="textbox"`.'
    ),
    "value": (
        "Value to fill. For a checkbox use `true`/`false`; for a combobox use the "
        'option text. Example: `value="shnkreddy98@gmail.com"`.'
    ),
}

# A worked one-shot appended to each tool's own description.
_TOOL_EXAMPLES: dict[str, str] = {
    "browser_click": (
        'Example call — to click `- button "Apply Now" [ref=e76] [cursor=pointer]`: '
        '{"element": "Apply Now button", "target": "e76"}.'
    ),
    "browser_type": (
        'Example call — to fill `- textbox "Email" [ref=e282]`: '
        '{"element": "Email field", "target": "e282", "text": "shnkreddy98@gmail.com"}.'
    ),
    "browser_fill_form": (
        'Example call — fill two fields: {"fields": ['
        '{"name": "Email", "type": "textbox", "target": "e282", '
        '"value": "shnkreddy98@gmail.com"}, '
        '{"name": "Agree", "type": "checkbox", "target": "e301", "value": "true"}]}.'
    ),
    "browser_select_option": (
        'Example call — pick an option in `- combobox "Country" [ref=e173]`: '
        '{"element": "Country dropdown", "target": "e173", '
        '"values": ["United States"]}.'
    ),
    "browser_hover": (
        'Example call — hover `- link "Menu" [ref=e44]`: '
        '{"element": "Menu link", "target": "e44"}.'
    ),
    "browser_press_key": 'Example call — {"key": "Enter"}.',
    "browser_navigate": (
        'Example call — {"url": "https://jobs.lever.co/acme/123/apply"}.'
    ),
    "browser_file_upload": (
        'Example call — {"paths": ["/resume/shashank_reddy.pdf"]}.'
    ),
}

# Tools whose schemas we rewrite (snapshot-returning interaction tools).
_AUGMENT_TOOLS: frozenset[str] = frozenset(_TOOL_EXAMPLES)


def _patch_descriptions(node: Any) -> None:
    """Recursively rewrite argument descriptions in a JSON-Schema dict in place,
    matching by argument name (covers top-level params and nested `fields` items)."""
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            for arg, spec in props.items():
                if isinstance(spec, dict) and arg in _ARG_DESCRIPTIONS:
                    spec["description"] = _ARG_DESCRIPTIONS[arg]
        for value in node.values():
            _patch_descriptions(value)
    elif isinstance(node, list):
        for value in node:
            _patch_descriptions(value)


def _augment_tool(tool: types.Tool) -> types.Tool:
    """Return a copy of `tool` with clarified arg descriptions + a worked example
    appended, or the tool unchanged if it is not in the augment set."""
    if tool.name not in _AUGMENT_TOOLS:
        return tool
    schema = copy.deepcopy(tool.inputSchema or {"type": "object", "properties": {}})
    _patch_descriptions(schema)
    description = (tool.description or "").rstrip()
    example = _TOOL_EXAMPLES.get(tool.name)
    if example:
        description = f"{description}\n\n{example}" if description else example
    return types.Tool(name=tool.name, description=description, inputSchema=schema)


def _build_server() -> Server:
    server = Server("playwright-transparent-proxy")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        # Expose the upstream Playwright MCP tools, with clarified argument
        # descriptions + examples on the snapshot-returning interaction tools.
        result = await _require_session().list_tools()
        return [_augment_tool(t) for t in result.tools]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        result = await _require_session().call_tool(name, dict(arguments or {}))
        text = _extract_text(result)
        # If this call produced a snapshot, settle the page then re-snapshot and swap
        # in the settled tree (the upstream one may be a pre-render shell). Otherwise
        # just inline whatever snapshot the result already carries (or pass through).
        link = _SNAPSHOT_LINK_RE.search(text)
        if link and SETTLE_SECONDS > 0:
            settled = await _settle_snapshot_tree()
            if settled is not None:
                return [TextContent(type="text", text=text.replace(link.group(0), settled))]
        return [TextContent(type="text", text=_inline_snapshot(text))]

    return server


async def _settle_snapshot_tree() -> str | None:
    """Wait SETTLE_SECONDS for the page to render, take a fresh snapshot, and return
    its full accessibility tree text. Returns None if the upstream calls fail (the
    caller then falls back to the un-settled snapshot). Calls upstream directly, so
    it does not recurse through `call_tool`."""
    session = _require_session()
    try:
        await session.call_tool("browser_wait_for", {"time": SETTLE_SECONDS})
        result = await session.call_tool("browser_snapshot", {})
    except Exception as exc:  # settling is best-effort, never fatal
        logger.warning("settle failed: %s", exc)
        return None
    text = _extract_text(result)
    m = _SNAPSHOT_LINK_RE.search(text)
    if not m:
        return text  # already an inline tree
    path = SNAPSHOT_DIR / m.group(1)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read settled snapshot %s: %s", path, exc)
        return None


def _require_session() -> ClientSession:
    if upstream.session is None:
        raise RuntimeError("Upstream Playwright MCP session is not connected.")
    return upstream.session


def _extract_text(result: CallToolResult) -> str:
    parts = [c.text for c in result.content if isinstance(c, TextContent)]
    return "\n".join(parts) if parts else str(result.content)


def _inline_snapshot(text: str) -> str:
    """Replace a `[Snapshot](file.yml)` link with the file's full contents so the
    agent sees the whole accessibility tree. Pass the text through unchanged if
    there is no link or the file can't be read."""
    m = _SNAPSHOT_LINK_RE.search(text)
    if not m:
        return text
    path = SNAPSHOT_DIR / m.group(1)
    try:
        tree = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read snapshot %s: %s", path, exc)
        return text
    return text.replace(m.group(0), tree)


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    server = _build_server()
    session_manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,
        json_response=False,
        stateless=True,
    )

    @asynccontextmanager
    async def lifespan(_app):
        # Open ONE persistent session to the upstream Playwright MCP and keep it
        # for the whole proxy lifetime so browser state survives across calls.
        async with AsyncExitStack() as stack:
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(UPSTREAM_URL)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            upstream.session = session
            logger.info("Connected to upstream Playwright MCP: %s", UPSTREAM_URL)

            async with session_manager.run():
                logger.info("Transparent proxy listening on %s:%s", HOST, PORT)
                try:
                    yield
                finally:
                    upstream.session = None

    app = Starlette(
        lifespan=lifespan,
        routes=[Mount("/mcp", app=session_manager.handle_request)],
    )

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
