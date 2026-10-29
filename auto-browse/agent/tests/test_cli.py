"""Unit tests for the autoapply CLI core functions (cli/main.py). DB-backed tests
use the throwaway jobs_test DB via the shared `db` fixture; skip when infra is down."""

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from cli import main as cli
from common import repo


def _mk_user(db, email="cli@test.dev", active=True) -> str:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, full_name, is_active, default_resume_label) "
            "VALUES (%s, 'CLI Test', %s, 'general') RETURNING user_id",
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


def test_add_jobs_stores_posting_url_not_apply_form(db):
    """An apply-form URL has no JD on it; the stored url is what capture fetches."""
    uid = _mk_user(db)
    posting = "https://jobs.ashbyhq.com/ostium/f4b646e5-cec6-4716-af52-ce9fa02ff6e2"
    r = cli.add_jobs(db, uid, [posting + "/application?utm_source=2JnBOk4N8m"])[0]
    assert r["trimmed"] is True
    with db.cursor() as cur:
        cur.execute("SELECT url FROM jobs WHERE job_id = %s", (r["job_id"],))
        assert cur.fetchone()["url"] == posting + "?utm_source=2JnBOk4N8m"
    # and it dedupes against the plain posting URL
    assert cli.add_jobs(db, uid, [posting])[0]["job_id"] == r["job_id"]


def test_parse_url_lines():
    text = "https://a.dev/1\n\n# comment\n  https://b.dev/2  \n"
    assert cli.parse_url_lines(text) == ["https://a.dev/1", "https://b.dev/2"]


# ---------------------------------------------------------------------------
# jobs rm


def test_remove_job_deletes_row_when_sole_owner(db):
    uid = _mk_user(db)
    job_id = cli.add_jobs(db, uid, ["https://ex.dev/j/gone"])[0]["job_id"]
    assert repo.remove_job_for_user(db, uid, job_id) == "deleted"
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM jobs WHERE job_id = %s", (job_id,))
        assert cur.fetchone() is None
    # re-adding the same URL starts a fresh jd_pending job
    again = cli.add_jobs(db, uid, ["https://ex.dev/j/gone"])[0]
    assert again["duplicate"] is False
    assert again["status"] == "jd_pending"


def test_remove_job_keeps_row_for_other_owner(db):
    uid = _mk_user(db)
    other = _mk_user(db, "other@test.dev")
    url = "https://ex.dev/j/shared"
    job_id = cli.add_jobs(db, uid, [url])[0]["job_id"]
    cli.add_jobs(db, other, [url])
    assert repo.remove_job_for_user(db, uid, job_id) == "unlinked"
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM jobs WHERE job_id = %s", (job_id,))
        assert cur.fetchone() is not None
    assert repo.job_owner_ids(db, job_id) == [other]


def test_remove_job_clears_this_users_tailoring_only(db):
    uid = _mk_user(db)
    other = _mk_user(db, "other@test.dev")
    url = "https://ex.dev/j/shared"
    job_id = _mk_ready_job(db, uid, url)
    repo.add_job_for_user(db, other, url, source="manual")
    repo.enqueue_tailoring(db, uid, job_id)
    repo.enqueue_tailoring(db, other, job_id)
    assert repo.remove_job_for_user(db, uid, job_id) == "unlinked"
    with db.cursor() as cur:
        cur.execute(
            "SELECT user_id FROM tailored_resumes WHERE job_id = %s", (job_id,)
        )
        assert [r["user_id"] for r in cur.fetchall()] == [other]


def test_remove_job_not_owned_is_a_noop(db):
    uid = _mk_user(db)
    other = _mk_user(db, "other@test.dev")
    job_id = cli.add_jobs(db, other, ["https://ex.dev/j/theirs"])[0]["job_id"]
    assert repo.remove_job_for_user(db, uid, job_id) == "absent"
    assert repo.job_owner_ids(db, job_id) == [other]


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
    base_id = repo.insert_resume_asset(
        db, uid, "base", "k/base.docx", variant="docx", label="general"
    )

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


def test_status_for_one_past_day(db, monkeypatch, capsys):
    """`--today` was the only scope, so yesterday's batch could not be listed."""
    import contextlib
    from datetime import date, timedelta
    from types import SimpleNamespace

    uid = _mk_user(db)
    j_old = _mk_ready_job(db, uid, "https://ex.dev/j/three-days-ago")
    j_new = _mk_ready_job(db, uid, "https://ex.dev/j/today")
    repo.enqueue_tailoring(db, uid, j_old)
    repo.enqueue_tailoring(db, uid, j_new)
    _backdate_added(db, uid, j_old, days=3)
    day = date.today() - timedelta(days=3)

    s = cli.tailoring_summary(db, uid, cli._day_start(day), cli._day_start(day + timedelta(days=1)))
    assert [r["job_id"] for r in s["rows"]] == [j_old]

    monkeypatch.setattr(cli, "_connect", lambda: contextlib.nullcontext(db))
    args = cli.build_parser().parse_args(["status", "--date", day.isoformat()])
    assert cli.cmd_status(args) == 0
    out = capsys.readouterr().out
    assert "three-days-ago" in out and "j/today" not in out
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["status", "--today", "--date", day.isoformat()])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["status", "--date", "yesterday"])


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
        cli._apply_marker({"apply_status": "finished", "apply_outcome": "skipped"})
        == "skipped"
    )
    assert (
        cli._apply_marker({"apply_status": "finished", "apply_outcome": "already applied"})
        == "applied-before"
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
        ["jobs", "rm", "abcd1234"],
        ["jobs", "rm", "abcd1234", "https://ex.dev/j/2", "--user", "u@x.dev"],
        ["tailor"],
        ["tailor", "--user", "u@x.dev", "--retry-failed"],
        ["retry"],
        ["retry", "--all", "--user", "u@x.dev"],
        ["status"],
        ["status", "--today"],
        ["status", "abcd1234"],
        ["resume", "show", "abcd1234", "--out", "/tmp/r.pdf", "--open"],
        ["ask", "abcd1234", "Why this role?"],
        ["ask", "abcd1234", "-", "--words", "50", "--user", "u@x.dev", "-q"],
        ["tailor", "--label", "swe"],
        ["tailor", "abcd1234", "--label", "de"],
        ["resume", "add", "cv.docx", "--label", "swe"],
        ["resume", "add", "cv.docx", "--label", "swe", "--replace"],
        ["resume", "add", "cv.docx", "--label", "swe", "--default", "--model", "m"],
        ["resume", "parse"],
        ["resume", "parse", "de", "swe"],
        ["resume", "default", "de"],
        ["resume", "bases"],
        ["profile", "show", "--label", "de"],
        ["profile", "export", "--label", "de", "--out", "p.json"],
        ["profile", "init", "--docx", "cv.docx", "--label", "de", "--yes"],
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
    assert f("the portal says you already applied.\nSUBMITTED: already") == "already applied"
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
    assert "SUBMITTED: already" in auto and "read the page again" in auto
    assert "SUBMITTED: yes" not in review
    # shared scaffolding intact in both variants
    for prompt in (review, auto):
        assert "APPLICANT PROFILE" in prompt
        assert "fetch_aria" in prompt
        assert "`SAW: " in prompt  # the note that replaces old page text (_trim_history)


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


def test_jobs_skip_keeps_the_job_out_of_batch_apply(db, monkeypatch, capsys):
    """A job the user decided against (a student co-op) was picked up by every
    `apply --today` again; `jobs skip` records that decision."""
    import contextlib
    from types import SimpleNamespace

    uid = _mk_user(db)
    keep = _mk_ready_job(db, uid, "https://ex.dev/j/keep")
    skip = _mk_ready_job(db, uid, "https://ex.dev/j/skip")
    monkeypatch.setattr(cli, "_connect", lambda: contextlib.nullcontext(db))

    assert cli.cmd_jobs_skip(SimpleNamespace(jobs=[str(skip)], user=None)) == 0
    assert "skipped" in capsys.readouterr().out
    assert [r["job_id"] for r in cli.select_ready_jobs(db, uid, None)] == [keep]
    app = repo.list_applications(db, uid, skip)[0]
    assert (app["status"], app["outcome"]) == ("finished", "skipped")
    # still in the pipeline, unlike `jobs rm`
    assert skip in {r["job_id"] for r in repo.list_jobs_for_user(db, uid, "jd_ready")}


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
        jobs=[], all=False, today=True, user=None, model="m", autosubmit=True,
        engine="agent", mcp=None,
    )
    assert cli.cmd_apply(args) == 0
    assert set(ran) == {j1, j2}

    # neither refs nor a batch flag → usage error
    args = SimpleNamespace(
        jobs=[], all=False, today=False, user=None, model="m", autosubmit=False,
        engine="agent", mcp=None,
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
    args = SimpleNamespace(job=str(job_id), user=None, model="m", autosubmit=True, engine="agent", mcp=None)

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
        user_id, url, model, run_id=None, autosubmit=False, continue_run=False, handover=None
    ):
        seen.update(url=url, autosubmit=autosubmit, continue_run=continue_run, handover=handover)
        return "filled the rest and clicked submit.\nSUBMITTED: yes"

    monkeypatch.setattr(loop_mod, "main", fake_main)
    assert cli.cmd_continue(args) == 0
    assert seen == {
        "url": "https://ex.dev/j/1",
        "autosubmit": True,
        "continue_run": True,
        "handover": None,  # only det+agent briefs the agent
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


# ---------------------------------------------------------------------------
# ask


def test_run_answer_wiring(monkeypatch):
    """Subprocess into ai-assist with the request on stdin, and OPENROUTER_* stripped:
    this package's base-url/key must not leak into ai-assist's own LLM config."""
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        calls["kw"] = kw
        return subprocess.CompletedProcess(
            cmd, 0, stdout='{"answer": "I built it.", "word_count": 3}', stderr=""
        )

    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://pod.runpod.net/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "leaky")
    monkeypatch.setattr(subprocess, "run", fake_run)

    out = cli.run_answer("u-1", "j-1", "Why?", 100)
    assert out == {"answer": "I built it.", "word_count": 3}
    assert calls["cmd"] == ["uv", "run", "python", "answer_cli.py"]
    assert str(calls["kw"]["cwd"]).endswith("ai-assist")
    assert not [k for k in calls["kw"]["env"] if k.startswith("OPENROUTER_")]
    assert json.loads(calls["kw"]["input"]) == {
        "user_id": "u-1", "job_id": "j-1", "question": "Why?", "max_words": 100,
        "label": None,   # null -> ai-assist follows the job's own tailoring base
    }


def test_run_answer_forwards_the_label(monkeypatch):
    """--label has to reach ai-assist: it decides which base resume grounds the
    answer, and the wrong one makes the model deny experience the user has."""
    calls = {}

    def fake_run(cmd, **kw):
        calls["kw"] = kw
        return subprocess.CompletedProcess(
            cmd, 0, stdout='{"answer": "Yes.", "word_count": 1, "base_label": "aiml"}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli.run_answer("u", "j", "RAG?", 100, "aiml")["base_label"] == "aiml"
    assert json.loads(calls["kw"]["input"])["label"] == "aiml"


def test_run_answer_reports_error_when_ai_assist_prints_nothing(monkeypatch):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ModuleNotFoundError: qa")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert "ModuleNotFoundError" in cli.run_answer("u", "j", "Why?", 100)["error"]


def test_run_answer_ignores_log_lines_before_the_json(monkeypatch):
    """ai-assist logs to stdout; only the last line is the payload."""

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            cmd, 0, stdout='INFO:agent:Iteration 1/3\n{"answer": "Yes.", "word_count": 1}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli.run_answer("u", "j", "Why?", 100)["answer"] == "Yes."


def test_ask_requires_jd_ready(db, monkeypatch):
    uid = _mk_user(db)
    job_id = repo.add_job_for_user(db, uid, "https://ex.dev/j/ask-pending")
    monkeypatch.setattr(cli, "_connect", lambda: db)
    monkeypatch.setattr(cli, "run_answer", lambda *a: pytest.fail("must not reach ai-assist"))

    args = cli.build_parser().parse_args(["ask", str(job_id), "Why?"])
    with pytest.raises(SystemExit, match="not jd_ready"):
        cli.cmd_ask(args)


def test_ask_prints_answer_to_stdout(db, monkeypatch, capsys):
    uid = _mk_user(db)
    job_id = repo.add_job_for_user(db, uid, "https://ex.dev/j/ask-ready")
    with db.cursor() as cur:
        cur.execute("UPDATE jobs SET status = 'jd_ready' WHERE job_id = %s", (job_id,))
    db.commit()
    monkeypatch.setattr(cli, "_connect", lambda: db)
    monkeypatch.setattr(
        cli,
        "run_answer",
        lambda *a: {"answer": "I built it.", "word_count": 3, "base_label": "default"},
    )

    args = cli.build_parser().parse_args(["ask", str(job_id), "Why?"])
    assert cli.cmd_ask(args) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "I built it."     # answer alone on stdout, pipeable
    assert "3/100 words" in out.err
    assert "base: default" in out.err           # never silent about which resume


def test_ask_passes_label_through_and_reports_the_base(db, monkeypatch, capsys):
    uid = _mk_user(db)
    job_id = repo.add_job_for_user(db, uid, "https://ex.dev/j/ask-label")
    with db.cursor() as cur:
        cur.execute("UPDATE jobs SET status = 'jd_ready' WHERE job_id = %s", (job_id,))
    db.commit()
    monkeypatch.setattr(cli, "_connect", lambda: db)
    seen = {}

    def fake_answer(user_id, jid, question, words, label=None):
        seen["label"] = label
        return {"answer": "Shipped RAG.", "word_count": 2, "base_label": "aiml"}

    monkeypatch.setattr(cli, "run_answer", fake_answer)

    args = cli.build_parser().parse_args(
        ["ask", str(job_id), "RAG?", "--label", "aiml"]
    )
    assert cli.cmd_ask(args) == 0
    assert seen["label"] == "aiml"
    assert "base: aiml" in capsys.readouterr().err


def test_ask_returns_1_on_ai_assist_error(db, monkeypatch, capsys):
    uid = _mk_user(db)
    job_id = repo.add_job_for_user(db, uid, "https://ex.dev/j/ask-err")
    with db.cursor() as cur:
        cur.execute("UPDATE jobs SET status = 'jd_ready' WHERE job_id = %s", (job_id,))
    db.commit()
    monkeypatch.setattr(cli, "_connect", lambda: db)
    monkeypatch.setattr(cli, "run_answer", lambda *a: {"error": "no captured JD"})

    args = cli.build_parser().parse_args(["ask", str(job_id), "Why?"])
    assert cli.cmd_ask(args) == 1
    assert "no captured JD" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cover


def _jd_ready_job(db, uid, url):
    job_id = repo.add_job_for_user(db, uid, url)
    with db.cursor() as cur:
        cur.execute("UPDATE jobs SET status = 'jd_ready' WHERE job_id = %s", (job_id,))
    db.commit()
    return job_id


def test_cover_requires_jd_ready(db, monkeypatch):
    uid = _mk_user(db)
    job_id = repo.add_job_for_user(db, uid, "https://ex.dev/j/cover-pending")
    monkeypatch.setattr(cli, "_connect", lambda: db)
    monkeypatch.setattr(cli, "run_cover", lambda *a: pytest.fail("must not reach ai-assist"))

    args = cli.build_parser().parse_args(["cover", str(job_id)])
    with pytest.raises(SystemExit, match="not jd_ready"):
        cli.cmd_cover(args)


def test_cover_prints_the_letter_and_downloads_with_out(db, monkeypatch, capsys, tmp_path):
    uid = _mk_user(db)
    job_id = _jd_ready_job(db, uid, "https://ex.dev/j/cover-ready")
    monkeypatch.setattr(cli, "_connect", lambda: db)
    seen = {}

    def fake_cover(user_id, jid, words, label=None):
        seen.update(words=words, label=label)
        return {"text": "Dear Hiring Manager,\n\nI built it.", "word_count": 3,
                "base_label": "swe", "pdf_key": "k.pdf", "txt_key": "k.txt"}

    class Store:
        def get_file(self, key, dest):
            seen["key"] = key
            Path(dest).write_bytes(b"%PDF")

    monkeypatch.setattr(cli, "run_cover", fake_cover)
    monkeypatch.setattr(cli, "_storage", lambda: Store())
    out_pdf = tmp_path / "c.pdf"

    args = cli.build_parser().parse_args(
        ["cover", str(job_id), "--label", "swe", "--words", "200", "--out", str(out_pdf)]
    )
    assert cli.cmd_cover(args) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "Dear Hiring Manager,\n\nI built it."  # letter alone on stdout
    assert "base: swe" in out.err and str(out_pdf) in out.err
    assert seen == {"words": 200, "label": "swe", "key": "k.pdf"}
    assert out_pdf.read_bytes() == b"%PDF"


def test_cover_returns_1_on_ai_assist_error(db, monkeypatch, capsys):
    uid = _mk_user(db)
    job_id = _jd_ready_job(db, uid, "https://ex.dev/j/cover-err")
    monkeypatch.setattr(cli, "_connect", lambda: db)
    monkeypatch.setattr(cli, "run_cover", lambda *a: {"error": "no captured JD"})

    args = cli.build_parser().parse_args(["cover", str(job_id)])
    assert cli.cmd_cover(args) == 1
    assert "no captured JD" in capsys.readouterr().err


def test_run_cover_reads_the_last_stdout_line(monkeypatch):
    def fake_run(cmd, **kw):
        assert cmd[-1] == "cover_cli.py" and "OPENROUTER_API_KEY" not in kw["env"]
        return subprocess.CompletedProcess(
            cmd, 0, stdout='INFO:agent:Iteration 1/3\n{"text": "Hi.", "word_count": 1}',
            stderr="",
        )

    monkeypatch.setenv("OPENROUTER_API_KEY", "leak-me-not")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli.run_cover("u", "j", 350)["text"] == "Hi."


# ---------------------------------------------------------------------------
# role-labelled base resumes


def _mk_base(db, uid, label, variant="docx"):
    """A role base; 'general' is the default role every _mk_user user starts with."""
    key = f"users/{uid}/resumes/base/{label}/{variant}.{variant}"
    return repo.insert_resume_asset(db, uid, "base", key, variant=variant, label=label)


def test_backfill_uses_labelled_base(db):
    uid = _mk_user(db)
    job_id = _mk_ready_job(db, uid)
    _mk_base(db, uid, "general")             # default role
    swe = _mk_base(db, uid, "swe")

    assert cli.backfill_tailoring(db, uid, label="swe") == 1
    assert repo.lookup_tailored(db, uid, job_id)["base_asset_id"] == swe


def test_backfill_default_ignores_role_base(db):
    uid = _mk_user(db)
    job_id = _mk_ready_job(db, uid)
    default = _mk_base(db, uid, "general")
    _mk_base(db, uid, "swe")

    assert cli.backfill_tailoring(db, uid) == 1   # no label
    assert repo.lookup_tailored(db, uid, job_id)["base_asset_id"] == default


def test_backfill_missing_label_errors(db):
    uid = _mk_user(db)
    _mk_ready_job(db, uid)
    _mk_base(db, uid, "general")
    with pytest.raises(SystemExit, match="no resume labelled 'swe'"):
        cli.backfill_tailoring(db, uid, label="swe")


def test_reenqueue_one_resets_and_swaps_base(db):
    uid = _mk_user(db)
    job_id = _mk_ready_job(db, uid)
    default = _mk_base(db, uid, "general")
    de = _mk_base(db, uid, "de")

    cli.backfill_tailoring(db, uid)  # enqueue from default
    assert repo.lookup_tailored(db, uid, job_id)["base_asset_id"] == default

    # pretend the first tailoring finished
    with db.cursor() as cur:
        cur.execute(
            "UPDATE tailored_resumes SET status = 'ready' WHERE user_id = %s AND job_id = %s",
            (uid, job_id),
        )
    db.commit()

    cli.reenqueue_one(db, uid, job_id, "de")
    row = repo.lookup_tailored(db, uid, job_id)
    assert row["base_asset_id"] == de      # base swapped
    assert row["status"] == "pending"      # reset for the worker


class _RecordingStorage:
    def __init__(self):
        self.objs = {}

    def put_file(self, key, path):
        self.objs[key] = Path(path).read_bytes()

    def get_file(self, key, dest):
        Path(dest).write_bytes(self.objs[key])


class _KeepOpen:
    """cmd_* close the connection their `with _connect()` opened; tests keep theirs."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        return False


def _stub_resume_pipeline(monkeypatch, tmp_path, parsed_by_file=None, calls=None):
    """No LibreOffice and no paid parse in tests: the PDF is a copy and the parse
    returns whatever `parsed_by_file` maps the file's stem to."""
    from cli import onboard

    calls = calls if calls is not None else []

    def fake_parse(text, model=None, client=None):
        calls.append(text)
        return onboard.ParsedResume.model_validate((parsed_by_file or {}).get(text, {}))

    def fake_pdf(docx_path):
        out = tmp_path / (Path(docx_path).stem + ".pdf")
        out.write_bytes(b"%PDF " + Path(docx_path).read_bytes())
        return str(out)

    monkeypatch.setattr(onboard, "extract_text", lambda path: Path(path).stem)
    monkeypatch.setattr(onboard, "parse_resume_llm", fake_parse)
    monkeypatch.setattr(onboard, "docx_to_pdf", fake_pdf)
    return calls


def _docx(tmp_path, name):
    path = tmp_path / f"{name}.docx"
    path.write_bytes(b"PK\x03\x04 " + name.encode())
    return path


_SWE = {
    "summary": "Builds services.",
    "work_experiences": [
        {"job_title": "Software Engineer", "company_name": "Acme Inc",
         "from_date": "2025-01-01", "is_current": True},
        {"job_title": "Engineer", "company_name": "Globex", "from_date": "2020-04-01",
         "to_date": "2022-04-01"},
    ],
}


def test_resume_add_stores_docx_pdf_and_parsed_profile(db, monkeypatch, tmp_path, capsys):
    """A role resume becomes that role's profile: docx for tailoring, a PDF for the
    upload, and the parsed content. The first role added is the default."""
    uid = _mk_user(db)
    with db.cursor() as cur:  # a brand-new user has no default role yet
        cur.execute("UPDATE users SET default_resume_label = NULL WHERE user_id = %s", (uid,))
    db.commit()
    store = _RecordingStorage()
    _stub_resume_pipeline(monkeypatch, tmp_path, {"swe": _SWE})
    monkeypatch.setattr(cli, "_connect", lambda: _KeepOpen(db))
    monkeypatch.setattr(cli, "_storage", lambda: store)

    args = cli.build_parser().parse_args(["resume", "add", str(_docx(tmp_path, "swe")), "--label", "swe"])
    assert cli.cmd_resume_add(args) == 0

    assert set(store.objs) == {
        f"users/{uid}/resumes/base/swe/docx.docx",
        f"users/{uid}/resumes/base/swe/pdf.pdf",
    }
    assert repo.default_resume_label(db, uid) == "swe"
    p = repo.fetch_profile(db, uid)
    assert p.label == "swe" and p.summary == "Builds services."
    assert p.work_experiences[0]["company_name"] == "Acme Inc"
    assert "role 'swe' (default)" in capsys.readouterr().out


def test_resume_add_reports_employer_dates_that_disagree(db, monkeypatch, tmp_path, capsys):
    """The conflict that started this: role resumes disagreeing on an end date and on
    who the current employer is. Reported, never silently merged."""
    uid = _mk_user(db)
    de = {
        "work_experiences": [
            {"job_title": "Data Engineer", "company_name": "Initech", "from_date": "2026-05-01",
             "is_current": True},
            {"job_title": "Data Engineer", "company_name": "ACME", "from_date": "2025-01-01",
             "to_date": "2026-04-01"},
            {"job_title": "Data Engineer", "company_name": "Globex", "from_date": "2020-04-01",
             "to_date": "2022-12-01"},
        ]
    }
    store = _RecordingStorage()
    _stub_resume_pipeline(monkeypatch, tmp_path, {"swe": _SWE, "de": de})
    monkeypatch.setattr(cli, "_connect", lambda: _KeepOpen(db))
    monkeypatch.setattr(cli, "_storage", lambda: store)
    parser = cli.build_parser()

    cli.cmd_resume_add(parser.parse_args(["resume", "add", str(_docx(tmp_path, "swe")), "--label", "swe"]))
    capsys.readouterr()
    cli.cmd_resume_add(parser.parse_args(["resume", "add", str(_docx(tmp_path, "de")), "--label", "de"]))
    out = capsys.readouterr().out

    assert "Acme Inc: de says 2025-01 -> 2026-04, swe says 2025-01 -> present" in out
    assert "Globex: de says 2020-04 -> 2022-12, swe says 2020-04 -> 2022-04" in out
    assert "current employer: de says Initech, swe says Acme Inc" in out
    # both roles keep their own version
    assert repo.fetch_profile(db, uid, label="swe").work_experiences[1]["to_date"] == "2022-04-01"
    assert repo.fetch_profile(db, uid, label="de").work_experiences[2]["to_date"] == "2022-12-01"


def test_role_conflicts_ignore_titles_and_omitted_jobs(db):
    """Retitling a job per role is a choice, and a role may leave an old job out."""
    from cli import onboard

    uid = _mk_user(db)
    swe = _mk_base(db, uid, "swe")
    repo.set_resume_parsed(db, swe, _SWE)
    de = {"work_experiences": [
        {"job_title": "Data Engineer", "company_name": "ACME", "from_date": "2025-01-01",
         "is_current": True},
    ]}
    assert onboard.role_conflicts(db, uid, "de", de) == []


def test_resume_add_rejects_duplicate_without_replace(db, monkeypatch, tmp_path):
    uid = _mk_user(db)
    _mk_base(db, uid, "swe")
    calls = _stub_resume_pipeline(monkeypatch, tmp_path)

    class NoUpload:
        def put_file(self, key, path):
            pytest.fail("must not upload")

    monkeypatch.setattr(cli, "_connect", lambda: _KeepOpen(db))
    monkeypatch.setattr(cli, "_storage", lambda: NoUpload())

    args = cli.build_parser().parse_args(["resume", "add", str(_docx(tmp_path, "cv")), "--label", "swe"])
    with pytest.raises(SystemExit, match="already exists"):
        cli.cmd_resume_add(args)
    assert calls == []  # refused before the paid parse


def test_resume_add_rejects_non_docx(db, monkeypatch, tmp_path):
    _mk_user(db)
    _stub_resume_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_connect", lambda: _KeepOpen(db))
    monkeypatch.setattr(cli, "_storage", lambda: _RecordingStorage())
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF")
    args = cli.build_parser().parse_args(["resume", "add", str(pdf), "--label", "swe"])
    with pytest.raises(SystemExit, match="a PDF can't be tailored"):
        cli.cmd_resume_add(args)


def test_resume_add_converts_markdown_and_keeps_only_the_docx(db, monkeypatch, tmp_path):
    """.md/.odt go through LibreOffice to a .docx; that .docx is what is parsed,
    stored and later tailored, and the source file is not uploaded."""
    from cli import onboard

    uid = _mk_user(db)
    store = _RecordingStorage()
    _stub_resume_pipeline(monkeypatch, tmp_path, {"cv": _SWE})
    converted = []

    def fake_convert(src, fmt):
        out = tmp_path / "converted" / f"{Path(src).stem}.{fmt}"
        out.parent.mkdir(exist_ok=True)
        out.write_bytes(b"PK\x03\x04 converted")
        converted.append((Path(src).name, fmt))
        return str(out)

    monkeypatch.setattr(onboard, "_libreoffice_convert", fake_convert)
    monkeypatch.setattr(cli, "_connect", lambda: _KeepOpen(db))
    monkeypatch.setattr(cli, "_storage", lambda: store)
    md = tmp_path / "cv.md"
    md.write_text("# Jane Doe\n\n- Built pipelines\n")

    args = cli.build_parser().parse_args(["resume", "add", str(md), "--label", "swe"])
    assert cli.cmd_resume_add(args) == 0

    assert converted == [("cv.md", "docx")]
    assert store.objs[f"users/{uid}/resumes/base/swe/docx.docx"] == b"PK\x03\x04 converted"
    assert set(store.objs) == {
        f"users/{uid}/resumes/base/swe/docx.docx",
        f"users/{uid}/resumes/base/swe/pdf.pdf",
    }
    assert repo.fetch_profile(db, uid, "swe").summary == "Builds services."


def test_profile_init_refuses_a_pdf_before_asking_anything(monkeypatch, tmp_path):
    """A PDF used to be parsed, the facts saved, and only then refused as a role
    resume — a half-finished onboarding. It now fails before the first prompt."""
    from cli import onboard

    monkeypatch.setattr(onboard, "parse_resume_llm", lambda *a, **k: pytest.fail("paid parse"))
    monkeypatch.setattr(onboard, "interactive_review", lambda *a, **k: pytest.fail("prompted"))
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF")
    args = cli.build_parser().parse_args(["profile", "init", "--docx", str(pdf)])
    with pytest.raises(SystemExit, match="a PDF can't be tailored"):
        onboard.cmd_profile_init(args)


@pytest.mark.parametrize("label", ["default", "Data Eng", "../x", ""])
def test_resume_add_rejects_bad_labels(db, monkeypatch, tmp_path, label):
    """'default' means "your default role" in `ask --label`; labels also become
    storage folders, so no spaces, slashes or capitals."""
    _mk_user(db)
    _stub_resume_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_connect", lambda: _KeepOpen(db))
    monkeypatch.setattr(cli, "_storage", lambda: _RecordingStorage())
    args = cli.build_parser().parse_args(["resume", "add", str(_docx(tmp_path, "cv")), "--label", label])
    with pytest.raises(SystemExit, match="reserved|bad label"):
        cli.cmd_resume_add(args)


def test_resume_add_replace_reuses_the_rows(db, monkeypatch, tmp_path):
    """--replace overwrites the objects in place: one docx row and one pdf row per
    role, with stable ids, so earlier tailorings' base_asset_id stays valid."""
    uid = _mk_user(db)
    store = _RecordingStorage()
    _stub_resume_pipeline(monkeypatch, tmp_path, {"cv": _SWE})
    monkeypatch.setattr(cli, "_connect", lambda: _KeepOpen(db))
    monkeypatch.setattr(cli, "_storage", lambda: store)
    parser = cli.build_parser()
    docx = str(_docx(tmp_path, "cv"))

    cli.cmd_resume_add(parser.parse_args(["resume", "add", docx, "--label", "swe"]))
    first = {b["variant"]: b["id"] for b in repo.list_base_assets(db, uid)}
    for _ in range(2):
        cli.cmd_resume_add(parser.parse_args(["resume", "add", docx, "--label", "swe", "--replace"]))
    assert {b["variant"]: b["id"] for b in repo.list_base_assets(db, uid)} == first


def test_resume_parse_reparses_stored_resumes(db, monkeypatch, tmp_path):
    """Bases uploaded before parsing existed get a profile (and a PDF) without the
    user uploading them again."""
    uid = _mk_user(db)
    store = _RecordingStorage()
    key = f"users/{uid}/resumes/base/de/docx.docx"
    store.objs[key] = b"PK\x03\x04 de"
    repo.insert_resume_asset(db, uid, "base", key, variant="docx", label="de")
    # the stored copy is downloaded as <label>.docx, which the stub parses by stem
    _stub_resume_pipeline(monkeypatch, tmp_path, {"de": _SWE})
    monkeypatch.setattr(cli, "_connect", lambda: _KeepOpen(db))
    monkeypatch.setattr(cli, "_storage", lambda: store)

    assert cli.cmd_resume_parse(cli.build_parser().parse_args(["resume", "parse"])) == 0
    assert repo.fetch_profile(db, uid, label="de").summary == "Builds services."
    assert f"users/{uid}/resumes/base/de/pdf.pdf" in store.objs


def test_resume_default_switches_the_default_role(db, monkeypatch, capsys):
    uid = _mk_user(db)
    _mk_base(db, uid, "de")
    monkeypatch.setattr(cli, "_connect", lambda: _KeepOpen(db))
    assert cli.cmd_resume_default(cli.build_parser().parse_args(["resume", "default", "de"])) == 0
    assert repo.default_resume_label(db, uid) == "de"
    with pytest.raises(SystemExit, match="no resume labelled 'swe'"):
        cli.cmd_resume_default(cli.build_parser().parse_args(["resume", "default", "swe"]))


def test_cmd_resume_bases_marks_default_and_parse_state(db, monkeypatch, capsys):
    uid = _mk_user(db)
    general = _mk_base(db, uid, "general")
    repo.set_resume_parsed(db, general, {"summary": "x"})
    _mk_base(db, uid, "swe")
    monkeypatch.setattr(cli, "_connect", lambda: _KeepOpen(db))

    assert cli.cmd_resume_bases(cli.build_parser().parse_args(["resume", "bases"])) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(l.startswith("* general") and "parsed" in l for l in lines)
    assert any(l.startswith("  swe") and "NOT PARSED" in l for l in lines)


def test_profile_export_is_one_role_without_the_password(db, monkeypatch, tmp_path):
    uid = _mk_user(db)
    de = _mk_base(db, uid, "de")
    repo.set_resume_parsed(db, de, _SWE)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO application_profiles (user_id, city) VALUES (%s, 'Omaha')", (uid,)
        )
        cur.execute(
            "INSERT INTO portal_credentials (user_id, login_email, login_password) "
            "VALUES (%s, 'cli@test.dev', 'hunter2')",
            (uid,),
        )
    db.commit()
    monkeypatch.setattr(cli, "_connect", lambda: _KeepOpen(db))
    out = tmp_path / "profile.json"

    args = cli.build_parser().parse_args(["profile", "export", "--label", "de", "--out", str(out)])
    assert cli._cmd_profile_export(args) == 0
    doc = json.loads(out.read_text())
    assert doc["role"] == "de" and doc["facts"]["city"] == "Omaha"
    assert doc["work_experiences"][0]["company_name"] == "Acme Inc"
    assert doc["portal_login"] == {"email": "cli@test.dev", "password_set": True}
    assert "hunter2" not in out.read_text()


def test_jobs_add_missing_file_is_a_clean_error(db, monkeypatch, tmp_path):
    """A typo'd --file path used to surface as a raw FileNotFoundError traceback;
    every other bad-input path on this command exits with a one-line message."""
    _mk_user(db)
    monkeypatch.setattr(cli, "_connect", lambda: db)
    args = cli.build_parser().parse_args(
        ["jobs", "add", "--file", str(tmp_path / "nope.txt")]
    )
    with pytest.raises(SystemExit, match="no such file"):
        cli.cmd_jobs_add(args)


# ---------------------------------------------------------------------------
# duplicate detection at tailor time


def _finish_tailoring(db, uid, job_id):
    """Mark a job's tailoring ready, as the worker would."""
    with db.cursor() as cur:
        cur.execute(
            "UPDATE tailored_resumes SET status = 'ready' "
            "WHERE user_id = %s AND job_id = %s",
            (uid, job_id),
        )
    db.commit()


def test_already_tailored_finds_duplicates(db):
    uid = _mk_user(db)
    done = _mk_ready_job(db, uid, "https://ex.dev/j/done")
    fresh = _mk_ready_job(db, uid, "https://ex.dev/j/fresh")
    _mk_base(db, uid, "general")

    cli.backfill_tailoring(db, uid)
    assert cli.already_tailored(db, uid) == []  # both still pending

    _finish_tailoring(db, uid, done)
    dupes = cli.already_tailored(db, uid)
    assert [str(r["job_id"]) for r in dupes] == [str(done)]
    assert str(fresh) not in {str(r["job_id"]) for r in dupes}


def test_already_tailored_is_per_user(db):
    uid = _mk_user(db)
    other = _mk_user(db, "other@test.dev")
    job_id = _mk_ready_job(db, other, "https://ex.dev/j/theirs")
    _mk_base(db, other, "general")
    cli.backfill_tailoring(db, other)
    _finish_tailoring(db, other, job_id)

    assert cli.already_tailored(db, uid) == []          # not mine
    assert len(cli.already_tailored(db, other)) == 1


def test_print_duplicates_caps_the_list(capsys):
    from datetime import datetime, timezone

    rows = [
        {
            "job_id": uuid.uuid4(),
            "url": f"https://ex.dev/j/{i}",
            "company": f"co{i}",
            "updated_at": datetime(2026, 7, 24, 12, tzinfo=timezone.utc),
        }
        for i in range(13)
    ]
    cli.print_duplicates(rows, limit=10)
    out = capsys.readouterr().out
    assert "13 duplicate(s)" in out
    assert "... +3 more" in out
    assert "co0" in out and "co12" not in out


def test_print_duplicates_silent_when_none(capsys):
    cli.print_duplicates([])
    assert capsys.readouterr().out == ""


def test_requeue_duplicates_resets_to_pending(db):
    uid = _mk_user(db)
    job_id = _mk_ready_job(db, uid)
    default = _mk_base(db, uid, "general")
    swe = _mk_base(db, uid, "swe")

    cli.backfill_tailoring(db, uid)
    _finish_tailoring(db, uid, job_id)
    dupes = cli.already_tailored(db, uid)

    assert cli.requeue_duplicates(db, uid, dupes, "swe") == 1
    row = repo.lookup_tailored(db, uid, job_id)
    assert row["status"] == "pending"
    assert row["base_asset_id"] == swe          # --force honours --label
    assert row["base_asset_id"] != default


def test_tailor_force_flag_parses():
    args = cli.build_parser().parse_args(["tailor", "--force", "--label", "de"])
    assert args.force is True and args.label == "de"
    assert cli.build_parser().parse_args(["tailor"]).force is False


# ---------------------------------------------------------------------------
# --retry-failed scoping (by label, and by when the row failed)


def _fail_tailoring(db, uid, job_id, base_asset_id, days_ago=0):
    """A failed tailoring row that failed `days_ago` days back."""
    repo.enqueue_tailoring(db, uid, job_id, base_asset_id)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE tailored_resumes SET status = 'failed', error = 'boom', "
            "updated_at = now() - (%s || ' days')::interval "
            "WHERE user_id = %s AND job_id = %s",
            (days_ago, uid, job_id),
        )
    db.commit()


def _statuses(db, uid):
    with db.cursor() as cur:
        cur.execute(
            "SELECT j.url, t.status FROM tailored_resumes t JOIN jobs j USING (job_id) "
            "WHERE t.user_id = %s",
            (uid,),
        )
        return {r["url"].rsplit("/", 1)[-1]: r["status"] for r in cur.fetchall()}


def test_parse_since_defaults_to_today(monkeypatch):
    assert cli.parse_since(None) == cli._today_start()
    assert cli.parse_since("today") == cli._today_start()


def test_parse_since_all_removes_the_cutoff():
    assert cli.parse_since("all") is None


def test_parse_since_accepts_day_counts_and_dates():
    from datetime import timedelta

    assert cli.parse_since("7d") == cli._today_start() - timedelta(days=7)
    assert cli.parse_since("2026-08-29").date().isoformat() == "2026-08-29"


def test_parse_since_rejects_garbage():
    with pytest.raises(SystemExit, match="retry-since"):
        cli.parse_since("last tuesday")


def test_retry_failed_only_touches_the_labelled_base(db):
    """`--label swe --retry-failed` must not drag a failed `de` row into the swe
    batch — repoint_pending would then move it onto the swe resume."""
    uid = _mk_user(db)
    swe, de = _mk_base(db, uid, "swe"), _mk_base(db, uid, "de")
    swe_job = repo.add_job_for_user(db, uid, "https://ex.dev/j/retry-swe")
    de_job = repo.add_job_for_user(db, uid, "https://ex.dev/j/retry-de")
    _fail_tailoring(db, uid, swe_job, swe)
    _fail_tailoring(db, uid, de_job, de)

    assert cli.reset_failed_tailorings(db, uid, cli._today_start(), "swe") == 1
    assert _statuses(db, uid) == {"retry-swe": "pending", "retry-de": "failed"}


def test_retry_failed_without_a_label_takes_every_base(db):
    uid = _mk_user(db)
    swe, de = _mk_base(db, uid, "swe"), _mk_base(db, uid, "de")
    a = repo.add_job_for_user(db, uid, "https://ex.dev/j/retry-any-1")
    b = repo.add_job_for_user(db, uid, "https://ex.dev/j/retry-any-2")
    _fail_tailoring(db, uid, a, swe)
    _fail_tailoring(db, uid, b, de)

    assert cli.reset_failed_tailorings(db, uid, cli._today_start(), None) == 2


def test_retry_failed_leaves_older_failures_alone(db):
    """Failures from previous days were left unretried on purpose (or are duds);
    each retry is a paid model call, so the default window is today."""
    uid = _mk_user(db)
    swe = _mk_base(db, uid, "swe")
    today = repo.add_job_for_user(db, uid, "https://ex.dev/j/retry-today")
    old = repo.add_job_for_user(db, uid, "https://ex.dev/j/retry-old")
    _fail_tailoring(db, uid, today, swe)
    _fail_tailoring(db, uid, old, swe, days_ago=3)

    assert cli.reset_failed_tailorings(db, uid, cli._today_start(), "swe") == 1
    assert _statuses(db, uid) == {"retry-today": "pending", "retry-old": "failed"}
    assert cli.count_older_failures(db, uid, cli._today_start(), "swe") == 1


def test_retry_since_all_includes_old_failures(db):
    uid = _mk_user(db)
    swe = _mk_base(db, uid, "swe")
    old = repo.add_job_for_user(db, uid, "https://ex.dev/j/retry-old-all")
    _fail_tailoring(db, uid, old, swe, days_ago=30)

    assert cli.reset_failed_tailorings(db, uid, cli.parse_since("all"), "swe") == 1


def test_retry_failed_ignores_rows_with_no_base_when_labelled(db):
    """A row with no base belongs to no role — a labelled retry must skip it rather
    than adopt it into that role's batch."""
    uid = _mk_user(db)
    _mk_base(db, uid, "swe")
    job = repo.add_job_for_user(db, uid, "https://ex.dev/j/retry-nobase")
    _fail_tailoring(db, uid, job, None)

    assert cli.reset_failed_tailorings(db, uid, cli._today_start(), "swe") == 0
    assert cli.reset_failed_tailorings(db, uid, cli._today_start(), None) == 1


def test_retry_failed_is_per_user(db):
    uid, other = _mk_user(db), _mk_user(db, email="other-retry@e.co")
    swe = _mk_base(db, other, "swe")
    job = repo.add_job_for_user(db, other, "https://ex.dev/j/retry-other")
    _fail_tailoring(db, other, job, swe)

    assert cli.reset_failed_tailorings(db, uid, cli._today_start(), "swe") == 0


def test_retry_since_flag_parses():
    args = cli.build_parser().parse_args(
        ["tailor", "--label", "swe", "--retry-failed", "--retry-since", "7d"]
    )
    assert args.retry_failed and args.retry_since == "7d"


# ---------------------------------------------------------------------------
# CLI sweep regressions (2026-09-01)


def test_resolve_job_short_prefix_says_it_is_too_short(db):
    """A 2-char prefix used to report "no job matching 'ab'", which reads like the
    job is gone rather than like the 4-char floor rejected the argument."""
    uid = _mk_user(db)
    cli.add_jobs(db, uid, ["https://ex.dev/j/short"])
    with pytest.raises(SystemExit, match="too short"):
        cli.resolve_job(db, "ab")


def test_add_jobs_duplicate_echoes_the_stored_url(db):
    """Dedup is on url_normalized, so a URL pasted with tracking params can match a
    row stored clean. Echoing the input would misdescribe the row we now own."""
    uid = _mk_user(db)
    first = cli.add_jobs(db, uid, ["https://ex.dev/j/dup"])[0]
    again = cli.add_jobs(db, uid, ["https://ex.dev/j/dup?utm_source=news"])[0]
    assert again["duplicate"] is True
    assert again["job_id"] == first["job_id"]
    assert again["url"] == "https://ex.dev/j/dup"


def test_jobs_list_shows_the_company_until_the_title_is_filled(db, monkeypatch, capsys):
    """jobs.title is blank for every manually added job until its first tailoring
    backfills it, which left the column 40 characters of nothing."""
    uid = _mk_user(db)
    job_id = cli.add_jobs(db, uid, ["https://ex.dev/j/co"])[0]["job_id"]
    repo.set_job_company(db, job_id, "Acme Corp")
    monkeypatch.setattr(cli, "_connect", lambda: db)

    cli.cmd_jobs_list(cli.build_parser().parse_args(["jobs", "list"]))
    assert "Acme Corp" in capsys.readouterr().out


def test_jobs_list_shows_the_company_beside_the_title(db, monkeypatch, capsys):
    """The company must survive once a tailoring backfills the title — it is the
    name you scan for before handing the id to `autoapply ask`."""
    uid = _mk_user(db)
    job_id = cli.add_jobs(db, uid, ["https://ex.dev/j/co2"])[0]["job_id"]
    repo.set_job_company(db, job_id, "Acme Corp")
    repo.set_job_title(db, job_id, "Data Engineer")
    monkeypatch.setattr(cli, "_connect", lambda: db)

    cli.cmd_jobs_list(cli.build_parser().parse_args(["jobs", "list"]))
    out = capsys.readouterr().out
    assert "Acme Corp - Data Engineer" in out


def test_status_shows_the_company_beside_the_title(db, monkeypatch, capsys):
    uid = _mk_user(db)
    job_id = _mk_ready_job(db, uid, "https://ex.dev/j/status-co")
    repo.set_job_company(db, job_id, "Acme Corp")
    repo.set_job_title(db, job_id, "Data Engineer")
    repo.enqueue_tailoring(db, uid, job_id)
    monkeypatch.setattr(cli, "_connect", lambda: db)

    cli.cmd_status(cli.build_parser().parse_args(["status"]))
    assert "Acme Corp - Data Engineer" in capsys.readouterr().out


def test_status_today_words_an_empty_day_like_jobs_list(db, monkeypatch, capsys):
    """`status --today` said "jobs: none   tailoring: none" where `jobs list --today`
    said "no jobs added today" — the same state described two different ways."""
    _mk_user(db)
    monkeypatch.setattr(cli, "_connect", lambda: db)

    cli.cmd_status(cli.build_parser().parse_args(["status", "--today"]))
    assert capsys.readouterr().out.strip() == "no jobs added today"


def test_status_words_an_empty_pipeline_like_jobs_list(db, monkeypatch, capsys):
    _mk_user(db)
    monkeypatch.setattr(cli, "_connect", lambda: db)

    cli.cmd_status(cli.build_parser().parse_args(["status"]))
    assert capsys.readouterr().out.strip() == "no jobs"
