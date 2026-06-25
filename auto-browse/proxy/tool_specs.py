_PRUNED_SUFFIX = (
    " Returns a PRUNED accessibility snapshot filtered to `next_goal`."
    " Refs are preserved exactly and are only valid from this latest returned tree"
    " — discard refs from older snapshots."
    " If the element you need is missing, call `expand_region` on the nearest landmark"
    " or call `snapshot` again with a more specific `goal`."
)

_NEXT_GOAL_PROP = {
    "next_goal": {
        "type": "string",
        "description": (
            "What you intend to find or do next. Used to prune the returned"
            " page snapshot to relevant nodes. If omitted, the full unpruned tree is returned."
        ),
    }
}

TOOL_SPECS: dict[str, dict] = {
    "navigate": {
        "upstream": "browser_navigate",
        "description": ("Navigate to a URL." + _PRUNED_SUFFIX),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to navigate to"},
                **_NEXT_GOAL_PROP,
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    "navigate_back": {
        "upstream": "browser_navigate_back",
        "description": (
            "Go back to the previous page in the history." + _PRUNED_SUFFIX
        ),
        "inputSchema": {
            "type": "object",
            "properties": {**_NEXT_GOAL_PROP},
            "additionalProperties": False,
        },
    },
    "snapshot": {
        "upstream": "browser_snapshot",
        "description": (
            "Capture accessibility snapshot of the current page."
            " Returns a PRUNED accessibility snapshot filtered to `goal`."
            " Refs are preserved exactly and are only valid from this latest returned tree"
            " — discard refs from older snapshots."
            " If the element you need is missing, call `expand_region` on the nearest landmark."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": (
                        "What element or action you are looking for. Used to prune"
                        " the returned tree to relevant nodes."
                        " If omitted, the full unpruned tree is returned."
                    ),
                },
                "target": {
                    "type": "string",
                    "description": "Exact target element reference or selector to scope the snapshot.",
                },
                "depth": {
                    "type": "number",
                    "description": "Limit the depth of the snapshot tree.",
                },
            },
            "additionalProperties": False,
        },
    },
    "click": {
        "upstream": "browser_click",
        "description": (
            "Click an element by its ref from the most recent pruned snapshot."
            + _PRUNED_SUFFIX
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "element": {
                    "type": "string",
                    "description": "Human-readable description of the element being clicked (for logging).",
                },
                "target": {
                    "type": "string",
                    "description": "Exact ref from the most recent pruned snapshot, e.g. 'e25'.",
                },
                "doubleClick": {
                    "type": "boolean",
                    "description": "Whether to perform a double click instead of a single click.",
                },
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "description": "Button to click, defaults to left.",
                },
                **_NEXT_GOAL_PROP,
            },
            "required": ["target"],
            "additionalProperties": False,
        },
    },
    "type": {
        "upstream": "browser_type",
        "description": (
            "Type text into an editable element by its ref." + _PRUNED_SUFFIX
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "element": {
                    "type": "string",
                    "description": "Human-readable description of the element.",
                },
                "target": {
                    "type": "string",
                    "description": "Exact ref from the most recent pruned snapshot.",
                },
                "text": {
                    "type": "string",
                    "description": "Text to type into the element.",
                },
                "submit": {
                    "type": "boolean",
                    "description": "Whether to submit entered text (press Enter after).",
                },
                "slowly": {
                    "type": "boolean",
                    "description": "Whether to type one character at a time.",
                },
                **_NEXT_GOAL_PROP,
            },
            "required": ["target", "text"],
            "additionalProperties": False,
        },
    },
    "fill_form": {
        "upstream": "browser_fill_form",
        "description": ("Fill multiple form fields at once." + _PRUNED_SUFFIX),
        "inputSchema": {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "element": {"type": "string"},
                            "target": {"type": "string"},
                            "name": {"type": "string"},
                            "type": {
                                "type": "string",
                                "enum": [
                                    "textbox",
                                    "checkbox",
                                    "radio",
                                    "combobox",
                                    "slider",
                                ],
                            },
                            "value": {"type": "string"},
                        },
                        "required": ["target", "name", "type", "value"],
                        "additionalProperties": False,
                    },
                    "description": "Fields to fill in.",
                },
                **_NEXT_GOAL_PROP,
            },
            "required": ["fields"],
            "additionalProperties": False,
        },
    },
    "select_options": {
        "upstream": None,  # proxy-orchestrated: runs several upstream calls itself
        "orchestrated": True,
        "description": (
            "Pick one or more options from a dropdown / combobox / listbox /"
            " multiselect in a SINGLE call. Pass the combobox's ref as `target` and"
            " the human-readable option label(s) you want (from the profile) as"
            " `values`. The proxy opens the dropdown, matches each value to the"
            " closest visible option, and clicks it — for a multiselect it repeats"
            " for EVERY value, so pass them all at once (e.g. values=['Python',"
            " 'SQL', 'Spark']). Use this for ALL ARIA dropdowns/listboxes/"
            " multiselects instead of clicking the control then clicking an option"
            " separately. It does NOT work for radio/checkbox questions — click"
            " those refs directly. Returns a pruned snapshot plus a summary line of"
            " which values matched and which did not (with the options that were"
            " available, so you can retry an unmatched one with an exact label)."
            + _PRUNED_SUFFIX
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "element": {
                    "type": "string",
                    "description": "Human-readable description of the dropdown (for logging).",
                },
                "target": {
                    "type": "string",
                    "description": "Exact ref of the combobox/dropdown from the most recent pruned snapshot.",
                },
                "values": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Human-readable option label(s) to select, taken from the"
                        " applicant profile. Pass every value for a multiselect."
                    ),
                },
                **_NEXT_GOAL_PROP,
            },
            "required": ["target", "values"],
            "additionalProperties": False,
        },
    },
    "select_option": {
        "upstream": "browser_select_option",
        "description": (
            "Select an option in a NATIVE <select> dropdown by its ref. Most ATS"
            " dropdowns are NOT native <select> — use `select_options` for those."
            + _PRUNED_SUFFIX
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "element": {"type": "string"},
                "target": {
                    "type": "string",
                    "description": "Exact ref from the most recent pruned snapshot.",
                },
                "values": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Array of option values to select.",
                },
                **_NEXT_GOAL_PROP,
            },
            "required": ["target", "values"],
            "additionalProperties": False,
        },
    },
    "press_key": {
        "upstream": "browser_press_key",
        "description": ("Press a key on the keyboard." + _PRUNED_SUFFIX),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Name of the key to press, e.g. 'Enter', 'ArrowLeft', 'a'.",
                },
                **_NEXT_GOAL_PROP,
            },
            "required": ["key"],
            "additionalProperties": False,
        },
    },
    "hover": {
        "upstream": "browser_hover",
        "description": ("Hover over an element by its ref." + _PRUNED_SUFFIX),
        "inputSchema": {
            "type": "object",
            "properties": {
                "element": {"type": "string"},
                "target": {
                    "type": "string",
                    "description": "Exact ref from the most recent pruned snapshot.",
                },
                **_NEXT_GOAL_PROP,
            },
            "required": ["target"],
            "additionalProperties": False,
        },
    },
    "wait_for": {
        "upstream": "browser_wait_for",
        "description": (
            "Wait for text to appear or disappear, or for a specified time."
            + _PRUNED_SUFFIX
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "time": {"type": "number", "description": "Seconds to wait."},
                "text": {"type": "string", "description": "Text to wait for."},
                "textGone": {
                    "type": "string",
                    "description": "Text to wait for to disappear.",
                },
                **_NEXT_GOAL_PROP,
            },
            "additionalProperties": False,
        },
    },
    "handle_dialog": {
        "upstream": "browser_handle_dialog",
        "description": "Handle a browser dialog (alert, confirm, prompt).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "accept": {
                    "type": "boolean",
                    "description": "Whether to accept the dialog.",
                },
                "promptText": {
                    "type": "string",
                    "description": "Text for prompt dialogs.",
                },
            },
            "required": ["accept"],
            "additionalProperties": False,
        },
    },
    "screenshot": {
        "upstream": "browser_take_screenshot",
        "description": (
            "Take a screenshot of the current page or a specific element."
            " Returns the filename where the screenshot was saved."
            " Use this to capture visual state at key points in the task."
            " Note: you cannot interact with elements based on a screenshot — use `snapshot` for that."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "element": {
                    "type": "string",
                    "description": "Human-readable description of the element to screenshot (optional).",
                },
                "target": {
                    "type": "string",
                    "description": "Ref of a specific element to screenshot (optional, screenshots full page if omitted).",
                },
                "type": {
                    "type": "string",
                    "enum": ["png", "jpeg"],
                    "description": "Image format. Defaults to png.",
                    "default": "png",
                },
                "filename": {
                    "type": "string",
                    "description": "Filename to save the screenshot to. Auto-generated if not provided.",
                },
                "fullPage": {
                    "type": "boolean",
                    "description": "When true, captures the full scrollable page instead of just the visible viewport.",
                },
            },
            "required": ["type"],
            "additionalProperties": False,
        },
    },
    "file_upload": {
        "upstream": "browser_file_upload",
        "description": (
            "Upload one or more local files to a file input on the page (e.g. a"
            " resume/CV upload). First click the upload control or file input so"
            " the chooser is active, then call this with the absolute file path(s)."
            " The resume path is given in the applicant profile." + _PRUNED_SUFFIX
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Absolute path(s) of the file(s) to upload, e.g."
                        " ['/resume/jane_doe.pdf']."
                    ),
                },
                **_NEXT_GOAL_PROP,
            },
            "required": ["paths"],
            "additionalProperties": False,
        },
    },
    "select_tab": {
        "upstream": "browser_tabs",
        "inject_args": {"action": "select"},
        "description": (
            "Switch to a different browser tab by its index, taken from the"
            " '### Open tabs' list in recent tool results (e.g. tab 1)."
            " Use this when an 'Apply'/action opened the application form in a NEW"
            " tab — switch to that tab instead of re-clicking the same button"
            " (re-clicking just opens more duplicate tabs)."
            " Returns a PRUNED snapshot of the tab you switched to." + _PRUNED_SUFFIX
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "number",
                    "description": "Index of the tab to switch to, from the 'Open tabs' list.",
                },
                **_NEXT_GOAL_PROP,
            },
            "required": ["index"],
            "additionalProperties": False,
        },
    },
    "close_tab": {
        "upstream": "browser_tabs",
        "inject_args": {"action": "close"},
        "description": (
            "Close a browser tab by its index (from the '### Open tabs' list);"
            " omit the index to close the current tab. Use it to clean up duplicate"
            " or dead tabs. Returns a PRUNED snapshot of the now-current tab."
            + _PRUNED_SUFFIX
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "number",
                    "description": "Index of the tab to close. If omitted, the current tab is closed.",
                },
                **_NEXT_GOAL_PROP,
            },
            "additionalProperties": False,
        },
    },
    "expand_region": {
        "upstream": None,  # proxy-only, not forwarded
        "description": (
            "Read the FULL content of a region by its ref. Returns every node"
            " inside that ref verbatim — including the text/value leaves (dates,"
            " times, prices, status, labels) that a pruned snapshot omits."
            " This is the READ tool: use a pruned `snapshot` to locate the region"
            " you want, then call `expand_region` on its ref to read everything"
            " inside it. Also use it when `snapshot` returned landmarks_only."
            " No keyword filtering is applied — expansion is total."
            " Makes zero upstream browser calls (operates on the cached tree)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The ref of the region/container to read, e.g. 'e834'.",
                },
            },
            "required": ["ref"],
            "additionalProperties": False,
        },
    },
}
