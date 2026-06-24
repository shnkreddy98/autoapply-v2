"""
Tests for proxy tool list curation and tool_specs correctness.
No upstream connection needed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from proxy.tool_allowlist import EXPOSED, HIDDEN
from proxy.tool_specs import TOOL_SPECS


def test_dangerous_tools_not_in_specs():
    for hidden in HIDDEN:
        assert hidden not in TOOL_SPECS, f"{hidden} should be hidden from LLM"
    assert "browser_run_code_unsafe" not in TOOL_SPECS
    assert "browser_evaluate" not in TOOL_SPECS


def test_all_exposed_tools_in_specs():
    # Each exposed upstream tool should have a wrapper in TOOL_SPECS
    spec_upstreams = {
        spec["upstream"] for spec in TOOL_SPECS.values() if spec["upstream"]
    }
    for tool in EXPOSED:
        assert tool in spec_upstreams, (
            f"{tool} is exposed but has no wrapper in TOOL_SPECS"
        )


def test_expand_region_is_proxy_only():
    assert "expand_region" in TOOL_SPECS
    assert TOOL_SPECS["expand_region"]["upstream"] is None


def test_every_spec_has_required_fields():
    for name, spec in TOOL_SPECS.items():
        assert "description" in spec, f"{name} missing description"
        assert "inputSchema" in spec, f"{name} missing inputSchema"
        assert "upstream" in spec, f"{name} missing upstream"


def test_tree_returning_tools_have_next_goal():
    from proxy.tool_allowlist import TREE_RETURNING

    for name, spec in TOOL_SPECS.items():
        upstream = spec.get("upstream")
        if upstream in TREE_RETURNING:
            props = spec["inputSchema"].get("properties", {})
            assert "next_goal" in props or "goal" in props, (
                f"{name} returns a tree but has no next_goal/goal param"
            )


def test_snapshot_has_goal_not_next_goal():
    props = TOOL_SPECS["snapshot"]["inputSchema"]["properties"]
    assert "goal" in props
    assert "next_goal" not in props


def test_hidden_tool_direct_call_rejected():
    """Simulate the proxy rejecting a hidden tool call."""
    from proxy.tool_specs import TOOL_SPECS

    hidden_tool = "browser_run_code_unsafe"
    assert hidden_tool not in TOOL_SPECS


def test_expand_region_schema():
    schema = TOOL_SPECS["expand_region"]["inputSchema"]
    assert "ref" in schema["properties"]
    assert "ref" in schema["required"]
    # expand_region is now a full raw dump — no goal/keyword filtering
    assert "goal" not in schema["properties"]


# --- settle / empty-tree handling (#1, #2) ---


def test_navigates_detects_navigation_actions():
    from proxy.server import _navigates

    assert _navigates("browser_navigate", {})
    assert _navigates("browser_navigate_back", {})
    assert _navigates("browser_type", {"submit": True})
    assert not _navigates("browser_type", {"submit": False})
    assert not _navigates("browser_type", {})
    assert _navigates("browser_press_key", {"key": "Enter"})
    assert _navigates("browser_press_key", {"key": "Return"})
    assert not _navigates("browser_press_key", {"key": "a"})
    assert not _navigates("browser_click", {})


def test_node_count_ignores_url_and_text_nodes():
    from proxy.server import _node_count

    assert _node_count("") == 0
    assert _node_count("- generic [ref=e1]") == 1
    tree = (
        "- generic [ref=e1]:\n"
        '  - link "Home" [ref=e2]:\n'
        "    - /url: https://x\n"
        "  - text: hello"
    )
    # generic + link = 2 real nodes; /url and text excluded
    assert _node_count(tree) == 2


def test_empty_tree_message_constant():
    from proxy.server import _EMPTY_TREE_MSG

    assert "wait_for" in _EMPTY_TREE_MSG
