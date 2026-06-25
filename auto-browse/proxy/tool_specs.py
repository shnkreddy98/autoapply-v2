_PRUNED_SUFFIX = (
    " Returns the PAGE MODEL: the page type, every form field (ref, type,"
    " required, current value, options), the action buttons, and any validation"
    " ERRORS. Field types tell you how to fill each one — text→type,"
    " dropdown→select_options, select→select_option, radio/checkbox→click the"
    " option ref, file→open chooser then file_upload. Refs are valid only from"
    " this latest result — discard older refs. Use `expand_region` on a ref to"
    " read a region's full raw content (long option lists, descriptions)."
)

# Selection is now structural (page_model), not goal-based, so tools no longer
# take a goal. Kept as an empty mapping so the `**_NEXT_GOAL_PROP` spreads below
# remain valid no-ops without touching every schema.
_NEXT_GOAL_PROP: dict = {}

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
            "Re-read the current page." + _PRUNED_SUFFIX
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    "click": {
        "upstream": "browser_click",
        "description": (
            "Click an element (button/radio/checkbox/link) by its ref."
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
                    "description": "Exact ref from the most recent page model, e.g. 'e25'.",
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
                    "description": "Exact ref from the most recent page model.",
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
            " those refs directly. Returns the page model plus a summary line of"
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
                    "description": "Exact ref of the combobox/dropdown from the most recent page model.",
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
                    "description": "Exact ref from the most recent page model.",
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
                    "description": "Exact ref from the most recent page model.",
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
            "" + _PRUNED_SUFFIX
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
            " or dead tabs."
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
            " times, prices, status, long option lists) that the page model"
            " summarizes. This is the READ tool: take a field/region ref from the"
            " page model, then call `expand_region` on it to read everything"
            " inside. Expansion is total — no filtering."
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
