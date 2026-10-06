"""`autoapply apply --engine det` wiring: saved answers, outcome bookkeeping, and
what run_det hands the engine (role profile, container resume path, password).
Browser and engine are faked; Postgres is the jobs_test DB."""

from __future__ import annotations

import asyncio

import pytest

from agent import det_apply
from cli import main as cli
from common import repo
from det_autofill.classify import Row
from det_autofill.engine import PageReport, RunResult


class FakeStorage:
    def __init__(self):
        self.objs = {}

    def put_file(self, key, path):
        self.objs[key] = open(path, "rb").read()

    def get_file(self, key, dest):
        with open(dest, "wb") as f:
            f.write(self.objs[key])

    def get_bytes(self, key):
        return self.objs[key]


class KeepOpen:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        return False


def _user_with_role(db):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, full_name, default_resume_label) VALUES "
            "('det@test.dev', 'Det Test', 'de') RETURNING user_id"
        )
        uid = cur.fetchone()["user_id"]
        cur.execute("INSERT INTO application_profiles (user_id, first_name, city) VALUES (%s, 'Det', 'Omaha')", (uid,))
        cur.execute(
            "INSERT INTO portal_credentials (user_id, login_email, login_password) VALUES (%s, 'det@test.dev', 'hunter2')",
            (uid,),
        )
    db.commit()
    docx = repo.insert_resume_asset(db, uid, "base", f"users/{uid}/resumes/base/de/docx.docx", variant="docx", label="de")
    repo.insert_resume_asset(db, uid, "base", f"users/{uid}/resumes/base/de/pdf.pdf", variant="pdf", label="de")
    repo.set_resume_parsed(db, docx, {"work_experiences": [
        {"job_title": "Data Engineer", "company_name": "Initech", "is_current": True}]})
    return uid, docx


def _row(label, options=()):
    return Row(index=0, selectors=["#q"], widget="select" if options else "text", label=label,
               required=True, options=[{"value": o, "text": o} for o in options])


def test_saved_answers_adapter_scopes_per_job_rows(db):
    uid, _ = _user_with_role(db)
    job_a = repo.add_job_for_user(db, uid, "https://ex.dev/j/det-a")
    job_b = repo.add_job_for_user(db, uid, "https://ex.dev/j/det-b")
    a, b = det_apply.DbSavedAnswers(db, uid, job_a), det_apply.DbSavedAnswers(db, uid, job_b)

    a.put(_row("Preferred office?", ["Austin", "Remote"]), "Remote", per_job=False, source="llm")
    a.put(_row("Why Acme?"), "Because of the platform.", per_job=True, source="llm")

    assert b.get(_row("preferred office"), per_job=False) == {"answer": "Remote", "source": "llm"}  # reusable anywhere
    assert a.get(_row("Why Acme?"), per_job=True)["answer"] == "Because of the platform."
    assert b.get(_row("Why Acme?"), per_job=True) is None  # never replayed on another job
    saved = repo.get_saved_answer(db, uid, "Preferred office?")
    assert saved["options"] == ["Austin", "Remote"] and saved["times_used"] == 1


def test_run_det_hands_the_engine_the_jobs_role_and_container_resume(db, monkeypatch):
    uid, docx = _user_with_role(db)
    job_id = repo.add_job_for_user(db, uid, "https://ex.dev/j/det-run")
    repo.enqueue_tailoring(db, uid, job_id, docx)  # pending: resolve_resume falls back to the de PDF
    storage = FakeStorage()
    storage.objs[f"users/{uid}/resumes/base/de/pdf.pdf"] = b"%PDF de"
    seen = {}

    class FakeBrowser:
        def __init__(self, url):
            seen["mcp"] = url

        async def connect(self):
            return self

        async def navigate(self, url):
            seen["navigated"] = url

        async def close(self):
            seen["closed"] = True

    async def fake_run(driver, store, *, resolver, autosubmit, context, credentials=None, otp=None):
        seen["store"], seen["resolver"], seen["autosubmit"] = store, resolver, autosubmit
        return RunResult(status="ready-to-submit", reason="stopped before 'Submit'")

    monkeypatch.setattr(det_apply, "Browser", FakeBrowser)
    monkeypatch.setattr(det_apply.engine, "run", fake_run)
    monkeypatch.setattr(det_apply, "REPORT_DIR", __import__("pathlib").Path("/tmp"))
    import agent.apply as apply_mod

    async def no_sleep(_):
        return None

    monkeypatch.setattr(apply_mod.asyncio, "sleep", no_sleep)

    result = asyncio.run(det_apply.run_det(
        db, storage, uid, job_id, "https://ex.dev/j/det-run", run_id="r1", model="m",
        answer_question=lambda *a: {"answer": "x"}, mcp_url="http://mcp:1/mcp",
        complete=lambda messages: None,
    ))
    assert result.status == "ready-to-submit" and seen["navigated"] == "https://ex.dev/j/det-run"
    store = seen["store"]
    assert store.get("current_title").value == "Data Engineer"  # the de role's resume
    assert store.p["resume_path"].endswith("/r1/resume.pdf") and store.p["resume_path"].startswith("/")
    assert store.get("password").value == "hunter2"  # login pages may need it...
    assert "hunter2" not in repr(seen["resolver"].profile_doc)  # ...the model never sees it
    assert seen["closed"]


@pytest.mark.parametrize("status,app_status,outcome,ok", [
    ("submitted", "finished", "submitted", True),
    ("unconfirmed", "finished", "unconfirmed", True),
    ("stuck", "failed", "required field(s) unanswered: X", False),
    ("ready-to-submit", "finished", "submitted", True),  # human confirmed in the prompt
])
def test_run_apply_det_records_outcomes(db, monkeypatch, status, app_status, outcome, ok):
    uid, _ = _user_with_role(db)
    job_id = repo.add_job_for_user(db, uid, f"https://ex.dev/j/det-{status}")
    reason = outcome if status == "stuck" else "r"

    async def fake_run_det(*a, **kw):
        return RunResult(status=status, reason=reason)

    monkeypatch.setattr(det_apply, "run_det", fake_run_det)
    monkeypatch.setattr(cli, "_storage", lambda: FakeStorage())
    monkeypatch.setattr(cli, "_confirm_submitted", lambda: "submitted")

    assert cli._run_apply_det(db, uid, job_id, "https://ex.dev/j", "m", False) is ok
    app = repo.list_applications(db, uid, job_id)[0]
    assert (app["status"], app["outcome"]) == (app_status, outcome)


def test_engine_flag_parses():
    args = cli.build_parser().parse_args(["apply", "abcd1234", "--engine", "det", "--mcp", "http://x/mcp"])
    assert (args.engine, args.mcp) == ("det", "http://x/mcp")
    assert cli.build_parser().parse_args(["apply", "abcd1234"]).engine == "agent"
    assert cli.build_parser().parse_args(["continue", "abcd1234", "--engine", "det"]).engine == "det"
    assert cli.build_parser().parse_args(["apply", "abcd1234", "--engine", "det+agent"]).engine == "det+agent"
    assert cli.build_parser().parse_args(["continue", "abcd1234", "--engine", "det+agent"]).engine == "det+agent"
    with pytest.raises(SystemExit, match="only drive --engine det"):
        cli._check_engine_model("det+agent", "jev-latest")  # the agent half needs a chat model


# ---------------------------------------------------------------------------
# --engine det+agent


def _det_result(status, reason, filled=(), unresolved=()):
    page = PageReport(number=1, url="https://ex.dev/apply", unresolved=list(unresolved),
                      filled=[{"label": label, "key": None, "ok": ok, "how": "x", "source": "profile"}
                              for label, ok in filled])
    return RunResult(status=status, reason=reason, pages=[page])


@pytest.mark.parametrize("status,reason,unresolved,takes_over", [
    ("stuck", "required field(s) unanswered: Visa type", ["Visa type"], True),
    ("stuck", "page rejected Next: Employment History is required", [], True),
    ("stuck", "'Apply Now' did not open an application form", [], True),
    ("handoff", "no sign-in button", [], True),  # Rivian: the agent signed in
    ("handoff", "no account on this portal (the check never creates one)", [], True),
    ("handoff", "captcha over the form (hcaptcha)", [], False),
    ("handoff", "human check after Submit: solve it in the browser (noVNC :6080) and confirm", [], False),
    ("handoff", "captcha-or-code: Enter the code we emailed you", [], False),
    ("handoff", "sign-in rejected: incorrect password", [], False),
    ("stuck", "required field(s) unanswered: Are you an LLM, AI agent, or other associated agentic tool?",
     ["Are you an LLM, AI agent, or other associated agentic tool?"], False),  # the applicant's to answer
    ("submitted", "confirmation page seen", [], False),
    ("ready-to-submit", "stopped before 'Submit'", [], False),
])
def test_agent_takes_over_only_what_it_may(status, reason, unresolved, takes_over):
    assert det_apply.agent_should_take_over(_det_result(status, reason, unresolved=unresolved)) is takes_over


def test_handover_lists_what_det_filled_and_what_is_left():
    note = det_apply.agent_handover(_det_result(
        "stuck", "required field(s) unanswered: Visa type",
        filled=[("Email", True), ("Phone", True), ("Discipline", False)], unresolved=["Visa type"]))
    assert "keep these; touch one only if the page marks it invalid: Email; Phone" in note
    assert note.split("Left for you: ")[1].split("\n")[0] == "Visa type; Discipline"
    assert "(stuck: required field(s) unanswered: Visa type)" in note
    assert "never guess" in note and "Captchas are a human handoff" in note


def test_handover_becomes_the_agents_continue_task():
    from agent.loop import apply_task

    task = apply_task("https://ex.dev/j", continue_run=True, handover="HANDOVER FROM THE DETERMINISTIC FILLER: x")
    assert task.startswith("Continue the partially completed application") and task.endswith("FILLER: x")
    assert "HANDOVER" not in apply_task("https://ex.dev/j", continue_run=True)


def test_det_agent_hands_the_open_page_to_the_agent(db, monkeypatch):
    uid, _ = _user_with_role(db)
    job_id = repo.add_job_for_user(db, uid, "https://ex.dev/j/det-agent")
    calls = {}

    async def fake_run_det(*a, **kw):
        calls["push"] = kw["push"]
        return _det_result("stuck", "required field(s) unanswered: Visa type",
                           filled=[("Email", True)], unresolved=["Visa type"])

    def fake_agent(conn, u, j, url, model, autosubmit, continue_run=False, handover=None):
        calls["agent"] = (continue_run, autosubmit, handover)
        return True

    monkeypatch.setattr(det_apply, "run_det", fake_run_det)
    monkeypatch.setattr(cli, "_storage", lambda: FakeStorage())
    monkeypatch.setattr(cli, "_run_apply_agent", fake_agent)

    assert cli._run_apply_det(db, uid, job_id, "https://ex.dev/j", "m", True, agent_fallback=True) is True
    continue_run, autosubmit, handover = calls["agent"]
    assert continue_run and autosubmit and "Left for you: Visa type" in handover
    assert calls["push"] is False  # no "apply paused" alert while the agent carries on
    det_row = repo.list_applications(db, uid, job_id)[0]
    assert det_row["status"] == "failed" and det_row["outcome"].startswith("det stopped, agent took over")


def test_det_agent_leaves_a_captcha_to_the_person(db, monkeypatch):
    uid, _ = _user_with_role(db)
    job_id = repo.add_job_for_user(db, uid, "https://ex.dev/j/det-captcha")
    pushed = []

    async def fake_run_det(*a, **kw):
        return _det_result("handoff", "captcha over the form (hcaptcha)")

    monkeypatch.setattr(det_apply, "run_det", fake_run_det)
    monkeypatch.setattr(det_apply, "push_handoff", lambda *a: pushed.append(a))
    monkeypatch.setattr(cli, "_storage", lambda: FakeStorage())
    monkeypatch.setattr(cli, "_run_apply_agent", lambda *a, **kw: pytest.fail("the agent must not take a captcha"))
    monkeypatch.setattr(cli, "_confirm_submitted", lambda: None)

    assert cli._run_apply_det(db, uid, job_id, "https://ex.dev/j", "m", False, agent_fallback=True) is True
    assert len(pushed) == 1  # the person still gets the alert
