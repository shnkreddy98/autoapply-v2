"""Headless Chromium for extractor tests. Uses a cached Playwright browser when the
installed package's own build is not downloaded (DET_AUTOFILL_CHROMIUM overrides)."""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path

EXTRACT_JS = (Path(__file__).parents[1] / "det_autofill" / "extract.js").read_text()
FORMS = Path(__file__).parent / "fixtures" / "forms"


def chromium_executable() -> str | None:
    if os.environ.get("DET_AUTOFILL_CHROMIUM"):
        return os.environ["DET_AUTOFILL_CHROMIUM"]
    found = sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")))
    return found[-1] if found else None


def extract_file(page, path: Path) -> list[dict]:
    page.goto(path.as_uri())
    page.wait_for_load_state("load")
    return json.loads(page.evaluate(EXTRACT_JS))["fields"]
