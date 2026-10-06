"""Side-by-side report of several bench runs (setups) over the same sites.

    uv run python -m bench compare "det only=output/bench/<run>" "det + Jev=output/bench/<run>" ...

A setup may be split over several run directories (the agent runs one site group per
process): give them joined with '+'. Per setup the report has mean score, mean
required, wrong answers, submitted count, model calls and cost, then a site table and
every wrong answer.

"Unbacked" counts fields whose answer key is {"any": true, "personal": true}: personal
facts the profile does not hold (a driver's license, a felony, references, a relative at
the company). The score takes any value there, so filling one means the engine stated a
fact about the applicant that nobody gave it. Leaving it for a person is the honest
outcome; this column is where a confident guess shows. Harmless preferences with no
right answer (phone type, contact method) are not counted.
"""

from __future__ import annotations

import json
from pathlib import Path

from bench.server import load_specs

JEV_PRICE = 0.042 / 1e6  # $ per input token, jev-1.13 (docs: Models)


def load_setup(spec: str) -> tuple[str, list[dict], list[dict]]:
    label, _, paths = spec.partition("=")
    results, metas = [], []
    for p in paths.split("+"):
        d = json.loads((Path(p) / "results.json").read_text())
        results += d["results"]
        metas.append(d["meta"])
    return label.strip(), results, metas


def _unbacked_ids(specs: dict) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for site, spec in specs.items():
        for step in spec["steps"]:
            for sec in step["sections"]:
                for f in sec["fields"]:
                    exp = f.get("expect") or {}
                    if exp.get("any") and exp.get("personal"):
                        out.setdefault(site, set()).add(f["id"])
    return out


def build(setups: list[str]) -> str:
    specs = load_specs()
    unbacked = _unbacked_ids(specs)
    loaded = [load_setup(s) for s in setups]
    sites = sorted({r["site"] for _, results, _ in loaded for r in results})
    lines = ["# Apply bench: setups compared", "",
             "Same 20 mock sites, same synthetic applicant (`bench/profile.json`), engines allowed to submit.", "",
             "| setup | model | mean score | mean required | wrong | unbacked | submitted | model calls | cost |",
             "|---|---|---|---|---|---|---|---|---|"]
    by_setup = {}
    for label, results, metas in loaded:
        rows = {r["site"]: r for r in results}
        by_setup[label] = rows
        valid = [r for r in results if r["engine"].get("valid", True)]
        n = len(valid) or 1
        cost = sum(r["engine"].get("cost_usd") or 0 for r in results)
        cost += sum(r["engine"].get("jev_input_tokens", 0) for r in results) * JEV_PRICE
        calls = sum(r["engine"].get("model_calls") or 0 for r in results)
        unb = sum(1 for r in results for f in r["score"]["fields"]
                  if f["id"] in unbacked.get(r["site"], ()) and f["status"] == "correct")
        model = metas[0].get("model") or "none"
        if metas[0].get("model_only"):
            model += " (every field)"
        cost_txt = f"${cost:.3f}" if cost else ("free" if model == "none" else "see note")
        lines.append(f"| {label} | {model} | {sum(r['score']['score'] for r in valid) / n:.1f}% "
                     f"| {sum(r['score']['required_score'] for r in valid) / n:.1f}% | {sum(r['score']['wrong'] for r in results)} "
                     f"| {unb} | {sum(r['score']['submitted'] for r in results)}/{len(results)} | {calls} | {cost_txt} |")
    labels = [label for label, _, _ in loaded]
    lines += ["", "## Score per site (submitted = ✓)", "", "| site | " + " | ".join(labels) + " |",
              "|---|" + "---|" * len(labels)]
    for site in sites:
        cells = []
        for label in labels:
            r = by_setup[label].get(site)
            if not r:
                cells.append("—")
                continue
            s = r["score"]
            mark = " ✓" if s["submitted"] else ""
            flag = " (invalid)" if not r["engine"].get("valid", True) else ""
            cells.append(f"{s['score']}%{mark}{flag}")
        lines.append(f"| {site} | " + " | ".join(cells) + " |")
    lines += ["", "## Wrong answers", ""]
    for label in labels:
        wrong = [(site, f) for site, r in sorted(by_setup[label].items()) for f in r["score"]["fields"] if f["status"] == "wrong"]
        lines.append(f"**{label}** ({len(wrong)})")
        lines.append("")
        for site, f in wrong:
            lines.append(f"- {site}: {f['label'][:70]}: expected `{str(f['expected'])[:40]}`, got `{str(f['got'])[:50]}`"
                         + (f" ({f['source']})" if f.get("source") else ""))
        lines.append("")
    lines += ["## Unbacked answers (profile cannot answer; the engine filled them anyway)", ""]
    for label in labels:
        hits = [(site, f) for site, r in sorted(by_setup[label].items()) for f in r["score"]["fields"]
                if f["id"] in unbacked.get(site, ()) and f["status"] == "correct"]
        lines.append(f"- **{label}**: " + (", ".join(f"{site} {f['label'][:40]!r} = `{str(f['got'])[:25]}`" for site, f in hits) or "none"))
    return "\n".join(lines) + "\n"
