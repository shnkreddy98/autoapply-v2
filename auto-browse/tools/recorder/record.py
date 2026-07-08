"""Human-driven application recorder.

Opens a list of job URLs as tabs in a real (headed, persistent-profile) Chromium
that YOU drive. As you apply, it records each page's raw ARIA accessibility tree
plus a screenshot on every meaningful DOM event (click / change / submit / blur)
and on every navigation. The corpus is used to design the proxy's deterministic
page-model extractor (see plan: zany-giggling-pumpkin).

The captured ARIA snapshots use Playwright's `aria_snapshot()` YAML, which is the
same `- role "name"` format `proxy/tree_parser.parse()` already consumes (minus the
`[ref=eN]` ids).

Run:
    uv run python -m tools.recorder.record tools/recorder/urls.example.txt
    uv run python -m tools.recorder.record urls.txt --out tools/recorder/recordings

Then apply to each job, tab by tab. Close a tab when done. Ctrl-C to stop.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Page, async_playwright

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "recordings"
DEFAULT_PROFILE = HERE / ".chrome-profile"

# Injected into every page/frame before load. Capturing-phase listeners fire before
# the site's own handlers, so we observe the acted-on element. `input` (per-keystroke)
# is intentionally NOT listened to — we capture on change/blur/submit/click only.
INIT_SCRIPT = r"""
(() => {
  if (window.__recInstalled) return;
  window.__recInstalled = true;

  const accName = (el) => {
    if (!el || el.nodeType !== 1) return "";
    const aria = el.getAttribute && el.getAttribute("aria-label");
    if (aria) return aria.trim();
    const labelledby = el.getAttribute && el.getAttribute("aria-labelledby");
    if (labelledby) {
      const t = labelledby.split(/\s+/).map(id => {
        const n = document.getElementById(id); return n ? n.textContent : "";
      }).join(" ").trim();
      if (t) return t;
    }
    if (el.labels && el.labels.length) {
      const t = Array.from(el.labels).map(l => l.textContent).join(" ").trim();
      if (t) return t;
    }
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab && lab.textContent) return lab.textContent.trim();
    }
    const ph = el.getAttribute && el.getAttribute("placeholder");
    if (ph) return ph.trim();
    const txt = (el.textContent || "").trim();
    return txt.slice(0, 120);
  };

  const describe = (el) => {
    if (!el || el.nodeType !== 1) return null;
    return {
      tag: el.tagName ? el.tagName.toLowerCase() : "",
      type: el.getAttribute ? (el.getAttribute("type") || "") : "",
      role: el.getAttribute ? (el.getAttribute("role") || "") : "",
      name: accName(el),
      value: ("value" in el) ? String(el.value || "").slice(0, 200) : "",
      required: !!(el.required || (el.getAttribute && el.getAttribute("aria-required") === "true")),
    };
  };

  const send = (kind) => (e) => {
    try {
      const d = describe(e.target) || {};
      d.event = kind;
      // fire-and-forget; do not block default action
      if (window.__rec) window.__rec(d);
    } catch (err) { /* ignore */ }
  };

  document.addEventListener("click", send("click"), true);
  document.addEventListener("change", send("change"), true);
  document.addEventListener("submit", send("submit"), true);
  document.addEventListener("blur", send("blur"), true);
})();
"""


class Recorder:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = (self.out_dir / "manifest.jsonl").open("a", encoding="utf-8")
        self._lock = asyncio.Lock()
        self._tab_dirs: dict[int, Path] = {}
        self._tab_seq: dict[int, int] = {}
        self._tab_index = 0
        self._snap_hashes: dict[int, str] = {}  # last snap hash per tab (dedup)

    def _tab_dir(self, page: Page) -> Path:
        pid = id(page)
        if pid not in self._tab_dirs:
            self._tab_index += 1
            host = urlsplit(page.url).netloc or "blank"
            d = self.out_dir / f"{self._tab_index:02d}-{host}"
            d.mkdir(parents=True, exist_ok=True)
            self._tab_dirs[pid] = d
            self._tab_seq[pid] = 0
        return self._tab_dirs[pid]

    async def capture(self, page: Page, event: str, target: dict | None) -> None:
        """Snapshot + screenshot the page now, and append a manifest line."""
        if page.is_closed():
            return
        try:
            snap = await page.locator("html").aria_snapshot()
        except Exception:
            return  # page navigating / detached — the load handler will catch it
        async with self._lock:
            pid = id(page)
            tab_dir = self._tab_dir(page)
            digest = hashlib.sha1(snap.encode("utf-8", "replace")).hexdigest()
            seq = self._tab_seq[pid]
            snap_file = tab_dir / f"snap_{seq:04d}.txt"
            shot_file = tab_dir / f"shot_{seq:04d}.png"

            dup = self._snap_hashes.get(pid) == digest
            if not dup:
                snap_file.write_text(snap, encoding="utf-8")
                self._snap_hashes[pid] = digest
            else:
                # reference the previous (identical) snapshot; skip rewrite
                snap_file = tab_dir / f"snap_{seq - 1:04d}.txt"

            try:
                if not dup:
                    await page.screenshot(path=str(shot_file))
                else:
                    shot_file = tab_dir / f"shot_{seq - 1:04d}.png"
            except Exception:
                shot_file = None  # type: ignore[assignment]

            rec = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "tab": tab_dir.name,
                "url": page.url,
                "title": (await _safe_title(page)),
                "event": event,
                "target": target,
                "snapshot_file": str(snap_file.relative_to(self.out_dir)),
                "screenshot_file": (
                    str(shot_file.relative_to(self.out_dir)) if shot_file else None
                ),
                "snapshot_unchanged": dup,
            }
            self.manifest.write(json.dumps(rec) + "\n")
            self.manifest.flush()
            if not dup:
                self._tab_seq[pid] = seq + 1
            print(f"  rec [{rec['tab']}] {event:8} {page.url[:70]}", flush=True)

    def close(self) -> None:
        self.manifest.close()


async def _safe_title(page: Page) -> str:
    try:
        return await page.title()
    except Exception:
        return ""


async def main() -> int:
    ap = argparse.ArgumentParser(description="Human-driven application recorder")
    ap.add_argument("urls", help="newline-delimited file of job URLs")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="recordings root dir")
    ap.add_argument(
        "--profile", default=str(DEFAULT_PROFILE), help="persistent profile dir"
    )
    args = ap.parse_args()

    urls = [
        u.strip()
        for u in Path(args.urls).read_text(encoding="utf-8").splitlines()
        if u.strip() and not u.strip().startswith("#")
    ]
    if not urls:
        print("No URLs found in", args.urls, file=sys.stderr)
        return 1

    session = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out) / session
    rec = Recorder(out_dir)
    print(f"Recording session -> {out_dir}")
    print(
        f"{len(urls)} tab(s). Apply to each, close the tab when done. Ctrl-C to stop.\n"
    )

    closed = asyncio.Event()
    live = {"n": 0}

    def attach(page: Page) -> None:
        live["n"] += 1

        async def on_event(source: dict, payload: dict) -> None:
            evt = (payload or {}).get("event", "event")
            await rec.capture(source["page"], evt, payload)

        # expose_binding is per-context; bound once below. Per-page nav/load hooks here:
        async def on_load() -> None:
            await rec.capture(page, "navigation", None)

        page.on("load", lambda: asyncio.create_task(on_load()))
        page.on("framenavigated", lambda f: None)  # SPA covered by next click

        def on_close() -> None:
            live["n"] -= 1
            if live["n"] <= 0:
                closed.set()

        page.on("close", lambda: on_close())

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            args.profile,
            headless=False,
            accept_downloads=True,
            viewport={"width": 1400, "height": 900},
        )
        await context.add_init_script(INIT_SCRIPT)

        async def binding(source: dict, payload: dict) -> None:
            try:
                await rec.capture(
                    source["page"], (payload or {}).get("event", "event"), payload
                )
            except Exception:
                pass

        await context.expose_binding("__rec", binding)
        context.on("page", lambda p: attach(p))

        # Open all URLs as tabs up front.
        for url in urls:
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                print(f"  goto failed {url}: {exc}", flush=True)
            await rec.capture(page, "open", None)

        try:
            await closed.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            rec.close()
            try:
                await context.close()
            except Exception:
                pass

    print(f"\nDone. Corpus at {out_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
