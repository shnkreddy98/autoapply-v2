"""Local web viewer for agent run traces (data/traces/*.jsonl).

Pick a trace from the sidebar; the main pane stacks each iteration top-to-bottom:
the model's reasoning, every tool call with its full arguments, and — paired right
under the call — that call's tool result (page model / snapshot) in a scrollable
monospace box so you can read exactly what the agent saw.

Zero dependencies (stdlib http.server). Run:

    python tools/trace_viewer.py            # serves http://127.0.0.1:8000
    python tools/trace_viewer.py --port 9000 --dir data/traces

Then open the printed URL.
"""

from __future__ import annotations

import argparse
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TRACE_DIR = Path("data/traces")


def _read_events(path: Path) -> list[dict]:
    events: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _summary(path: Path) -> dict:
    """Lightweight meta for the sidebar list, derived from a trace's events."""
    events = _read_events(path)
    start = next((e for e in events if e.get("type") == "run_start"), {})
    end = next((e for e in reversed(events) if e.get("type") == "run_end"), {})
    return {
        "name": path.name,
        "model": start.get("model", ""),
        "task": (start.get("task", "") or "").strip(),
        "ts": start.get("ts", ""),
        "outcome": end.get("outcome", "running?"),
        "success": end.get("success"),
        "iterations": end.get("iterations"),
        "duration_s": end.get("duration_s"),
    }


def _list_traces(trace_dir: Path) -> list[dict]:
    files = sorted(trace_dir.glob("*.jsonl"), reverse=True)
    out = []
    for f in files:
        try:
            out.append(_summary(f))
        except Exception as exc:  # never let one bad file break the list
            out.append({"name": f.name, "task": f"(unreadable: {exc})", "outcome": "error"})
    return out


def _make_handler(trace_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # quiet
            pass

        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200) -> None:
            self._send(json.dumps(obj).encode(), "application/json; charset=utf-8", status)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            route = parsed.path
            if route == "/":
                self._send(INDEX_HTML.encode(), "text/html; charset=utf-8")
            elif route == "/api/traces":
                self._json(_list_traces(trace_dir))
            elif route == "/api/trace":
                qs = parse_qs(parsed.query)
                name = (qs.get("name") or [""])[0]
                # guard against path traversal — accept a bare filename only
                safe = Path(name).name
                path = trace_dir / safe
                if not safe or not path.is_file():
                    self._json({"error": "trace not found"}, 404)
                    return
                self._json({"name": safe, "events": _read_events(path)})
            else:
                self._send(b"not found", "text/plain", 404)

    return Handler


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trace Viewer</title>
<style>
  :root {
    --bg: #0f1116; --panel: #171a21; --panel2: #1d212b; --border: #2a2f3a;
    --text: #d8dde6; --muted: #8b93a3; --accent: #5b9dff; --accent2: #9d7bff;
    --ok: #43c08a; --warn: #e6b54a; --err: #ef6a6a; --code: #0b0d12;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; }
  body {
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: var(--text); background: var(--bg); display: flex; height: 100vh; overflow: hidden;
  }
  /* sidebar */
  #sidebar {
    width: 320px; min-width: 320px; border-right: 1px solid var(--border);
    background: var(--panel); display: flex; flex-direction: column; height: 100%;
  }
  #sidebar h1 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em;
    color: var(--muted); margin: 0; padding: 14px 16px; border-bottom: 1px solid var(--border); }
  #filter { margin: 10px; padding: 8px 10px; width: calc(100% - 20px); background: var(--code);
    border: 1px solid var(--border); border-radius: 6px; color: var(--text); }
  #trace-list { overflow-y: auto; flex: 1; }
  .trace-item { padding: 10px 14px; border-bottom: 1px solid var(--border); cursor: pointer; }
  .trace-item:hover { background: var(--panel2); }
  .trace-item.active { background: #233049; border-left: 3px solid var(--accent); padding-left: 11px; }
  .trace-item .name { font-family: ui-monospace, monospace; font-size: 11px; color: var(--muted); }
  .trace-item .task { font-size: 12.5px; margin: 3px 0; color: var(--text);
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .trace-item .meta { font-size: 11px; color: var(--muted); display: flex; gap: 8px; align-items: center; }
  .badge { font-size: 10.5px; padding: 1px 7px; border-radius: 999px; font-weight: 600; }
  .badge.ok { background: rgba(67,192,138,.18); color: var(--ok); }
  .badge.err { background: rgba(239,106,106,.18); color: var(--err); }
  .badge.warn { background: rgba(230,181,74,.18); color: var(--warn); }
  .badge.neutral { background: var(--panel2); color: var(--muted); }
  /* main */
  #main { flex: 1; overflow-y: auto; height: 100%; }
  #empty { color: var(--muted); padding: 60px; text-align: center; font-size: 15px; }
  #run-header { position: sticky; top: 0; z-index: 5; background: var(--panel);
    border-bottom: 1px solid var(--border); padding: 16px 24px; }
  #run-header .task { font-size: 16px; font-weight: 600; margin-bottom: 8px; white-space: pre-wrap; }
  #run-header .stats { display: flex; flex-wrap: wrap; gap: 8px 16px; font-size: 12.5px; color: var(--muted); }
  #run-header .stats b { color: var(--text); font-weight: 600; }
  .toolbar { display: flex; gap: 8px; margin-top: 10px; }
  .toolbar button { background: var(--panel2); color: var(--text); border: 1px solid var(--border);
    padding: 5px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  .toolbar button:hover { border-color: var(--accent); }
  details.sys { margin-top: 10px; font-size: 12.5px; }
  details.sys summary { cursor: pointer; color: var(--muted); }
  details.sys pre { margin-top: 8px; }
  #timeline { padding: 18px 24px 120px; }
  .iter { margin-bottom: 22px; border: 1px solid var(--border); border-radius: 10px;
    overflow: hidden; background: var(--panel); }
  .iter-head { display: flex; align-items: center; gap: 10px; padding: 8px 14px;
    background: var(--panel2); border-bottom: 1px solid var(--border); }
  .iter-num { font-weight: 700; color: var(--accent); font-size: 13px; }
  .iter-head .ts { color: var(--muted); font-size: 11px; margin-left: auto; }
  .section { padding: 12px 14px; border-bottom: 1px solid var(--border); }
  .section:last-child { border-bottom: none; }
  .section-label { font-size: 10.5px; text-transform: uppercase; letter-spacing: .07em;
    color: var(--muted); margin-bottom: 6px; }
  .reasoning { color: #c8d3e6; white-space: pre-wrap; font-size: 13.5px; }
  .tool-call { border: 1px solid var(--border); border-radius: 8px; margin-top: 10px; background: var(--code); }
  .tool-call:first-of-type { margin-top: 0; }
  .tc-head { display: flex; align-items: center; gap: 10px; padding: 8px 12px; }
  .tc-name { font-family: ui-monospace, monospace; font-weight: 700; color: var(--accent2); }
  .tc-args { margin: 0 12px 10px; }
  pre { background: var(--code); border: 1px solid var(--border); border-radius: 6px;
    padding: 10px 12px; margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px; color: #cdd6e4; overflow: auto; }
  pre.args { max-height: 220px; }
  .result-wrap { margin: 8px 12px 12px; }
  .result-wrap .rlabel { display: flex; align-items: center; gap: 8px; font-size: 11px;
    color: var(--muted); margin-bottom: 5px; }
  pre.result { max-height: 460px; white-space: pre; line-height: 1.45; }
  pre.result.wrap { white-space: pre-wrap; word-break: break-word; }
  .mini-btn { background: var(--panel2); border: 1px solid var(--border); color: var(--muted);
    font-size: 10.5px; padding: 1px 7px; border-radius: 5px; cursor: pointer; }
  .mini-btn:hover { color: var(--text); border-color: var(--accent); }
  .guard { background: rgba(230,181,74,.12); border: 1px solid var(--warn); color: var(--warn);
    border-radius: 8px; padding: 10px 14px; margin-bottom: 22px; font-size: 13px; }
  .run-end { border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px;
    background: var(--panel); }
  .run-end .final { white-space: pre-wrap; margin-top: 8px; color: #c8d3e6; }
  code.inline { background: var(--code); padding: 1px 6px; border-radius: 4px;
    font-family: ui-monospace, monospace; font-size: 12px; }
</style>
</head>
<body>
  <aside id="sidebar">
    <h1>Traces</h1>
    <input id="filter" placeholder="filter by task / name…" autocomplete="off">
    <div id="trace-list"></div>
  </aside>
  <main id="main">
    <div id="empty">Select a trace from the left.</div>
  </main>

<script>
const $ = (s, r=document) => r.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag);
  if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const esc = s => (s ?? "").toString();

let TRACES = [];
let ACTIVE = null;

function badgeFor(outcome, success) {
  const o = (outcome || "").toLowerCase();
  let cls = "neutral";
  if (success === true || o.includes("success") || o.includes("complete") || o.includes("paused")) cls = "ok";
  else if (o.includes("block") || o.includes("error") || o.includes("fail")) cls = "err";
  else if (o.includes("running") || o === "") cls = "warn";
  const b = el("span", "badge " + cls, outcome || "—");
  return b;
}

async function loadList() {
  TRACES = await (await fetch("/api/traces")).json();
  renderList();
}

function renderList() {
  const q = $("#filter").value.toLowerCase();
  const list = $("#trace-list");
  list.innerHTML = "";
  TRACES.filter(t => !q || (t.task||"").toLowerCase().includes(q) || (t.name||"").toLowerCase().includes(q))
    .forEach(t => {
      const item = el("div", "trace-item" + (t.name === ACTIVE ? " active" : ""));
      item.appendChild(el("div", "name", t.name));
      item.appendChild(el("div", "task", t.task || "(no task)"));
      const meta = el("div", "meta");
      meta.appendChild(badgeFor(t.outcome, t.success));
      if (t.iterations != null) meta.appendChild(el("span", null, t.iterations + " iters"));
      if (t.duration_s != null) meta.appendChild(el("span", null, Math.round(t.duration_s) + "s"));
      if (t.model) meta.appendChild(el("span", null, t.model.split("/").pop()));
      item.appendChild(meta);
      item.onclick = () => loadTrace(t.name);
      list.appendChild(item);
    });
}

function prettyArgs(args) {
  if (args == null) return "";
  if (typeof args === "string") {
    try { return JSON.stringify(JSON.parse(args), null, 2); }
    catch { return args; }
  }
  return JSON.stringify(args, null, 2);
}

function resultBlock(res, ok) {
  const wrap = el("div", "result-wrap");
  const rlabel = el("div", "rlabel");
  rlabel.appendChild(el("span", null, "result"));
  rlabel.appendChild(badgeFor(ok === false ? "error" : "ok", ok !== false));
  const wrapBtn = el("button", "mini-btn", "wrap");
  const copyBtn = el("button", "mini-btn", "copy");
  rlabel.appendChild(wrapBtn);
  rlabel.appendChild(copyBtn);
  wrap.appendChild(rlabel);
  const pre = el("pre", "result", esc(res));
  wrapBtn.onclick = () => pre.classList.toggle("wrap");
  copyBtn.onclick = () => navigator.clipboard.writeText(esc(res));
  wrap.appendChild(pre);
  return wrap;
}

function toolCallBlock(call, result) {
  const box = el("div", "tool-call");
  const head = el("div", "tc-head");
  head.appendChild(el("span", "tc-name", call.name || "?"));
  if (result && result.ok === false) head.appendChild(badgeFor("error", false));
  box.appendChild(head);
  const args = prettyArgs(call.arguments);
  if (args && args !== "{}") {
    const apre = el("pre", "args", args);
    const aw = el("div", "tc-args"); aw.appendChild(apre);
    box.appendChild(aw);
  }
  if (result) box.appendChild(resultBlock(result.result, result.ok));
  return box;
}

async function loadTrace(name) {
  ACTIVE = name;
  renderList();
  const data = await (await fetch("/api/trace?name=" + encodeURIComponent(name))).json();
  renderTrace(data.events || []);
}

function renderTrace(events) {
  const main = $("#main");
  main.innerHTML = "";

  // index tool_result by tool_call_id so each call shows its own result
  const resultById = {};
  const resultsByIter = {};
  for (const e of events) {
    if (e.type === "tool_result") {
      if (e.tool_call_id) resultById[e.tool_call_id] = e;
      (resultsByIter[e.iteration] = resultsByIter[e.iteration] || []).push(e);
    }
  }

  const start = events.find(e => e.type === "run_start") || {};
  const end = events.find(e => e.type === "run_end");

  // header
  const header = el("div", "");
  header.id = "run-header";
  header.appendChild(el("div", "task", start.task || "(no task)"));
  const stats = el("div", "stats");
  const stat = (label, val) => { const s = el("span"); s.innerHTML =
    label + " <b>" + esc(val) + "</b>"; stats.appendChild(s); };
  if (start.model) stat("model", start.model);
  if (end) {
    stat("outcome", end.outcome); stat("iterations", end.iterations);
    if (end.duration_s != null) stat("duration", Math.round(end.duration_s) + "s");
    if (end.usage_totals) {
      stat("tokens", (end.usage_totals.total_tokens || 0).toLocaleString());
      if (end.usage_totals.cost != null) stat("cost", "$" + end.usage_totals.cost.toFixed(4));
    }
  }
  header.appendChild(stats);
  if (start.tools) {
    const d = el("details", "sys");
    d.appendChild(el("summary", null, "tools (" + start.tools.length + ")"));
    d.appendChild(el("pre", null, start.tools.join(", ")));
    header.appendChild(d);
  }
  if (start.system_prompt) {
    const d = el("details", "sys");
    d.appendChild(el("summary", null, "system prompt"));
    d.appendChild(el("pre", null, start.system_prompt));
    header.appendChild(d);
  }
  const toolbar = el("div", "toolbar");
  const expandAll = el("button", null, "expand all results");
  const collapseAll = el("button", null, "collapse all results");
  expandAll.onclick = () => document.querySelectorAll("pre.result").forEach(p => p.style.maxHeight = "none");
  collapseAll.onclick = () => document.querySelectorAll("pre.result").forEach(p => p.style.maxHeight = "");
  toolbar.appendChild(expandAll); toolbar.appendChild(collapseAll);
  header.appendChild(toolbar);
  main.appendChild(header);

  const timeline = el("div", ""); timeline.id = "timeline";

  const consumed = new Set();
  for (const e of events) {
    if (e.type === "llm_turn") {
      const iter = el("div", "iter");
      const head = el("div", "iter-head");
      head.appendChild(el("span", "iter-num", "iteration " + (e.iteration ?? "?")));
      if (e.usage) head.appendChild(el("span", "badge neutral",
        (e.usage.total || 0) + " tok"));
      if (e.ts) head.appendChild(el("span", "ts", e.ts.replace("T", " ").replace("+00:00", "")));
      iter.appendChild(head);

      if (e.assistant_content && e.assistant_content.trim()) {
        const sec = el("div", "section");
        sec.appendChild(el("div", "section-label", "reasoning"));
        sec.appendChild(el("div", "reasoning", e.assistant_content));
        iter.appendChild(sec);
      }

      const calls = e.tool_calls || [];
      if (calls.length) {
        const sec = el("div", "section");
        sec.appendChild(el("div", "section-label", "tool call" + (calls.length > 1 ? "s" : "")));
        for (const c of calls) {
          const res = c.id ? resultById[c.id] : null;
          if (res) consumed.add(res);
          sec.appendChild(toolCallBlock(c, res));
        }
        iter.appendChild(sec);
      } else if (e.finished) {
        const sec = el("div", "section");
        sec.appendChild(el("div", "section-label", "final answer"));
        sec.appendChild(el("div", "reasoning", e.assistant_content || "(no text)"));
        // avoid duplicating reasoning section above
        iter.appendChild(sec);
      }
      timeline.appendChild(iter);
    } else if (e.type === "tool_result" && !consumed.has(e)) {
      // result with no matching call id — show standalone so nothing is lost
      const iter = el("div", "iter");
      const head = el("div", "iter-head");
      head.appendChild(el("span", "iter-num", "iteration " + (e.iteration ?? "?")));
      head.appendChild(el("span", "badge neutral", "orphan result"));
      iter.appendChild(head);
      const sec = el("div", "section");
      sec.appendChild(toolCallBlock({ name: e.name, arguments: e.arguments }, e));
      iter.appendChild(sec);
      timeline.appendChild(iter);
    } else if (e.type === "repeat_guard") {
      const g = el("div", "guard");
      g.innerHTML = "⚠ repeat guard fired at iteration <b>" + esc(e.iteration) +
        "</b> — same action repeated <b>" + esc(e.count) + "×</b>. signature: <code class='inline'>" +
        esc(e.signature) + "</code>";
      timeline.appendChild(g);
    }
  }

  if (end) {
    const re = el("div", "run-end");
    re.appendChild(el("div", "section-label", "run end — " + esc(end.outcome)));
    const stats2 = el("div", "stats");
    const s2 = (l, v) => { const s = el("span"); s.innerHTML = l + " <b>" + esc(v) + "</b>"; stats2.appendChild(s); };
    s2("success", end.success); s2("iterations", end.iterations);
    if (end.duration_s != null) s2("duration", Math.round(end.duration_s) + "s");
    if (end.error) s2("error", end.error);
    re.appendChild(stats2);
    if (end.final_text && end.final_text.trim())
      re.appendChild(el("div", "final", end.final_text));
    timeline.appendChild(re);
  }

  main.appendChild(timeline);
  main.scrollTop = 0;
}

$("#filter").addEventListener("input", renderList);
loadList();
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Local web viewer for agent run traces.")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--dir", default=str(TRACE_DIR), help="trace directory (default data/traces)")
    ap.add_argument("--no-open", action="store_true", help="don't auto-open the browser")
    args = ap.parse_args()

    trace_dir = Path(args.dir).resolve()
    if not trace_dir.is_dir():
        raise SystemExit(f"trace dir not found: {trace_dir}")

    server = ThreadingHTTPServer((args.host, args.port), _make_handler(trace_dir))
    url = f"http://{args.host}:{args.port}"
    print(f"Trace viewer serving {trace_dir}")
    print(f"  -> {url}  (Ctrl-C to stop)")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
