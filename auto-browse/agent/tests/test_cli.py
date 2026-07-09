"""Unit tests for the autoapply CLI core functions (cli/main.py). DB-backed tests
use the throwaway jobs_test DB via the shared `db` fixture; skip when infra is down."""

import subprocess
import uuid

import pytest

from cli import main as cli
from common import repo


def _mk_user(db, email="cli@test.dev", active=True) -> str:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, full_name, is_active) VALUES (%s, 'CLI Test', %s) "
            "RETURNING user_id",
            (email, active),
        )
        uid = cur.fetchone()["user_id"]
    db.commit()
    return uid


# ---------------------------------------------------------------------------
# resolve_user


def test_resolve_user_single_active(db):
    uid = _mk_user(db)
    assert cli.resolve_user(db, None) == uid


def test_resolve_user_none_active(db):
    with pytest.raises(SystemExit, match="no active users"):
        cli.resolve_user(db, None)


def test_resolve_user_multiple_active(db):
    _mk_user(db, "a@test.dev")
    _mk_user(db, "b@test.dev")
    with pytest.raises(SystemExit, match="multiple active users"):
        cli.resolve_user(db, None)


def test_resolve_user_by_email(db):
    uid = _mk_user(db)
    _mk_user(db, "other@test.dev")
    assert cli.resolve_user(db, "cli@test.dev") == uid


def test_resolve_user_by_uuid_passthrough(db):
    uid = str(uuid.uuid4())
    assert cli.resolve_user(db, uid) == uid


def test_resolve_user_unknown_email(db):
    with pytest.raises(SystemExit, match="no user with email"):
        cli.resolve_user(db, "ghost@test.dev")


# ---------------------------------------------------------------------------
# resolve_job


def test_resolve_job_by_uuid(db):
    job_id = repo.get_or_create_job(db, "https://ex.dev/j/1", source="manual")
    assert cli.resolve_job(db, job_id)["job_id"] == job_id


def test_resolve_job_by_prefix(db):
    job_id = repo.get_or_create_job(db, "https://ex.dev/j/1", source="manual")
    assert cli.resolve_job(db, str(job_id)[:8])["job_id"] == job_id


def test_resolve_job_by_url_variants(db):
    job_id = repo.get_or_create_job(db, "https://ex.dev/j/1", source="manual")
    # url_normalize drops www + trailing slash, forces https
    assert cli.resolve_job(db, "http://www.ex.dev/j/1/")["job_id"] == job_id


def test_resolve_job_unknown(db):
    with pytest.raises(SystemExit, match="no job"):
        cli.resolve_job(db, "deadbeef")


def test_resolve_job_ambiguous_prefix(db):
    # Two jobs with ids sharing a 4-char prefix (prefix lookup needs >= 4 chars).
    for i in (1, 2):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (job_id, url, url_normalized, source) "
                "VALUES (%s, %s, %s, 'manual')",
                (
                    f"abcd{i}{i}{i}{i}-0000-4000-8000-00000000000{i}",
                    f"https://ex.dev/amb/{i}",
                    f"https://ex.dev/amb/{i}",
                ),
            )
        db.commit()
    with pytest.raises(SystemExit, match="ambiguous"):
        cli.resolve_job(db, "abcd")


# ---------------------------------------------------------------------------
# jobs add


def test_add_jobs_fresh_and_duplicate(db):
    uid = _mk_user(db)
    r1 = cli.add_jobs(db, uid, ["https://ex.dev/j/1"])[0]
    assert r1["duplicate"] is False
    with db.cursor() as cur:
        cur.execute(
            "SELECT source, status FROM jobs WHERE job_id = %s", (r1["job_id"],)
        )
        row = cur.fetchone()
    assert row["source"] == "manual"
    assert row["status"] == "jd_pending"

    r2 = cli.add_jobs(db, uid, ["http://www.ex.dev/j/1/"])[0]  # normalized dup
    assert r2["duplicate"] is True
    assert r2["job_id"] == r1["job_id"]
    # ownership linked exactly once
    assert repo.job_owner_ids(db, r1["job_id"]) == [uid]


def test_jobs_list_scoped_to_owner(db):
    uid = _mk_user(db)
    other = _mk_user(db, "other@test.dev")
    mine = cli.add_jobs(db, uid, ["https://ex.dev/j/mine"])[0]["job_id"]
    cli.add_jobs(db, other, ["https://ex.dev/j/theirs"])
    rows = cli.list_jobs(db, uid, None)
    assert [r["job_id"] for r in rows] == [mine]


def test_parse_url_lines():
    text = "https://a.dev/1\n\n# comment\n  https://b.dev/2  \n"
    assert cli.parse_url_lines(text) == ["https://a.dev/1", "https://b.dev/2"]


# ---------------------------------------------------------------------------
# tailor: backfill + summary


def _mk_ready_job(db, user_id, url="https://ex.dev/j/1") -> str:
    job_id = repo.add_job_for_user(db, user_id, url, source="manual")
    with db.cursor() as cur:
        cur.execute("UPDATE jobs SET status = 'jd_ready' WHERE job_id = %s", (job_id,))
    db.commit()
    return job_id


def test_backfill_tailoring(db):
    uid = _mk_user(db)
    job_id = _mk_ready_job(db, uid)
    base_id = repo.insert_resume_asset(db, uid, "base", "k/base.docx", variant="docx")

    assert cli.backfill_tailoring(db, uid) == 1
    row = repo.lookup_tailored(db, uid, job_id)
    assert row["status"] == "pending"
    assert row["base_asset_id"] == base_id

    assert cli.backfill_tailoring(db, uid) == 0  # idempotent


def test_backfill_skips_non_ready_jobs(db):
    uid = _mk_user(db)
    repo.add_job_for_user(db, uid, "https://ex.dev/j/1", source="manual")  # jd_pending
    assert cli.backfill_tailoring(db, uid) == 0


def test_backfill_skips_unowned_jobs(db):
    uid = _mk_user(db)
    other = _mk_user(db, "other@test.dev")
    _mk_ready_job(db, other, "https://ex.dev/j/theirs")
    assert cli.backfill_tailoring(db, uid) == 0


def test_tailoring_summary(db):
    uid = _mk_user(db)
    other = _mk_user(db, "other@test.dev")
    j1 = _mk_ready_job(db, uid, "https://ex.dev/j/1")
    j2 = _mk_ready_job(db, uid, "https://ex.dev/j/2")
    t1 = repo.enqueue_tailoring(db, uid, j1)
    repo.enqueue_tailoring(db, uid, j2)
    repo.enqueue_tailoring(db, other, j1)  # must not appear in uid's summary
    with db.cursor() as cur:
        cur.execute(
            "UPDATE tailored_resumes SET status='ready', resume_score=60, "
            "new_resume_score=85 WHERE id = %s",
            (t1,),
        )
    db.commit()

    s = cli.tailoring_summary(db, uid)
    assert s["tailor_counts"] == {"ready": 1, "pending": 1}
    assert len(s["rows"]) == 2
    ready = next(r for r in s["rows"] if r["status"] == "ready")
    assert (ready["resume_score"], ready["new_resume_score"]) == (60, 85)


# ---------------------------------------------------------------------------
# --today filter (user_jobs.added_at)


def _backdate_added(db, user_id, job_id, days=3):
    with db.cursor() as cur:
        cur.execute(
            "UPDATE user_jobs SET added_at = now() - make_interval(days => %s) "
            "WHERE user_id = %s AND job_id = %s",
            (days, user_id, job_id),
        )
    db.commit()


def test_list_jobs_since_filters_by_added_at(db):
    uid = _mk_user(db)
    old = cli.add_jobs(db, uid, ["https://ex.dev/j/old"])[0]["job_id"]
    new = cli.add_jobs(db, uid, ["https://ex.dev/j/new"])[0]["job_id"]
    _backdate_added(db, uid, old)

    rows = cli.list_jobs(db, uid, None, cli._today_start())
    assert [r["job_id"] for r in rows] == [new]
    assert len(cli.list_jobs(db, uid, None)) == 2  # no filter: everything


def test_tailoring_summary_since(db):
    uid = _mk_user(db)
    j_old = _mk_ready_job(db, uid, "https://ex.dev/j/old")
    j_new = _mk_ready_job(db, uid, "https://ex.dev/j/new")
    repo.enqueue_tailoring(db, uid, j_old)
    repo.enqueue_tailoring(db, uid, j_new)
    _backdate_added(db, uid, j_old)

    s = cli.tailoring_summary(db, uid, since=cli._today_start())
    assert s["job_counts"] == {"jd_ready": 1}
    assert s["tailor_counts"] == {"pending": 1}
    assert [r["job_id"] for r in s["rows"]] == [j_new]


# ---------------------------------------------------------------------------
# retry


def _fail_job(db, job_id, error="net timeout"):
    with db.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'failed', error = %s WHERE job_id = %s",
            (error, job_id),
        )
    db.commit()


def test_reset_failed_jobs_today_scope(db):
    uid = _mk_user(db)
    old = cli.add_jobs(db, uid, ["https://ex.dev/j/old"])[0]["job_id"]
    new = cli.add_jobs(db, uid, ["https://ex.dev/j/new"])[0]["job_id"]
    _fail_job(db, old)
    _fail_job(db, new)
    _backdate_added(db, uid, old)

    assert cli.reset_failed_jobs(db, uid, cli._today_start()) == 1
    with db.cursor() as cur:
        cur.execute("SELECT job_id, status, error FROM jobs")
        rows = {r["job_id"]: r for r in cur.fetchall()}
    assert rows[new]["status"] == "jd_pending"
    assert rows[new]["error"] is None
    assert rows[old]["status"] == "failed"  # older than today: untouched

    assert cli.reset_failed_jobs(db, uid, None) == 1  # --all sweeps the rest
    assert cli.count_failed_jobs(db, uid, None) == 0


def test_reset_failed_jobs_scoped_to_owner(db):
    uid = _mk_user(db)
    other = _mk_user(db, "other@test.dev")
    theirs = cli.add_jobs(db, other, ["https://ex.dev/j/theirs"])[0]["job_id"]
    _fail_job(db, theirs)

    assert cli.reset_failed_jobs(db, uid, None) == 0
    assert cli.count_failed_jobs(db, other, None) == 1


# ---------------------------------------------------------------------------
# apply tracking surfaced


def test_apply_marker_mapping():
    assert cli._apply_marker({}) == ""
    assert (
        cli._apply_marker({"apply_status": "finished", "apply_outcome": "submitted"})
        == "applied"
    )
    assert (
        cli._apply_marker({"apply_status": "finished", "apply_outcome": "unconfirmed"})
        == "unconfirmed"
    )
    assert (
        cli._apply_marker({"apply_status": "finished", "apply_outcome": None})
        == "handed-off"
    )
    assert (
        cli._apply_marker({"apply_status": "failed", "apply_outcome": "boom"})
        == "apply-failed"
    )
    assert (
        cli._apply_marker({"apply_status": "running", "apply_outcome": None})
        == "running"
    )


def test_list_jobs_carries_latest_application(db):
    uid = _mk_user(db)
    job_id = cli.add_jobs(db, uid, ["https://ex.dev/j/1"])[0]["job_id"]
    rows = cli.list_jobs(db, uid, None)
    assert rows[0]["apply_status"] is None  # never applied

    a1 = repo.start_application(db, uid, job_id, run_id="run-1")
    repo.finish_application(db, a1, "failed", outcome="boom")
    a2 = repo.start_application(db, uid, job_id, run_id="run-2")
    repo.finish_application(db, a2, "finished", outcome="submitted")

    row = cli.list_jobs(db, uid, None)[0]
    assert (row["apply_status"], row["apply_outcome"]) == ("finished", "submitted")

    apps = repo.list_applications(db, uid, job_id)
    assert [a["run_id"] for a in apps] == ["run-2", "run-1"]  # newest first
    assert apps[0]["url"] == "https://ex.dev/j/1"


def test_confirm_submitted():
    assert cli._confirm_submitted(ask=lambda _: "y") == "submitted"
    assert cli._confirm_submitted(ask=lambda _: "YES ") == "submitted"
    assert cli._confirm_submitted(ask=lambda _: "") == "not submitted"
    assert cli._confirm_submitted(ask=lambda _: "n") == "not submitted"

    def eof(_):
        raise EOFError

    assert cli._confirm_submitted(ask=eof) is None


# ---------------------------------------------------------------------------
# applications helpers (common/repo.py)


def test_application_lifecycle(db):
    uid = _mk_user(db)
    job_id = repo.get_or_create_job(db, "https://ex.dev/j/1", source="manual")
    app_id = repo.start_application(db, uid, job_id, run_id="run-1")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM applications WHERE id = %s", (app_id,))
        row = cur.fetchone()
    assert row["status"] == "running"
    assert row["run_id"] == "run-1"
    assert row["started_at"] is not None
    assert row["finished_at"] is None

    repo.finish_application(db, app_id, "failed", outcome="boom")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM applications WHERE id = %s", (app_id,))
        row = cur.fetchone()
    assert row["status"] == "failed"
    assert row["outcome"] == "boom"
    assert row["finished_at"] is not None


# ---------------------------------------------------------------------------
# tailor worker subprocess wiring


def test_run_tailor_worker_wiring(monkeypatch):
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        calls["kw"] = kw
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli.run_tailor_worker() == 0
    assert calls["cmd"] == ["uv", "run", "python", "worker.py"]
    assert str(calls["kw"]["cwd"]).endswith("ai-assist")
    assert calls["kw"]["env"]["TAILOR_ONCE"] == "1"


# ---------------------------------------------------------------------------
# parser smoke tests (no DB)


@pytest.mark.parametrize(
    "argv",
    [
        ["jobs", "add", "https://ex.dev/j/1"],
        ["jobs", "add", "--file", "urls.txt"],
        ["jobs", "list"],
        ["jobs", "list", "--status", "jd_ready"],
        ["jobs", "list", "--today"],
        ["tailor"],
        ["tailor", "--user", "u@x.dev", "--retry-failed"],
        ["retry"],
        ["retry", "--all", "--user", "u@x.dev"],
        ["status"],
        ["status", "--today"],
        ["status", "abcd1234"],
        ["resume", "show", "abcd1234", "--out", "/tmp/r.pdf", "--open"],
        ["apply", "abcd1234", "https://ex.dev/j/2", "--model", "m"],
        ["apply", "abcd1234", "--autosubmit"],
        ["apply", "--today", "--autosubmit"],
        ["apply", "--all"],
        ["apply", "--all", "--today", "--autosubmit"],
        ["continue", "abcd1234"],
        ["continue", "https://ex.dev/j/2", "--autosubmit", "--model", "m"],
    ],
)
def test_parser_accepts(argv):
    args = cli.build_parser().parse_args(argv)
    assert callable(args.func)


def test_apply_autosubmit_defaults_off():
    args = cli.build_parser().parse_args(["apply", "abcd1234"])
    assert args.autosubmit is False


def test_autosubmit_outcome_parsing():
    f = cli._autosubmit_outcome
    assert f("all done.\nSUBMITTED: yes") == "submitted"
    assert f("no confirmation appeared.\nSUBMITTED: no") == "not submitted"
    assert f("submitted: YES") == "submitted"  # case-insensitive
    # last verdict wins over earlier mentions of the format
    assert f("I must end with SUBMITTED: yes or no.\n...\nSUBMITTED: no") == (
        "not submitted"
    )
    assert f("finished but forgot the verdict line") == "unconfirmed"
    assert f("") == "unconfirmed"
    assert f(None) == "unconfirmed"


def test_build_system_prompt_autosubmit():
    from agent.loop import build_system_prompt

    review = build_system_prompt()
    auto = build_system_prompt(autosubmit=True)
    assert "Do NOT click the final Submit" in review
    assert "Do NOT click the final Submit" not in auto
    assert "click the final Submit" in auto
    assert "SUBMITTED: yes" in auto  # verdict line demanded of autosubmit runs
    assert "SUBMITTED: yes" not in review
    # shared scaffolding intact in both variants
    for prompt in (review, auto):
        assert "APPLICANT PROFILE" in prompt
        assert "fetch_aria" in prompt


def test_system_prompt_spells_out_hcaptcha_accessibility_path():
    # agents kept handing off at the hCaptcha options menu instead of clicking
    # through to the solvable text challenge (iCIMS runs, 2026-06/07)
    from agent.loop import HUMAN_TOOL_NAME, build_system_prompt

    p = build_system_prompt()
    assert "About hCaptcha & Accessibility Options" in p
    assert "Display hCaptcha Accessibility Challenge" in p
    assert "seeing the menu is not the same as switching the challenge" in p
    # handoff is the last resort, conditioned on the control being absent
    assert f"Only call `{HUMAN_TOOL_NAME}` when step 1 or 2 has no such control" in p


def test_build_system_prompt_continue_and_no_navigate():
    from agent.loop import build_system_prompt

    fresh = build_system_prompt()
    cont = build_system_prompt(continue_run=True)
    assert "CONTINUING A PREVIOUS RUN" in cont
    assert "CONTINUING A PREVIOUS RUN" not in fresh
    # navigation is harness-only: the model must never be told it has the tool
    for prompt in (fresh, cont):
        assert "browser_navigate" not in prompt


def test_is_blank_page():
    from agent.loop import _is_blank_page

    assert _is_blank_page("")
    assert _is_blank_page("   \n ")
    assert _is_blank_page("(snapshot unavailable: ConnectionError)")
    assert _is_blank_page("about:blank")
    assert not _is_blank_page("Some Role, Some Company ... Apply")


# ---------------------------------------------------------------------------
# apply --today batch selection


def test_select_ready_jobs_filters(db):
    uid = _mk_user(db)
    ready = _mk_ready_job(db, uid, "https://ex.dev/j/ready")
    _mk_ready_job(db, uid, "https://ex.dev/j/old-ready")
    old = cli.resolve_job(db, "https://ex.dev/j/old-ready")["job_id"]
    _backdate_added(db, uid, old)
    repo.add_job_for_user(db, uid, "https://ex.dev/j/pending", source="manual")
    done = _mk_ready_job(db, uid, "https://ex.dev/j/done")
    app = repo.start_application(db, uid, done, run_id="run-x")
    repo.finish_application(db, app, "finished", outcome="submitted")
    # handed-off (unconfirmed submit) stays in the batch
    half = _mk_ready_job(db, uid, "https://ex.dev/j/half")
    app2 = repo.start_application(db, uid, half, run_id="run-y")
    repo.finish_application(db, app2, "finished", outcome="not submitted")

    picked = cli.select_ready_jobs(db, uid, cli._today_start())
    assert {r["job_id"] for r in picked} == {ready, half}
    # no date filter (--all): backdated ready job included too
    picked_all = cli.select_ready_jobs(db, uid, None)
    assert {r["job_id"] for r in picked_all} == {ready, half, old}


def test_cmd_apply_today_runs_each_ready_job(db, monkeypatch):
    import contextlib
    from types import SimpleNamespace

    uid = _mk_user(db)
    j1 = _mk_ready_job(db, uid, "https://ex.dev/j/1")
    j2 = _mk_ready_job(db, uid, "https://ex.dev/j/2")
    monkeypatch.setattr(cli, "_connect", lambda: contextlib.nullcontext(db))
    ran = []
    monkeypatch.setattr(
        cli,
        "_run_apply_agent",
        lambda conn, u, job_id, url, model, autosub, continue_run=False: (
            ran.append(job_id) or True
        ),
    )
    args = SimpleNamespace(
        jobs=[], all=False, today=True, user=None, model="m", autosubmit=True
    )
    assert cli.cmd_apply(args) == 0
    assert set(ran) == {j1, j2}

    # neither refs nor a batch flag → usage error
    args = SimpleNamespace(
        jobs=[], all=False, today=False, user=None, model="m", autosubmit=False
    )
    with pytest.raises(SystemExit, match="--all / --today"):
        cli.cmd_apply(args)


# ---------------------------------------------------------------------------
# continue command


def test_cmd_continue_guards_and_run(db, monkeypatch):
    import contextlib
    from types import SimpleNamespace

    import agent.loop as loop_mod

    uid = _mk_user(db)
    job_id = repo.add_job_for_user(db, uid, "https://ex.dev/j/1", source="manual")
    monkeypatch.setattr(cli, "_connect", lambda: contextlib.nullcontext(db))
    args = SimpleNamespace(job=str(job_id), user=None, model="m", autosubmit=True)

    with pytest.raises(SystemExit, match="no apply attempt"):
        cli.cmd_continue(args)

    app_id = repo.start_application(db, uid, job_id, run_id="run-1")
    with pytest.raises(SystemExit, match="still marked running"):
        cli.cmd_continue(args)

    repo.finish_application(db, app_id, "finished", outcome="submitted")
    with pytest.raises(SystemExit, match="already submitted"):
        cli.cmd_continue(args)

    # handed-off (finished, submission unconfirmed) → resumes
    app2 = repo.start_application(db, uid, job_id, run_id="run-2")
    repo.finish_application(db, app2, "finished", outcome="not submitted")
    seen = {}

    async def fake_main(
        user_id, url, model, run_id=None, autosubmit=False, continue_run=False
    ):
        seen.update(url=url, autosubmit=autosubmit, continue_run=continue_run)
        return "filled the rest and clicked submit.\nSUBMITTED: yes"

    monkeypatch.setattr(loop_mod, "main", fake_main)
    assert cli.cmd_continue(args) == 0
    assert seen == {
        "url": "https://ex.dev/j/1",
        "autosubmit": True,
        "continue_run": True,
    }
    # a new attempt row was recorded; outcome came from the SUBMITTED verdict
    apps = repo.list_applications(db, uid, job_id)
    assert len(apps) == 3
    assert apps[0]["status"] == "finished"
    assert apps[0]["outcome"] == "submitted"

    # autosubmit run whose final text lacks the verdict → unconfirmed, retryable
    with db.cursor() as cur:  # un-submit so the continue guard lets us in again
        cur.execute(
            "UPDATE applications SET outcome = 'not submitted' WHERE id = %s",
            (apps[0]["id"],),
        )
    db.commit()

    async def vague_main(*a, **kw):
        return "clicked submit, page did something"

    monkeypatch.setattr(loop_mod, "main", vague_main)
    assert cli.cmd_continue(args) == 0
    apps = repo.list_applications(db, uid, job_id)
    assert apps[0]["outcome"] == "unconfirmed"


def test_parser_rejects_bad_status():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["jobs", "list", "--status", "bogus"])
