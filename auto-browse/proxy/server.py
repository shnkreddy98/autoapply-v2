"""Text-extracting proxy MCP server.

Sits in front of Microsoft's Playwright MCP, exposes the upstream tool list
verbatim, and relays each call over a single persistent session (so browser state
survives across calls). For any call that produces an accessibility snapshot,
Playwright MCP writes the ARIA tree to a `.yml` file and returns a
`[Snapshot](path)` link; we read that tree and reply with ONLY its visible text —
accessible names, `text:` nodes, and inline values — dropping roles, refs, URLs,
and structure. Tool results that carry no snapshot pass through unchanged.

`_visible_text` is the single seam for the text<->ARIA mapping; evolve it there.
"""

import asyncio
import copy
import json
import logging
import os
import re
from contextlib import asynccontextmanager
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
SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", "output"))
_SNAPSHOT_LINK_RE = re.compile(r"(?:- )?\[Snapshot\]\(([^)]+\.ya?ml)\)")

# After any tool call that returns a snapshot, the upstream captures it the instant
# the action resolves — often before a JS/SPA page has finished rendering (Oracle
# Cloud / Workday return an unrendered shell). We wait this long, then re-snapshot,
# and hand the agent the settled tree instead. Set SETTLE_SECONDS=0 to disable.
SETTLE_SECONDS = float(os.getenv("SETTLE_SECONDS", "2"))


class _Upstream:
    """Holds the single persistent client session to the Playwright MCP, shared
    across all forwarded calls so every navigate/click acts on the same tab.

    The session can die (upstream restart, idle timeout). All open/close of that
    session happens in ONE long-lived manager task (`_upstream_manager`) — never in
    a request handler — because `streamable_http_client` binds anyio cancel scopes
    to the task that opened them. Request handlers only *signal* a reconnect via
    `reconnect_request` and wait on `ready`."""

    session: ClientSession | None = None
    ready: asyncio.Event = asyncio.Event()  # set while `session` is usable
    reconnect_request: asyncio.Event = asyncio.Event()  # set to ask the manager to cycle
    stop: asyncio.Event = asyncio.Event()  # set on shutdown
    lock: asyncio.Lock = asyncio.Lock()  # serialize reconnect triggers
    # Read-before-write gate: refs the agent has read via fetch_aria since the last
    # page mutation. A ref-using action is only allowed if its target ref is in here.
    # Populated by fetch_aria; cleared on every mutation and on reconnect.
    fresh_refs: set[str] = set()


upstream = _Upstream()

# How long a request waits for a (re)connected session before giving up.
READY_TIMEOUT_SECONDS = float(os.getenv("UPSTREAM_READY_TIMEOUT", "30"))

# Markers in an upstream tool RESULT (no exception raised) that mean the session
# died and must be re-established before the call can succeed.
_DEAD_SESSION_MARKERS = ("session terminated", "session not found", "session closed")


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
    "text": ('Text to type into the element. Example: `text="shnkreddy98@gmail.com"`.'),
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
    "submit": ("Whether to press Enter after typing the text. Example: `submit=true`."),
    "slowly": (
        "Whether to type one character at a time (triggers per-key handlers). "
        "Example: `slowly=false`."
    ),
    "paths": (
        "Array of absolute file path(s) to upload. "
        'Example: `paths=["/resume/shashank_reddy.pdf"]`.'
    ),
    "name": ('Human-readable field name. Example: `name="Email Address"`.'),
    "type": (
        "Field type: one of textbox, checkbox, radio, combobox, slider. "
        'Example: `type="textbox"`.'
    ),
    "value": (
        "Value to fill. For a checkbox use `true`/`false`; for a combobox use the "
        'option text. Example: `value="shnkreddy98@gmail.com"`.'
    ),
    "action": (
        "Tab operation: `list` (show open tabs), `select` (switch to a tab by "
        "index), `close` (close a tab by index, or the current one if omitted), "
        '`new` (open a tab). Example: `action="select"`.'
    ),
    "index": (
        "Zero-based tab index from the '### Open tabs' list in a recent result. "
        "Example: `index=1`."
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
    "browser_tabs": (
        "Use this to manage browser tabs. When a click on 'Apply'/a link opens the "
        "form in a NEW tab, SWITCH to that tab instead of re-clicking the button "
        "(re-clicking just spawns more duplicate tabs). A result lists open tabs "
        "under '### Open tabs'. Example call — switch to the second tab: "
        '{"action": "select", "index": 1}. List tabs: {"action": "list"}. '
        'Close a dead/duplicate tab: {"action": "close", "index": 2}.'
    ),
}

# Full replacements for a tool's top-level description (the upstream text is dropped,
# not appended to). Use when the upstream description no longer matches what the proxy
# actually returns — e.g. browser_snapshot returns visible text here, not raw ARIA.
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "browser_snapshot": (
        "Return the CURRENT page's full visible TEXT — the rendered, human-visible "
        "content of every element on the page, freshly re-read. Use it to re-read the "
        "whole page after an action, or when you need the complete page text again. It "
        "does NOT return element refs or the ARIA tree — use `fetch_aria` to recover an "
        "element's `[ref=...]`. Takes no arguments."
    ),
}

# Tools whose entire arg schema we drop (expose them as no-arg). browser_snapshot
# upstream takes boxes/depth/filename/target, but the proxy always returns the whole
# page's visible text, so those args are dead weight / foot-guns (e.g. `filename`
# would dump raw markdown to a file and bypass the text reduction).
_NO_ARG_TOOLS: frozenset[str] = frozenset({"browser_snapshot"})

# Tools whose schemas we rewrite (snapshot-returning interaction tools) or whose
# top-level description we override.
_AUGMENT_TOOLS: frozenset[str] = (
    frozenset(_TOOL_EXAMPLES) | frozenset(_TOOL_DESCRIPTIONS) | _NO_ARG_TOOLS
)

# Upstream Playwright tools we expose to the agent. Everything else (screenshot,
# resize, network/console-request, drag/drop, close, run_code_unsafe) is hidden —
# browser_evaluate covers arbitrary-JS needs and browser_wait_for returns a fresh
# snapshot, so the hidden tools have backstops. `fetch_aria` (proxy-only) is added
# separately. The proxy still calls hidden tools internally (wait_for for settling +
# fetch_aria) via the upstream session, bypassing this allowlist. `browser_snapshot`
# is exposed too, but like every snapshot-returning call its result is reduced to the
# page's visible TEXT — raw ARIA still comes only from `fetch_aria`.
KEEP_TOOLS: frozenset[str] = frozenset({
    "browser_navigate",
    "browser_navigate_back",
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_press_key",
    "browser_file_upload",
    "browser_handle_dialog",
    "browser_console_messages",
    "browser_evaluate",
    "browser_hover",
    "browser_tabs",
    "browser_wait_for",
    "browser_snapshot",
})

# Interaction tools whose result means the page may have navigated/re-rendered, so
# every ref the model captured from an earlier fetch_aria/snapshot is now stale. We
# prepend a hint telling it to re-fetch before acting (browser_snapshot/fetch_aria
# are excluded — they already hand the model a fresh view to read refs from).
_REF_INVALIDATING_TOOLS: frozenset[str] = frozenset({
    "browser_navigate",
    "browser_navigate_back",
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_press_key",
    "browser_file_upload",
    "browser_handle_dialog",
})

_STALE_REF_HINT = (
    "⚠ The page changed — every ref from an earlier snapshot or fetch_aria is now "
    "STALE. You MUST call fetch_aria for the CURRENT page to get fresh refs before "
    "your next click/type; a click/type on a ref you did not just fetch_aria is "
    "REJECTED and not performed. Do NOT reuse a ref you saw before this action.\n\n"
)

# Ref-taking action tools gated by the read-before-write invariant: each must be
# preceded by a fetch_aria that surfaced the target ref since the last mutation.
_REF_GATED_TOOLS: frozenset[str] = frozenset({
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_press_key",
    "browser_file_upload",
    "browser_hover",
    "browser_drag",
    "browser_drop",
})

_ARIA_REF_RE = re.compile(r"\[ref=(e\d+)\]")       # refs as they appear in ARIA
_TARGET_REF_RE = re.compile(r"(?:ref=|#)(e\d+)")   # refs as the agent passes them


def _refs_in_aria(text: str) -> set[str]:
    """Refs surfaced to the agent in a fetch_aria result (`... [ref=e12] ...`)."""
    return set(_ARIA_REF_RE.findall(text))


def _target_refs(arguments: dict) -> set[str]:
    """Refs the agent is trying to act on, pulled from selector args like
    `button[ref=e76]` or `#e76`. Text/CSS selectors carrying no ref yield nothing."""
    return set(_TARGET_REF_RE.findall(json.dumps(arguments, default=str)))


def _ref_gate(name: str, arguments: dict, fresh: set[str]) -> str | None:
    """Read-before-write check. Returns a corrective message if the call targets a
    ref that was not read via fetch_aria since the last mutation, else None (allow).
    Non-gated tools and ref-less selectors always pass."""
    if name not in _REF_GATED_TOOLS:
        return None
    targets = _target_refs(arguments)
    stale = targets - fresh
    if not stale:
        return None
    return (
        f"⚠ Blocked: ref(s) {', '.join(sorted(stale))} were not read from the current "
        "page. Read-before-write: call fetch_aria for the element(s) you want on the "
        "CURRENT page, then retry with the fresh ref. (The action was NOT performed.)"
    )


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
    if tool.name in _NO_ARG_TOOLS:
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
    else:
        schema = copy.deepcopy(tool.inputSchema or {"type": "object", "properties": {}})
    _patch_descriptions(schema)
    # Override the whole description when set, else keep + append to the upstream one.
    description = (_TOOL_DESCRIPTIONS.get(tool.name) or tool.description or "").rstrip()
    example = _TOOL_EXAMPLES.get(tool.name)
    if example:
        description = f"{description}\n\n{example}" if description else example
    return types.Tool(name=tool.name, description=description, inputSchema=schema)


# --- proxy-only fetch_aria tool ------------------------------------------------
# The agent normally sees only the visible TEXT of the page. `fetch_aria` lets it
# pull the raw ARIA back for a specific key — every line that contains the key,
# with surrounding context — so it can recover an element's `[ref=...]` / structure.
FETCH_ARIA_NAME = "fetch_aria"
FETCH_ARIA_CONTEXT = 3  # default lines of context kept after each match

_FETCH_ARIA_TOOL = types.Tool(
    name=FETCH_ARIA_NAME,
    description=(
        "Search the CURRENT page's full ARIA snapshot for one or more text keys and "
        "return, for each key, every matching line of the raw ARIA YAML with `context` "
        "lines after the match (so you see the element and its `[ref=...]`). Use it to "
        "recover the ref/structure for something you saw in the visible-text output — "
        "e.g. search a button label to get its ref. Pass several keys at once to fetch "
        "multiple fields in a single call. The result is a YAML map: one entry per key "
        "(in the order requested), the value being a literal block of the matched ARIA "
        "lines (or `(no match)`). Use a larger `context` for deeply nested elements "
        "(forms, tables); a smaller one to keep the result tight. Non-contiguous "
        "matches for the same key are separated by a `--` line."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    "Case-insensitive texts to find in the ARIA snapshot. One result "
                    'block is returned per key. Example: keys=["Name", "Country", '
                    '"Apply Now"].'
                ),
            },
            "context": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Lines of context to include after each match (same value applied "
                    f"to every key). Defaults to {FETCH_ARIA_CONTEXT}. Example: "
                    "context=5."
                ),
            },
        },
        "required": ["keys"],
        "additionalProperties": False,
    },
)


async def _fetch_aria(arguments: dict) -> list[types.TextContent]:
    """Handle a `fetch_aria` call: snapshot the current page once, then grep its ARIA
    tree for each requested key, returning a YAML map of key -> matched lines."""
    raw_keys = arguments.get("keys")
    if not isinstance(raw_keys, list):
        return [TextContent(type="text", text="fetch_aria: 'keys' must be an array.")]
    keys = [str(k).strip() for k in raw_keys if str(k).strip()]
    if not keys:
        return [
            TextContent(type="text", text="fetch_aria: 'keys' must be a non-empty array.")
        ]
    try:
        context = int(arguments.get("context", FETCH_ARIA_CONTEXT))
    except (TypeError, ValueError):
        context = FETCH_ARIA_CONTEXT
    context = max(0, context)
    tree = await _snapshot_tree_now()
    if tree is None:
        return [
            TextContent(
                type="text", text="fetch_aria: could not read the current snapshot."
            )
        ]
    aria_map = _build_aria_map(tree, keys, context)
    # Record every ref the agent just read — these become the refs it may act on
    # until the next mutation clears them (read-before-write gate).
    upstream.fresh_refs |= _refs_in_aria(aria_map)
    return [TextContent(type="text", text=aria_map)]


async def _snapshot_tree_now() -> str | None:
    """Take a fresh snapshot of the current page and return its full ARIA tree."""
    try:
        result = await _upstream_call("browser_snapshot", {})
    except Exception as exc:
        logger.warning("fetch_aria snapshot failed: %s", exc)
        return None
    text = _extract_text(result)
    m = _SNAPSHOT_LINK_RE.search(text)
    return _read_tree(m.group(1)) if m else text


def _grep_tree(tree: str, key: str, context: int) -> list[str]:
    """Return the ARIA lines matching `key` (case-insensitive) in `tree`. For each
    occurrence the matched line plus `context` lines after it are kept, with the
    original indentation preserved. Non-contiguous occurrences are separated by a
    `"--"` sentinel line. Returns an empty list when `key` is not found."""
    lines = tree.splitlines()
    needle = key.lower()
    out: list[str] = []
    prev_hi: int | None = None
    for i, line in enumerate(lines):
        if needle not in line.lower():
            continue
        lo = i
        hi = min(len(lines), i + context + 1)
        if prev_hi is not None:
            if lo > prev_hi:
                out.append("--")  # gap between occurrences
            else:
                lo = prev_hi  # contiguous/overlapping -> don't repeat lines
        out.extend(lines[lo:hi])
        prev_hi = hi
    return out


def _build_aria_map(tree: str, keys: list[str], context: int) -> str:
    """Build a YAML map (one entry per key, in request order) from the ARIA `tree`.
    Each value is a literal block scalar of the matched lines (relative indentation
    preserved), or `(no match)` when the key is absent."""
    out: list[str] = []
    for key in keys:
        key_yaml = json.dumps(key)  # safely quote arbitrary keys for YAML
        matched = _grep_tree(tree, key, context)
        if not matched:
            out.append(f"{key_yaml}: (no match)")
            continue
        # Dedent to the shallowest matched line, then indent the whole block by 2 so
        # it is a valid YAML literal block scalar regardless of original nesting.
        indents = [
            len(line) - len(line.lstrip(" "))
            for line in matched
            if line != "--" and line.strip()
        ]
        base = min(indents) if indents else 0
        out.append(f"{key_yaml}: |-")
        for line in matched:
            if line == "--":
                out.append("  --")
            elif not line.strip():
                out.append("")
            else:
                out.append("  " + line[base:])
    return "\n".join(out) + "\n"


def _build_server() -> Server:
    server = Server("playwright-transparent-proxy")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        # Expose the upstream Playwright MCP tools, with clarified argument
        # descriptions + examples on the snapshot-returning interaction tools, plus
        # the proxy-only `fetch_aria` search tool.
        await _ensure_session()
        result = await _require_session().list_tools()
        kept = [_augment_tool(t) for t in result.tools if t.name in KEEP_TOOLS]
        return kept + [_FETCH_ARIA_TOOL]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        if name == FETCH_ARIA_NAME:
            return await _fetch_aria(dict(arguments or {}))
        if name not in KEEP_TOOLS:
            return [TextContent(type="text", text=f"Tool {name!r} is not available.")]
        args = dict(arguments or {})
        # Read-before-write: reject a ref-using action whose target was not read via
        # fetch_aria since the last mutation — do NOT forward it to the browser.
        gate = _ref_gate(name, args, upstream.fresh_refs)
        if gate is not None:
            logger.info("ref gate blocked %s: %s", name, sorted(_target_refs(args) - upstream.fresh_refs))
            return [TextContent(type="text", text=gate)]
        result = await _upstream_call(name, args)
        text = _extract_text(result)
        if name in _REF_INVALIDATING_TOOLS:
            upstream.fresh_refs.clear()  # page mutated -> every read ref is now stale
        # browser_snapshot returns the ARIA tree INLINE (no snapshot link), so reduce
        # that text to visible text directly — otherwise the raw ARIA leaks through.
        if name == "browser_snapshot":
            return [TextContent(type="text", text=_visible_text(text))]
        # Other interaction tools attach a snapshot link; if present, reply with just
        # its visible text. No snapshot in the result -> pass the output through.
        tree = await _snapshot_tree(text)
        if tree is None:
            return [TextContent(type="text", text=text)]
        body = _visible_text(tree)
        if name in _REF_INVALIDATING_TOOLS:
            body = _STALE_REF_HINT + body
        return [TextContent(type="text", text=body)]

    return server


async def _snapshot_tree(text: str) -> str | None:
    """Return the full ARIA tree for a result that links to a snapshot, or None if
    the result has no snapshot. When SETTLE_SECONDS > 0 we wait for the page to
    finish rendering and re-snapshot (the upstream tree can be a pre-render shell);
    otherwise we read the tree the call already produced."""
    link = _SNAPSHOT_LINK_RE.search(text)
    if not link:
        return None
    if SETTLE_SECONDS > 0:
        settled = await _settle_snapshot_tree()
        if settled is not None:
            return settled
    return _read_tree(link.group(1))


async def _settle_snapshot_tree() -> str | None:
    """Wait SETTLE_SECONDS for the page to render, take a fresh snapshot, and return
    its full accessibility tree text. Returns None if the upstream calls fail (the
    caller then falls back to the un-settled snapshot). Calls upstream directly, so
    it does not recurse through `call_tool`."""
    try:
        await _upstream_call("browser_wait_for", {"time": SETTLE_SECONDS})
        result = await _upstream_call("browser_snapshot", {})
    except Exception as exc:  # settling is best-effort, never fatal
        logger.warning("settle failed: %s", exc)
        return None
    text = _extract_text(result)
    m = _SNAPSHOT_LINK_RE.search(text)
    if not m:
        return text  # already an inline tree
    return _read_tree(m.group(1))


async def _upstream_manager() -> None:
    """Own the upstream session for the proxy's lifetime. Opens it, holds it until a
    reconnect is requested (or shutdown), then cycles. ALL session open/close lives
    here so anyio cancel scopes are entered and exited in the same task."""
    while not upstream.stop.is_set():
        try:
            async with streamable_http_client(UPSTREAM_URL) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    upstream.session = session
                    upstream.fresh_refs.clear()  # new session/page -> no read refs
                    upstream.reconnect_request.clear()
                    upstream.ready.set()
                    logger.info("Connected to upstream Playwright MCP: %s", UPSTREAM_URL)
                    await _wait_first(upstream.reconnect_request, upstream.stop)
        except Exception as exc:  # connect/init failed or session blew up
            logger.warning("upstream session error: %s", exc)
        finally:
            upstream.ready.clear()
            upstream.session = None
        if not upstream.stop.is_set():
            await asyncio.sleep(0.5)  # brief backoff before re-dialing
    logger.info("upstream manager stopped")


async def _wait_first(*events: asyncio.Event) -> None:
    """Return as soon as any of the given events is set."""
    waiters = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()


async def _ensure_session() -> ClientSession:
    """Wait for a live upstream session (the manager establishes it)."""
    await asyncio.wait_for(upstream.ready.wait(), timeout=READY_TIMEOUT_SECONDS)
    return upstream.session


async def _request_reconnect() -> ClientSession:
    """Signal the manager to cycle the session, then wait for the fresh one."""
    async with upstream.lock:
        if upstream.ready.is_set():  # first caller to notice the death triggers it
            upstream.ready.clear()
            upstream.reconnect_request.set()
    await asyncio.wait_for(upstream.ready.wait(), timeout=READY_TIMEOUT_SECONDS)
    return upstream.session


def _is_dead_session(result: CallToolResult) -> bool:
    text = _extract_text(result).strip().lower()
    return any(m in text for m in _DEAD_SESSION_MARKERS)


async def _upstream_call(name: str, arguments: dict) -> CallToolResult:
    """Call an upstream tool, reconnecting once if the session is missing or has
    died (either a raised transport error or a dead-session result body)."""
    session = await _ensure_session()
    try:
        result = await session.call_tool(name, arguments)
    except Exception as exc:
        logger.warning("upstream %s raised (%s); reconnecting", name, exc)
        return await (await _request_reconnect()).call_tool(name, arguments)
    if _is_dead_session(result):
        logger.warning("upstream %s returned dead session; reconnecting", name)
        return await (await _request_reconnect()).call_tool(name, arguments)
    return result


def _require_session() -> ClientSession:
    if upstream.session is None:
        raise RuntimeError("Upstream Playwright MCP session is not connected.")
    return upstream.session


def _extract_text(result: CallToolResult) -> str:
    parts = [c.text for c in result.content if isinstance(c, TextContent)]
    return "\n".join(parts) if parts else str(result.content)


def _read_tree(rel_path: str) -> str | None:
    """Read a Playwright snapshot `.yml` file (path relative to SNAPSHOT_DIR) and
    return its full ARIA tree text, or None if it can't be read."""
    path = SNAPSHOT_DIR / rel_path
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read snapshot %s: %s", path, exc)
        return None


# --- ARIA tree -> visible text -------------------------------------------------
# THE seam for the text<->ARIA mapping. A Playwright ARIA snapshot is a YAML tree
# of `- <role> "<name>" [ref=..]:` nodes plus `- text: ...` / `- <role> [..]: value`
# leaves and `- /url: ...` property lines. We keep the human-visible strings (the
# quoted accessible names and inline values) and drop roles, refs, urls, brackets,
# and pure structure. Evolve the rules here as the inspector reveals new shapes.

# Quoted accessible name: `role [..] "Name" [..]:` -> Name
_NAME_RE = re.compile(r'^[\w-]+(?:\s*\[[^\]]*\])*\s*"((?:[^"\\]|\\.)*)"')
# Inline value: `role [..]: value` / `text: value` / `paragraph: value` -> value
_VALUE_RE = re.compile(r"^[\w-]+(?:\s*\[[^\]]*\])*\s*:\s*(.+)$")
# Playwright page-state console summary, e.g. "3 errors, 1 warnings" — pure noise the
# model misreads as a tool failure. Drop it (it rides in via the `Console:` line).
_CONSOLE_NOISE_RE = re.compile(r"^\d+ errors?, \d+ warnings?$")


def _visible_text(snapshot: str) -> str:
    """Extract human-visible text from a Playwright ARIA snapshot, one item per
    line in document order, dropping consecutive duplicates."""
    out: list[str] = []
    for raw in snapshot.splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue
        line = line[2:].strip()
        if line.startswith("/"):  # /url: and other property nodes — not visible
            continue
        if m := _NAME_RE.match(line):
            value = m.group(1).strip()
        elif m := _VALUE_RE.match(line):
            value = m.group(1).strip().strip('"')
            if value.startswith("/"):
                continue
        else:
            continue
        if value and not _CONSOLE_NOISE_RE.match(value) and (not out or out[-1] != value):
            out.append(value)
    return "\n".join(out)


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
        # A single manager task owns the upstream Playwright MCP session for the whole
        # proxy lifetime so browser state survives across calls; if it dies mid-run,
        # `_upstream_call` signals the manager to re-establish it.
        upstream.stop.clear()
        manager = asyncio.create_task(_upstream_manager())
        try:
            await asyncio.wait_for(upstream.ready.wait(), timeout=READY_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.error("upstream did not connect within %ss", READY_TIMEOUT_SECONDS)
        try:
            async with session_manager.run():
                logger.info("Transparent proxy listening on %s:%s", HOST, PORT)
                yield
        finally:
            upstream.stop.set()
            await manager

    app = Starlette(
        lifespan=lifespan,
        routes=[Mount("/mcp", app=session_manager.handle_request)],
    )

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
