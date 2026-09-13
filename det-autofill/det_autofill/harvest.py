"""Harvest real ARIA fragments from auto-browse traces into test fixtures.

Every fetch_aria tool_result in the traces is a real ARIA fragment from a live
ATS page — labels, roles, refs, options, exactly what the filler will see.
Dedupe by content hash, tag with the run's host, write one .txt per fragment
plus an index.jsonl.

Usage: uv run python -m det_autofill.harvest [traces_dir] [out_dir]
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

DEFAULT_TRACES = Path(__file__).resolve().parents[2] / "auto-browse" / "output" / "traces"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "fragments"

_WRAP_RE = re.compile(r"<PAGE_CONTENT untrusted>\n?(.*?)\n?</PAGE_CONTENT>", re.S)


def unwrap(result: str) -> str:
    m = _WRAP_RE.search(result)
    return (m.group(1) if m else result).strip()


def harvest(traces_dir: Path = DEFAULT_TRACES, out_dir: Path = DEFAULT_OUT) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    index = []
    for tf in sorted(traces_dir.glob("*.jsonl")):
        host = ""
        for line in tf.open():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "run_start":
                m = re.search(r"https?://([^/\s]+)", ev.get("task", ""))
                host = m.group(1) if m else ""
            if ev.get("type") != "tool_result" or ev.get("name") != "fetch_aria":
                continue
            body = unwrap(str(ev.get("result", "")))
            if "[ref=" not in body:  # no interactive content
                continue
            h = hashlib.sha1(body.encode()).hexdigest()[:16]
            if h in seen:
                continue
            seen.add(h)
            name = f"{h}.txt"
            (out_dir / name).write_text(body)
            index.append({"file": name, "host": host, "run": tf.stem})
    (out_dir / "index.jsonl").write_text(
        "\n".join(json.dumps(r) for r in index) + "\n"
    )
    return {"fragments": len(index), "out": str(out_dir)}


if __name__ == "__main__":
    traces = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TRACES
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    print(harvest(traces, out))
