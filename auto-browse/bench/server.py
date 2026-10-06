"""Mock ATS server. Every run gets its own URL prefix, /r/<run>/<site>/..., so two
runs never share a record; the page posts what it holds to /state every tick and the
final snapshot to /submit. Submitting here sends nothing anywhere — the bench lets an
engine press Submit, which the live ATS check can never do.

    /                         index of sites (for poking at the clones by hand)
    /r/<run>/<site>/job       posting (Greenhouse: posting and form on one page)
    /r/<run>/<site>/apply     application
    /r/<run>/<site>/record    what the page last reported (JSON)
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from bench import render

HERE = Path(__file__).resolve().parent
SITES_DIR = HERE / "sites"


def load_specs() -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text()) for p in sorted(SITES_DIR.glob("*.json"))}


class Recorder:
    """Per (run, site): the last state the page reported, and the submission."""

    def __init__(self):
        self.lock = threading.Lock()
        self.records: dict[tuple[str, str], dict] = {}

    def _rec(self, run: str, site: str) -> dict:
        return self.records.setdefault((run, site), {"state": None, "submitted": None, "posts": 0, "pages": []})

    def visit(self, run: str, site: str, page: str) -> None:
        with self.lock:
            self._rec(run, site)["pages"].append({"page": page, "at": time.time()})

    def state(self, run: str, site: str, body: dict) -> None:
        with self.lock:
            rec = self._rec(run, site)
            rec["state"], rec["posts"] = body, rec["posts"] + 1

    def submit(self, run: str, site: str, body: dict) -> None:
        with self.lock:
            rec = self._rec(run, site)
            rec["state"] = rec["submitted"] = body | {"submitted_at": time.time()}

    def get(self, run: str, site: str) -> dict:
        with self.lock:
            return json.loads(json.dumps(self._rec(run, site)))


def make_app(recorder: Recorder, specs: dict[str, dict] | None = None) -> Starlette:
    specs = specs or load_specs()

    def spec_for(request: Request) -> tuple[dict, str, str, str]:
        run, site = request.path_params["run"], request.path_params["site"]
        if site not in specs:
            raise KeyError(site)
        return specs[site], run, site, f"/r/{run}/{site}"

    async def index(request: Request):
        rows = "".join(f'<li><a href="/r/manual/{s}/job">{s}</a> — {spec["company"]}: {spec["job"]["title"]}</li>'
                       for s, spec in specs.items())
        return HTMLResponse(f"<!doctype html><title>Apply bench</title><h1>Apply bench</h1><ul>{rows}</ul>")

    async def job(request: Request):
        spec, run, site, base = spec_for(request)
        recorder.visit(run, site, "job")
        return HTMLResponse(render.posting_page(spec, base))

    async def apply(request: Request):
        spec, run, site, base = spec_for(request)
        in_frame = request.query_params.get("in_iframe") == "1"
        recorder.visit(run, site, "apply-frame" if in_frame else "apply")
        return HTMLResponse(render.application_page(spec, base, in_frame=in_frame))

    async def state(request: Request):
        _, run, site, _ = spec_for(request)
        recorder.state(run, site, await request.json())
        return JSONResponse({"ok": True})

    async def submit(request: Request):
        _, run, site, _ = spec_for(request)
        recorder.submit(run, site, await request.json())
        return JSONResponse({"ok": True})

    async def record(request: Request):
        _, run, site, _ = spec_for(request)
        return JSONResponse(recorder.get(run, site))

    return Starlette(routes=[
        Route("/", index),
        Route("/r/{run}/{site}/job", job),
        Route("/r/{run}/{site}/apply", apply),
        Route("/r/{run}/{site}/state", state, methods=["POST"]),
        Route("/r/{run}/{site}/submit", submit, methods=["POST"]),
        Route("/r/{run}/{site}/record", record),
        Mount("/static", StaticFiles(directory=HERE / "static")),
    ])


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


class BackgroundServer:
    """uvicorn in a daemon thread, so the engine can run on the caller's event loop."""

    def __init__(self, app, host: str = "127.0.0.1", port: int | None = None):
        self.host, self.port = host, port or free_port(host)
        self.server = uvicorn.Server(uvicorn.Config(app, host=host, port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError(f"bench server did not start on {self.host}:{self.port}")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=5)
