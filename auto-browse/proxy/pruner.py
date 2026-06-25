"""Read path: verbatim subtree expansion.

The page-model extractor (proxy/page_model.py) is the WRITE path — it returns a
typed view of every form field/action/error. `expand_region` is the complementary
READ path: dump the FULL content of one ref's subtree from the cached tree,
including the text/value leaves (long option lists, descriptions, dates) that the
page model summarizes. No keyword filtering; refs and indentation preserved.
"""

from .tree_parser import collect_all, parse

EXPAND_MAX_NODES = 250


def expand_region(ref: str, goal: str = "", max_nodes: int = EXPAND_MAX_NODES) -> str:
    """Dump the FULL content of a subtree from the cached raw tree, verbatim.

    Capped at ``max_nodes`` to bound context; drill into a deeper ref if
    truncated. ``goal`` is accepted for API compatibility but ignored.
    """
    from . import snapshot_cache

    raw = snapshot_cache.get()
    if not raw:
        return "Error: no snapshot in cache. Call snapshot or navigate first."

    root = parse(raw)
    if root is None:
        return "Error: could not parse cached snapshot."

    target = next((n for n in collect_all(root) if n.ref == ref), None)
    if target is None:
        return f"Error: ref={ref} not found in cached snapshot."

    subtree = collect_all(target)
    shown = subtree[:max_nodes]

    lines = [
        f"[expanded: {len(shown)} of {len(subtree)} nodes under {ref} "
        f"— full content, refs preserved]",
        "",
    ]
    lines.extend(node.raw_line for node in shown)
    if len(subtree) > max_nodes:
        lines.append(
            f"  ... ({len(subtree) - max_nodes} more nodes truncated — "
            f"expand a deeper ref to read them)"
        )
    return "\n".join(lines)
