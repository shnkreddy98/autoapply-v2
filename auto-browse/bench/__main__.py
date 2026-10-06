"""Apply bench: run an apply engine against mock ATS sites and score what it filled.

    uv run python -m bench check                              # lint the answer keys
    uv run python -m bench selftest                           # oracle must score 100% (free, no model)
    uv run python -m bench compare "det only=output/bench/<run>" "det + Jev=output/bench/<run>"
    uv run python -m bench serve                              # browse the clones: http://127.0.0.1:8940/
    uv run python -m bench run --engine det --no-model        # free: profile answers only
    uv run python -m bench run --engine det --model google/gemini-3-flash-preview
    uv run python -m bench run --engine det --model jev-latest  # Jev picks; written answers stay open
    uv run python -m bench run --engine det --model jev-latest --writer google/gemini-3-flash-preview
    uv run python -m bench run --engine agent --model <openrouter-model-id> --sites ashby
    uv run python -m bench run ... --repeat 3                 # model answers vary: see the spread

Every run costs what the engine costs on a real application: det makes one resolver
call per page plus one per free-text question, agent makes one call per step (tens per
form). --no-model is free. Model calls go to OPENROUTER_BASE_URL with
OPENROUTER_API_KEY from auto-browse/.env (OpenRouter; `run` checks the model id
against the endpoint's model list first, for free). A `jev*` model goes to TypeSafe
(TYPESAFE_API_KEY), det engine only: Jev answers typed questions and writes nothing,
so the agent engine cannot use it.

The engines submit: the sites are local mock pages and submitting is how the bench
learns what the application would have sent. --no-submit stops at the final button,
like `autoapply apply` without --autosubmit.

Nothing touches the database or the user's profile. Results: output/bench/<stamp>/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
load_dotenv(ROOT / ".env")

from bench import engines, score  # noqa: E402
from bench.server import BackgroundServer, Recorder, free_port, load_specs, make_app  # noqa: E402

PROFILE = HERE / "profile.json"
OUT = ROOT / "output" / "bench"
PLAYWRIGHT_MCP_PKG = "@playwright/mcp@0.0.83"


def preflight_model(model: str) -> str:
    """Free check that the endpoint serves this model id (GET /models, no tokens).
    On 2026-09-30 auto-browse/.env still pointed OPENROUTER_BASE_URL at the offline
    RunPod server: every call 404'd and det scored exactly like --no-model."""
    import httpx

    base = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    hint = ("set OPENROUTER_BASE_URL=https://openrouter.ai/api/v1 and an OpenRouter OPENROUTER_API_KEY "
            "in auto-browse/.env; model ids: https://openrouter.ai/models")
    try:
        resp = httpx.get(f"{base}/models", timeout=20,
                         headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY', '')}"})
        resp.raise_for_status()
        ids = {m.get("id") for m in resp.json().get("data", [])}
    except Exception as exc:
        raise SystemExit(f"model endpoint {base} did not list its models ({type(exc).__name__}: {str(exc)[:120]})\n{hint}")
    if model not in ids:
        import difflib

        close = difflib.get_close_matches(model, [i for i in ids if i], n=4, cutoff=0.8)
        raise SystemExit(f"{base} has no model {model!r}" + (f"; close: {', '.join(close)}" if close else "") + f"\n{hint}")
    return base


def load_profile() -> dict:
    doc = json.loads(PROFILE.read_text())
    doc.pop("_comment", None)
    return doc


def cmd_check(args) -> int:
    profile, problems = load_profile(), []
    for spec in load_specs().values():
        problems += score.check_spec(spec, profile)
    for p in problems:
        print(p)
    specs = load_specs()
    fields = sum(len(score.all_fields(s)) for s in specs.values())
    print(f"{len(specs)} site(s), {fields} field(s): {'OK' if not problems else f'{len(problems)} problem(s)'}")
    return 1 if problems else 0


def cmd_selftest(args) -> int:
    from bench import selftest

    specs = load_specs()
    if args.sites:
        specs = {k: v for k, v in specs.items() if k in args.sites.split(",")}
    run_dir = OUT / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-selftest"
    resume = engines.resume_pdf(run_dir / "resume.pdf")
    failures = asyncio.run(selftest.run(specs, load_profile(), str(resume), headed=args.headed))
    for f in failures:
        print(f"FAIL {f}")
    print("selftest: " + ("OK" if not failures else f"{len(failures)} failure(s)"))
    return 1 if failures else 0


def cmd_serve(args) -> int:
    import uvicorn

    print(f"apply bench: http://{args.host}:{args.port}/  (records under run 'manual'; Ctrl-C stops)")
    uvicorn.run(make_app(Recorder()), host=args.host, port=args.port, log_level="warning")
    return 0


# ---------------------------------------------------------------- local browser stack


def _wait_port(port: int, proc: subprocess.Popen, what: str, timeout: float = 60) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"{what} exited with code {proc.returncode}")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.25)
    raise RuntimeError(f"{what} did not open port {port} within {timeout:.0f}s")


@contextmanager
def local_agent_browser(run_dir: Path, headed: bool):
    """A private headless Playwright MCP plus the pruning proxy in front of it, so the
    agent sees the same tool surface as in production without touching the long-lived
    docker browser (which may be mid-application, and holds its session)."""
    if not shutil.which("npx"):
        raise SystemExit("the agent engine needs npx for a local Playwright MCP (or pass --mcp URL)")
    mcp_port, proxy_port = free_port(), free_port()
    out_dir = run_dir / "mcp"
    out_dir.mkdir(parents=True, exist_ok=True)
    log = open(run_dir / "browser-stack.log", "w")
    # bound to loopback; --allowed-hosts '*' only turns off the Host-header check, which
    # refused the proxy's 127.0.0.1 requests to a server that calls itself localhost
    cmd = ["npx", "-y", PLAYWRIGHT_MCP_PKG, "--host", "127.0.0.1", "--port", str(mcp_port), "--allowed-hosts", "*", "--isolated",
           "--output-dir", str(out_dir), "--allow-unrestricted-file-access", "--viewport-size", "1280x900"]
    if not headed:
        cmd.append("--headless")
    exe = engines.chromium_executable() if not headed else None
    if exe:
        cmd += ["--executable-path", exe]
    mcp = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    proxy = None
    try:
        _wait_port(mcp_port, mcp, "Playwright MCP")
        env = os.environ | {"PLAYWRIGHT_MCP_URL": f"http://127.0.0.1:{mcp_port}/mcp", "PROXY_HOST": "127.0.0.1",
                            "PROXY_PORT": str(proxy_port), "SNAPSHOT_DIR": str(run_dir)}
        proxy = subprocess.Popen([sys.executable, "-m", "proxy.server"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        _wait_port(proxy_port, proxy, "pruning proxy")
        yield f"http://127.0.0.1:{proxy_port}/mcp"
    finally:
        for p in (proxy, mcp):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
        log.close()


# ---------------------------------------------------------------- run


def _fmt_sources(by_source: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(by_source.items())) or "-"


def write_report(run_dir: Path, meta: dict, results: list[dict]) -> Path:
    lines = [f"# Apply bench {meta['stamp']}", "",
             f"engine **{meta['engine']}** · model **{meta['model'] or 'none'}** ({meta.get('endpoint') or '-'}) · submit {'on' if meta['submit'] else 'off'}"
             f" · profile `bench/profile.json`", "",
             "| site | run | score | required | wrong | missed | free text | steps | submitted | engine | written by |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    recon = sorted({r["site"] for r in results if r["score"].get("reconstructed_fields")})
    for r in results:
        s = r["score"]
        site = r["site"] if r["engine"].get("valid", True) else f"{r['site']} **INVALID**"
        lines.append(f"| {site} | {r['repeat']} | {s['score']}% ({s['correct']}/{s['checked']}) "
                     f"| {s['required_score']}% ({s['required_done']}/{s['required']}) | {s['wrong']} | {s['missed']} "
                     f"| {s['free_text_answered']}/{s['free_text']} | {s['steps_reached']}/{s['steps']} "
                     f"| {'yes' if s['submitted'] else 'no'} | {r['engine']['status']} | {_fmt_sources(s['by_source'])} |")
    if recon:
        lines += ["", f"Sites with reconstructed steps (no captured page behind their sign-in or code wall): {', '.join(recon)}. "
                  "Fields marked † are on those steps."]
    for r in results:
        s = r["score"]
        bad = [f for f in s["fields"] if f["status"] in ("wrong", "missed", "short")]
        lines += ["", f"## {r['site']} (run {r['repeat']})", f"engine: {r['engine']['status']} — {str(r['engine'].get('reason', ''))[:300]}"]
        if r["engine"].get("model_errors"):
            lines.append(f"INVALID — {len(r['engine']['model_errors'])} model call(s) failed: {r['engine']['model_errors'][0]}")
        if "model_calls" in r["engine"]:
            lines.append(f"model calls: {r['engine']['model_calls']}"
                         + (f", Jev input tokens {r['engine']['jev_input_tokens']:,}" if r["engine"].get("jev_input_tokens") else ""))
        picks = r["engine"].get("jev_picks") or []
        if picks:
            lines += ["", "Jev picks (used = confident enough to fill):", "", "| field | choice | confidence | used |", "|---|---|---|---|"]
            lines += [f"| {p['label'][:60]} | {str(p['choice'])[:50]} | {p['confidence']} | {'yes' if p['used'] else 'no'} |" for p in picks]
        if r["engine"].get("handoffs"):
            lines.append(f"asked for a human {len(r['engine']['handoffs'])}x: " +
                         "; ".join(h.get("reason", "")[:80] for h in r["engine"]["handoffs"]))
        if bad:
            lines += ["", "| field | status | expected | got | req |", "|---|---|---|---|---|"]
            for f in bad:
                lines.append(f"| {'† ' if f.get('reconstructed') else ''}{f['label'][:60]} | {f['status']} | {str(f['expected'])[:50]} | {str(f['got'])[:50]} | {'*' if f['required'] else ''} |")
    path = run_dir / "report.md"
    path.write_text("\n".join(lines) + "\n")
    return path


async def run_all(args) -> int:
    specs = load_specs()
    wanted = args.sites.split(",") if args.sites else list(specs)
    missing = [s for s in wanted if s not in specs]
    if missing:
        raise SystemExit(f"unknown site(s): {', '.join(missing)} (have: {', '.join(specs)})")
    profile = load_profile()
    problems = [p for s in wanted for p in score.check_spec(specs[s], profile)]
    if problems:
        print("\n".join(problems))
        raise SystemExit("fix the answer keys first (python -m bench check)")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = OUT / f"{stamp}-{args.engine}{'-' + re.sub(r'[^a-z0-9.]+', '-', args.model.lower()) if args.model else ''}{'-only' if args.model_only else ''}"
    run_dir.mkdir(parents=True, exist_ok=True)
    resume = engines.resume_pdf(run_dir / "resume.pdf")
    from agent.jev_client import is_jev, jev_model, make_system_one

    jev = is_jev(args.model)
    if jev and not os.getenv("TYPESAFE_API_KEY"):
        raise SystemExit("TYPESAFE_API_KEY is not set: create one at https://console.typesafe.ai and add it to auto-browse/.env")
    if args.writer:
        preflight_model(args.writer)
    endpoint = None if args.no_model else ("https://api.typesafe.ai" if jev else preflight_model(args.model))
    meta = {"stamp": stamp, "engine": args.engine, "model": None if args.no_model else args.model, "endpoint": endpoint,
            "writer": args.writer, "model_only": args.model_only,
            "submit": not args.no_submit, "sites": wanted, "repeat": args.repeat}
    print(f"apply bench: {args.engine}, model {meta['model'] or 'none'}{f' via {endpoint}' if endpoint else ''}, "
          f"sites {', '.join(wanted)}, x{args.repeat} -> {run_dir}")

    recorder, results = Recorder(), []
    with BackgroundServer(make_app(recorder, specs)) as srv:
        stack = local_agent_browser(run_dir, args.headed) if args.engine == "agent" and not args.mcp else None
        mcp_url = stack.__enter__() if stack else args.mcp
        try:
            for site in wanted:
                spec = specs[site]
                for n in range(1, args.repeat + 1):
                    run_id = f"{stamp}-{site}-{n}"
                    url = f"http://{srv.host}:{srv.port}/r/{run_id}/{site}/job"
                    print(f"[{site} {n}/{args.repeat}] {url}", flush=True)
                    started = time.monotonic()
                    if args.engine == "det":
                        llm_model = args.writer if jev else (None if args.no_model else args.model)
                        complete = engines.CountingComplete(engines.openrouter_complete(llm_model)) if llm_model else None
                        jev_usage: dict = {}
                        jev_fn = engines.CountingJev(make_system_one(jev_model(args.model), usage=jev_usage)) if jev else None
                        eng = await engines.run_det(url, profile=profile, resume_path=str(resume),
                                                    jd_text=spec["job"]["description"], complete=complete, jev=jev_fn,
                                                    model_only=args.model_only, autosubmit=not args.no_submit, headed=args.headed)
                    else:
                        eng = await engines.run_agent(url, profile=profile, resume_path=str(resume), model=args.model,
                                                      mcp_url=mcp_url, autosubmit=not args.no_submit,
                                                      max_iterations=args.max_iterations)
                    await asyncio.sleep(1)  # the page's last state post
                    record = recorder.get(run_id, site)
                    s = score.score(spec, record, profile, today=date.today(), filled_log=eng.get("filled_log"),
                                    all_model=args.engine == "agent")
                    eng["seconds"] = round(time.monotonic() - started, 1)
                    if args.engine == "det" and jev:
                        # Jev bills input tokens only ($0.042 per million for jev-1.13, docs: Models)
                        eng["jev_input_tokens"] = jev_usage.get("input_tokens", 0)
                    # a model that never answered measures nothing: keep the row, flag it
                    eng["valid"] = not eng.get("model_errors")
                    results.append({"site": site, "repeat": n, "url": url, "engine": eng, "score": s, "record": record})
                    if not eng["valid"]:
                        print(f"    INVALID: {len(eng['model_errors'])} model call(s) failed: {eng['model_errors'][0][:120]}", flush=True)
                    print(f"    score {s['score']}%  required {s['required_score']}%  wrong {s['wrong']}  missed {s['missed']}"
                          f"  steps {s['steps_reached']}/{s['steps']}  submitted {'yes' if s['submitted'] else 'no'}"
                          f"  ({eng['status']}: {str(eng.get('reason', ''))[:100]})", flush=True)
                    (run_dir / "results.json").write_text(json.dumps({"meta": meta, "results": results}, indent=1, default=str))
        finally:
            if stack:
                stack.__exit__(None, None, None)
    report = write_report(run_dir, meta, results)
    jev_tokens = sum(r["engine"].get("jev_input_tokens", 0) for r in results)
    if jev_tokens:
        print(f"Jev input tokens {jev_tokens:,} (about ${jev_tokens * 0.042 / 1e6:.4f} at jev-1.13 pricing)")
    valid = [r for r in results if r["engine"]["valid"]]
    if valid:
        avg = sum(r["score"]["score"] for r in valid) / len(valid)
        req = sum(r["score"]["required_score"] for r in valid) / len(valid)
        print(f"\nmean score {avg:.1f}%  mean required {req:.1f}%  wrong answers {sum(r['score']['wrong'] for r in valid)}"
              f"  ({len(valid)}/{len(results)} valid runs)")
    if len(valid) < len(results):
        print(f"{len(results) - len(valid)} run(s) INVALID: model calls failed, those scores measure no model")
    print(f"report: {report}")
    return 0


def cmd_compare(args) -> int:
    from bench import compare

    text = compare.build(args.setups)
    path = OUT / f"compare-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.md"
    path.write_text(text)
    print(text)
    print(f"report: {path}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m bench", description="Score apply engines on mock ATS sites")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="lint the site answer keys against bench/profile.json")
    cp = sub.add_parser("compare", help="side-by-side report of several runs: 'label=output/bench/<run>[+<run>...]'")
    cp.add_argument("setups", nargs="+")
    st = sub.add_parser("selftest", help="oracle fills the answer key through the widgets: must score 100%%")
    st.add_argument("--sites")
    st.add_argument("--headed", action="store_true")
    sv = sub.add_parser("serve", help="serve the mock sites for a look in a browser")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8940)
    r = sub.add_parser("run", help="run an engine on the mock sites and score it")
    r.add_argument("--engine", choices=["det", "agent"], default="det")
    r.add_argument("--sites", help="comma-separated (default: every site in bench/sites)")
    model = r.add_mutually_exclusive_group(required=True)
    model.add_argument("--model", help="OpenRouter model id, e.g. google/gemini-3-flash-preview, or a Jev model "
                                       "(jev, jev-latest; det engine only) — paid")
    model.add_argument("--no-model", action="store_true", help="det only: profile answers, no model calls (free)")
    r.add_argument("--writer", help="with a Jev --model: an OpenRouter model for the written answers Jev cannot give "
                                    "(why us, text areas); without it those stay unanswered")
    r.add_argument("--model-only", action="store_true",
                   help="det: switch off the profile rules; the model answers every field and det only clicks and types")
    r.add_argument("--repeat", type=int, default=1, help="runs per site (model answers vary between runs)")
    r.add_argument("--no-submit", action="store_true", help="stop before the final Submit")
    r.add_argument("--headed", action="store_true", help="show the browser")
    r.add_argument("--mcp", help="agent: use this MCP URL instead of starting a local Playwright MCP + proxy "
                                 "(the browser behind it must reach 127.0.0.1)")
    r.add_argument("--max-iterations", type=int, default=120, help="agent: tool-call turns per site")
    args = p.parse_args()
    if args.cmd == "check":
        sys.exit(cmd_check(args))
    if args.cmd == "compare":
        sys.exit(cmd_compare(args))
    if args.cmd == "selftest":
        sys.exit(cmd_selftest(args))
    if args.cmd == "serve":
        sys.exit(cmd_serve(args))
    if args.engine == "agent" and args.no_model:
        p.error("the agent engine is the model: pass --model")
    if args.engine == "agent" and (args.model or "").lower().startswith("jev"):
        p.error("Jev answers typed questions and writes no tool calls: it can drive the det engine, not the agent")
    if args.writer and not (args.model or "").lower().startswith("jev"):
        p.error("--writer goes with a Jev --model")
    if args.model_only and (args.engine != "det" or args.no_model):
        p.error("--model-only is for the det engine with a --model")
    sys.exit(asyncio.run(run_all(args)))


if __name__ == "__main__":
    main()
