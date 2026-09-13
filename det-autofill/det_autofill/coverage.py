"""Match-rate report: run parser + taxonomy + answers over harvested fixtures.

THE gate metric: of required fillable fields seen across real traces, what %
does the deterministic pipeline resolve to an answer (matched AND answerable)?
Target >= 80% before going live (P3).

Usage: uv run python -m det_autofill.coverage [fragments_dir]
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from det_autofill import taxonomy
from det_autofill.answers import AnswerStore, example_profile
from det_autofill.aria import fillable, parse_snapshot

DEFAULT_FRAGMENTS = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "fragments"


def report(fragments_dir: Path = DEFAULT_FRAGMENTS) -> dict:
    store = AnswerStore(example_profile())
    idx = {
        json.loads(l)["file"]: json.loads(l)["host"]
        for l in (fragments_dir / "index.jsonl").open()
    }

    tot = Counter()  # all / required
    hit = Counter()  # matched / matched+answered, same split
    unmatched = Counter()
    per_host = defaultdict(lambda: [0, 0])  # required: [answered, total]
    seen_labels: set[tuple[str, str]] = set()  # (host, label) dedupe

    for f, host in idx.items():
        fields = fillable(parse_snapshot((fragments_dir / f).read_text()))
        for fld in fields:
            key = (host, taxonomy.normalize(fld.label or fld.context))
            if not key[1] or key in seen_labels:
                continue
            seen_labels.add(key)
            rule = taxonomy.match(fld)
            answered = bool(rule and store.get(rule.key, fld.label))
            tot["all"] += 1
            hit["all_matched"] += bool(rule)
            hit["all_answered"] += answered
            if fld.required:
                tot["req"] += 1
                hit["req_matched"] += bool(rule)
                hit["req_answered"] += answered
                per_host[host][1] += 1
                per_host[host][0] += answered
            if not rule:
                unmatched[fld.label[:70]] += 1

    pct = lambda a, b: round(100 * a / b, 1) if b else 0.0  # noqa: E731
    out = {
        "fields_unique": tot["all"],
        "matched_pct": pct(hit["all_matched"], tot["all"]),
        "answered_pct": pct(hit["all_answered"], tot["all"]),
        "required_unique": tot["req"],
        "required_matched_pct": pct(hit["req_matched"], tot["req"]),
        "required_answered_pct": pct(hit["req_answered"], tot["req"]),
    }
    print(json.dumps(out, indent=2))
    print("\nWorst hosts (required answered/total):")
    for h, (a, t) in sorted(per_host.items(), key=lambda kv: kv[1][0] / kv[1][1] if kv[1][1] else 1)[:8]:
        if t:
            print(f"  {a:3}/{t:<3} {h}")
    print("\nTop unmatched labels:")
    for lbl, n in unmatched.most_common(25):
        print(f"  {n:3}  {lbl}")
    return out


if __name__ == "__main__":
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FRAGMENTS
    report(d)
