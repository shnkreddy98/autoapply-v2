"""Observability for the agent: application logging, a JSONL conversation trace
for a separate trace app, and append-only per-run usage records.

Three artifacts, all under data/ (gitignored):
  data/logs/app.log          operational log, human-readable, appended
  data/traces/<run_id>.jsonl  one JSON event per line, the LLM-conversation trace
  data/usage.jsonl           one JSON object per run, appended (all runs visible)
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

HERE = Path(__file__).resolve().parent.parent
DATA = HERE / "data"
LOG_DIR = Path(os.getenv("LOG_DIR", DATA / "logs"))
TRACE_DIR = Path(os.getenv("TRACE_DIR", DATA / "traces"))
USAGE_PATH = Path(os.getenv("USAGE_PATH", DATA / "usage.jsonl"))

LOGGER_NAME = "auto_browse.agent"


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
    """Configure the agent logger: readable console + appended data/logs/app.log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(LOGGER_NAME)
    log.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    log.handlers.clear()
    log.propagate = False

    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(run_id)s] %(message)s",
                            datefmt="%H:%M:%S")
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
        record = {"run_id": self.run_id, "seq": self._seq, "ts": _utc_now(),
                  "type": type, **fields}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def append_usage(record: dict[str, Any]) -> None:
    """Append one run's usage record to data/usage.jsonl (all runs accumulate)."""
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with USAGE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
