"""Pruning proxy MCP server.

Sits in front of Microsoft's Playwright MCP. Exposes a curated, re-described
tool list (see tool_specs.py / tool_allowlist.py), forwards calls to the
upstream Playwright MCP over a single persistent session, and prunes every
returned accessibility tree down to the nodes relevant to the step goal.
"""

import logging
import os
import re
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import mcp.types as types
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette
from starlette.routing import Mount

from . import snapshot_cache
from .pruner import expand_region, prune
from .tool_allowlist import STRIP_PARAMS, TREE_RETURNING
from .tool_specs import TOOL_SPECS
from .tree_parser import collect_all, parse

logger = logging.getLogger(__name__)

UPSTREAM_URL = os.getenv("PLAYWRIGHT_MCP_URL", "http://localhost:8931/mcp")
HOST = os.getenv("PROXY_HOST", "0.0.0.0")
PORT = int(os.getenv("PROXY_PORT", "8932"))

# Dir that contains the shared `.playwright-mcp/` output dir, so snapshot files
# referenced by the upstream result can be read here. In compose this is /data.
SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", "data"))
_SNAPSHOT_LINK_RE = re.compile(r"(?:- )?\[Snapshot\]\(([^)]+\.ya?ml)\)")

# Settle handling: JS/SPA pages often haven't rendered when an action's own
# snapshot is captured, yielding a near-empty tree. After a navigation (or a
# submit that triggers one) we wait, then take a fresh snapshot to prune that.
SETTLE_SECONDS = float(os.getenv("SETTLE_SECONDS", "1.5"))
# A tree with <= this many real (non-url/text) nodes is treated as "not loaded".
MIN_MEANINGFUL_NODES = int(os.getenv("MIN_MEANINGFUL_NODES", "2"))
_EMPTY_TREE_MSG = "[empty: page not loaded — call wait_for, then snapshot again]"


class _Upstream:
    """Holds the single persistent client session to the Playwright MCP.

    One session is shared across all forwarded tool calls so every
    navigate/click acts on the same browser tab.
    """

    session: ClientSession | None = None


upstream = _Upstream()


def _build_server() -> Server:
    server = Server("playwright-pruning-proxy")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=name,
                description=spec["description"],
                inputSchema=spec["inputSchema"],
            )
            for name, spec in TOOL_SPECS.items()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        args = dict(arguments or {})

        # Reject hidden/unknown tools — never forwarded upstream.
        if name not in TOOL_SPECS:
            return [
                TextContent(type="text", text=f"Error: tool '{name}' is not available.")
            ]

        spec = TOOL_SPECS[name]

        # Proxy-only tools (e.g. expand_region) — no upstream call.
        if spec["upstream"] is None:
            return _handle_proxy_tool(name, args)

        # Pull the pruning goal out before forwarding; strip proxy-only params.
        goal = args.pop("next_goal", None) or args.pop("goal", None)
        for param in STRIP_PARAMS:
            args.pop(param, None)

        upstream_name = spec["upstream"]
        # Merge any fixed upstream args the wrapper pins (e.g. action="select"
        # for the tab wrappers) — done after goal/next_goal are stripped.
        args.update(spec.get("inject_args", {}))

        # Decide whether this action likely triggers a navigation/re-render, in
        # which case we should settle before snapshotting (see _prune_snapshot).
        force_settle = _navigates(upstream_name, args)

        raw_result = await _call_upstream(upstream_name, args)
        raw_text = _extract_text(raw_result)

        if upstream_name in TREE_RETURNING:
            raw_text = await _prune_snapshot(raw_text, goal, force_settle)

        return [TextContent(type="text", text=raw_text)]

    return server


def _handle_proxy_tool(name: str, args: dict) -> list[TextContent]:
    if name == "expand_region":
        result = expand_region(args.get("ref", ""), args.get("goal", ""))
        return [TextContent(type="text", text=result)]
    return [TextContent(type="text", text=f"Error: unknown proxy tool '{name}'.")]


async def _call_upstream(tool_name: str, arguments: dict) -> CallToolResult:
    if upstream.session is None:
        raise RuntimeError("Upstream Playwright MCP session is not connected.")
    return await upstream.session.call_tool(tool_name, arguments)


def _extract_text(result: CallToolResult) -> str:
    parts = [c.text for c in result.content if isinstance(c, TextContent)]
    return "\n".join(parts) if parts else str(result.content)


async def _prune_snapshot(raw_text: str, goal: str | None, force_settle: bool) -> str:
    """Find the accessibility tree in an upstream result, settle it if needed,
    cache it, and replace it with the pruned version (or leave full if no goal).

    Playwright MCP returns the snapshot either inline or, more commonly, as a
    `[Snapshot](.playwright-mcp/page-*.yml)` link to a file. The data dir is
    shared into this container (SNAPSHOT_DIR) so the file can be read here.

    Settle (#1/#2): if this action navigated, or the captured tree looks
    unrendered (<= MIN_MEANINGFUL_NODES), wait then re-snapshot and use the
    larger result. If the tree is still empty afterwards, return an explicit
    empty signal so the model knows to wait rather than reuse stale refs.
    """
    link_line, tree_text = _load_tree(raw_text)
    appended = False
    if tree_text is None:
        # Some results carry no tree of their own (e.g. browser_tabs returns just
        # the tab list). When the action changed the active page, fetch a fresh
        # snapshot of the now-current tab and append it; otherwise pass through.
        if not force_settle:
            return raw_text
        tree_text = await _wait_and_snapshot()
        if tree_text is None:
            return raw_text
        appended = True
    elif force_settle or _node_count(tree_text) <= MIN_MEANINGFUL_NODES:
        settled = await _wait_and_snapshot()
        if settled is not None and _node_count(settled) > _node_count(tree_text):
            tree_text = settled

    snapshot_cache.set(tree_text)

    if _node_count(tree_text) <= MIN_MEANINGFUL_NODES:
        body = _EMPTY_TREE_MSG
    elif goal:
        body = prune(tree_text, goal)
    else:
        body = tree_text

    # Splice the body into the original result (replacing the file-link line),
    # append it when the original carried no tree of its own (tab tools), or
    # return it directly when the tree was inline.
    if appended:
        return f"{raw_text}\n\n### Snapshot\n{body}"
    if link_line:
        return raw_text.replace(link_line, body)
    return body


def _navigates(upstream_name: str, args: dict) -> bool:
    """True if the action likely triggers a navigation or full re-render, so the
    resulting page should be settled before snapshotting."""
    if upstream_name in ("browser_navigate", "browser_navigate_back"):
        return True
    if upstream_name == "browser_type" and args.get("submit"):
        return True
    if upstream_name == "browser_press_key":
        return str(args.get("key", "")).lower() in ("enter", "return")
    # Switching/opening/closing a tab changes the active page; settle + snapshot
    # the now-current tab so the agent gets its fresh (pruned) tree.
    if upstream_name == "browser_tabs":
        return args.get("action") in ("select", "new", "close")
    return False


async def _wait_and_snapshot() -> str | None:
    """Wait for the page to settle, then take a fresh snapshot. Returns the tree
    text, or None if the extra upstream calls fail."""
    try:
        await _call_upstream("browser_wait_for", {"time": SETTLE_SECONDS})
        result = await _call_upstream("browser_snapshot", {})
        _, tree_text = _load_tree(_extract_text(result))
        return tree_text
    except Exception as exc:  # settling is best-effort, never fatal
        logger.warning("settle failed: %s", exc)
        return None


def _node_count(tree_text: str) -> int:
    """Number of real (non-url/non-text) nodes in a snapshot tree."""
    root = parse(tree_text)
    if root is None:
        return 0
    return sum(1 for n in collect_all(root) if not n.is_url and not n.is_text)


def _load_tree(text: str) -> tuple[str | None, str | None]:
    """Return (link_line, tree_text). link_line is the matched `[Snapshot](...)`
    line when the tree came from a file (None for an inline tree)."""
    m = _SNAPSHOT_LINK_RE.search(text)
    if m:
        path = SNAPSHOT_DIR / m.group(1)
        if path.is_file():
            return m.group(0), path.read_text(encoding="utf-8")
        return None, None

    # Fallback: inline tree (first `- role ... [ref=...]` line onward).
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("- ") and "[ref=" in stripped:
            return None, "\n".join(lines[i:])
    return None, None


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
                logger.info("Pruning proxy listening on %s:%s", HOST, PORT)
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
