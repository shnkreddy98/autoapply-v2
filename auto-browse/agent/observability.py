"""Observability for the agent: application logging, a JSONL conversation trace
for a separate trace app, and append-only per-run usage records.

Three artifacts, all under output/ (gitignored):
  output/logs/app.log          operational log, human-readable, appended
  output/traces/<run_id>.jsonl  one JSON event per line, the LLM-conversation trace
  output/usage.jsonl           one JSON object per run, appended (all runs visible)
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

HERE = Path(__file__).resolve().parent.parent
OUTPUT = HERE / "output"
LOG_DIR = Path(os.getenv("LOG_DIR", OUTPUT / "logs"))
TRACE_DIR = Path(os.getenv("TRACE_DIR", OUTPUT / "traces"))
FORMS_DIR = Path(os.getenv("FORMS_DIR", OUTPUT / "forms"))
USAGE_PATH = Path(os.getenv("USAGE_PATH", OUTPUT / "usage.jsonl"))

LOGGER_NAME = "auto_browse.agent"

# Values that must never reach disk. The portal password sits in the agent's profile
# text and comes back in tool results (the MCP echoes the code that typed it), so on
# 2026-10-05 72 trace / log / form files under output/ held it in plain text.
_SECRETS: set[str] = set()
REDACTED = "[redacted]"


def register_secret(value: str | None) -> None:
    """Scrub `value` from every trace event, log line and record written after this."""
    if value and len(value) >= 4:
        _SECRETS.add(value)


def scrub(text: str) -> str:
    """`text` with registered secrets replaced, as typed and as JSON-escaped."""
    for secret in _SECRETS:
        for form in {secret, json.dumps(secret, ensure_ascii=False)[1:-1]}:
            text = text.replace(form, REDACTED)
    return text


class _ScrubbingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


def new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:6]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


def setup_logging(run_id: str) -> logging.Logger:
    """Configure the agent logger: readable console + appended output/logs/app.log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(LOGGER_NAME)
    log.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    log.handlers.clear()
    log.propagate = False

    fmt = _ScrubbingFormatter(
        "%(asctime)s %(levelname)s [%(run_id)s] %(message)s", datefmt="%H:%M:%S"
    )
    run_filter = _RunIdFilter(run_id)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(run_filter)
    log.addHandler(console)

    file_handler = logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.addFilter(run_filter)
    log.addHandler(file_handler)

    return log


class Trace:
    """Append-only JSONL writer for the LLM-conversation trace of one run.

    Each event is one self-contained JSON line:
    {"run_id", "seq", "ts", "type", ...type-specific fields}.
    """

    def __init__(self, run_id: str, path: Path | None = None) -> None:
        self.run_id = run_id
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        self.path = path or TRACE_DIR / f"{run_id}.jsonl"
        self._seq = 0

    def event(self, type: str, **fields: Any) -> None:
        self._seq += 1
        record = {
            "run_id": self.run_id,
            "seq": self._seq,
            "ts": _utc_now(),
            "type": type,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(scrub(json.dumps(record, ensure_ascii=False, default=str)) + "\n")


def append_usage(record: dict[str, Any]) -> None:
    """Append one run's usage record to output/usage.jsonl (all runs accumulate)."""
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with USAGE_PATH.open("a", encoding="utf-8") as f:
        f.write(scrub(json.dumps(record, ensure_ascii=False, default=str)) + "\n")


def write_form_record(run_id: str, record: dict[str, Any]) -> Path:
    """Write a clean per-run record of every form field the agent filled
    (output/forms/<run_id>.json) so a past application can be reviewed."""
    FORMS_DIR.mkdir(parents=True, exist_ok=True)
    path = FORMS_DIR / f"{run_id}.json"
    path.write_text(
        scrub(json.dumps(record, ensure_ascii=False, indent=2, default=str)), encoding="utf-8"
    )
    return path
