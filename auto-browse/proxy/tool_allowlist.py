EXPOSED: frozenset[str] = frozenset({
    "browser_navigate",
    "browser_navigate_back",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_press_key",
    "browser_hover",
    "browser_wait_for",
    "browser_handle_dialog",
    "browser_take_screenshot",
    "browser_file_upload",
    "browser_tabs",
})

HIDDEN: frozenset[str] = frozenset({
    "browser_run_code_unsafe",
    "browser_evaluate",
    "browser_network_requests",
    "browser_network_request",
    "browser_console_messages",
    "browser_drop",
})

# Tools implemented by the proxy itself, not forwarded upstream
PROXY_ONLY: frozenset[str] = frozenset({"expand_region"})

# Params stripped from args before forwarding upstream
STRIP_PARAMS: frozenset[str] = frozenset({"goal", "next_goal"})

# Tools whose results contain an accessibility tree
TREE_RETURNING: frozenset[str] = frozenset({
    "browser_navigate",
    "browser_navigate_back",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_press_key",
    "browser_hover",
    "browser_wait_for",
    "browser_file_upload",
    "browser_tabs",
})
