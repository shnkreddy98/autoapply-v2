"""Integration tests against live Postgres + MinIO (root compose)."""

import uuid

from common import db as dbmod
from common import queue, repo


def test_s3_roundtrip(s3):
    key = f"jobs/{uuid.uuid4()}/jd.md"
    s3.put_bytes(key, b"# Job\nsome jd text", "text/markdown")
    assert s3.exists(key)
    assert s3.get_bytes(key) == b"# Job\nsome jd text"


def test_get_or_create_job_dedup(db):
    a = repo.get_or_create_job(db, "https://www.example.com/jobs/1/", "Eng")
    b = repo.get_or_create_job(db, "http://example.com/jobs/1?ref=x", "Eng (dup)")
    assert a == b  # normalized URL dedupes to one row
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS c FROM jobs")
        assert cur.fetchone()["c"] == 1


def test_set_job_title_fills_only_a_blank(db):
    """A second user's tailoring must not rewrite the title the first one wrote —
    same rule as set_job_company, which shares the backfill."""
    job_id = repo.get_or_create_job(db, "https://example.com/jobs/title")
    repo.set_job_title(db, job_id, "  Data Engineer  ")
    repo.set_job_title(db, job_id, "Something Else")
    repo.set_job_title(db, job_id, None)
    with db.cursor() as cur:
        cur.execute("SELECT title FROM jobs WHERE job_id = %s", (job_id,))
        assert cur.fetchone()["title"] == "Data Engineer"


def test_question_key_collapses_decoration():
    assert repo.question_key("First Name *") == repo.question_key("first name:")
    assert repo.question_key("  Why   Acme?? ") == "why acme"


def test_saved_answer_scopes_do_not_leak(db, seed_user):
    """A per-job answer ("why this company") must never come back for another job,
    and a reusable one must not shadow it."""
    user_id, _ = seed_user
    job_a = repo.add_job_for_user(db, user_id, "https://example.com/jobs/a")
    job_b = repo.add_job_for_user(db, user_id, "https://example.com/jobs/b")
    repo.save_answer(db, user_id, "Years of Spark?", "5", source="human", options=None)
    repo.save_answer(db, user_id, "Why Acme?", "for a", source="llm", job_id=job_a)

    assert repo.get_saved_answer(db, user_id, "years of spark")["answer"] == "5"
    assert repo.get_saved_answer(db, user_id, "Why Acme?", job_id=job_a)["answer"] == "for a"
    assert repo.get_saved_answer(db, user_id, "Why Acme?", job_id=job_b) is None
    assert repo.get_saved_answer(db, user_id, "Why Acme?") is None


def test_save_answer_replaces_the_earlier_answer(db, seed_user):
    """A human correction must replace the LLM answer, not sit beside it."""
    user_id, _ = seed_user
    first = repo.save_answer(db, user_id, "Remote OK?", "No", source="llm", options=["Yes", "No"])
    again = repo.save_answer(db, user_id, "remote ok", "Yes", source="human", options=["Yes", "No"])
    assert first == again
    row = repo.get_saved_answer(db, user_id, "Remote OK?")
    assert (row["answer"], row["source"]) == ("Yes", "human")
    repo.mark_answer_used(db, row["id"])
    assert repo.get_saved_answer(db, user_id, "Remote OK?")["times_used"] == 1


def test_remove_job_drops_its_per_job_answers(db, seed_user):
    user_id, _ = seed_user
    job_id = repo.add_job_for_user(db, user_id, "https://example.com/jobs/rm-answers")
    repo.save_answer(db, user_id, "Why us?", "because", source="llm", job_id=job_id)
    repo.save_answer(db, user_id, "Years of Go?", "3", source="human")
    repo.remove_job_for_user(db, user_id, job_id)
    assert repo.get_saved_answer(db, user_id, "Why us?", job_id=job_id) is None
    assert repo.get_saved_answer(db, user_id, "Years of Go?")["answer"] == "3"


def test_enqueue_tailoring_unique(db, seed_user):
    user_id, asset_id = seed_user
    job_id = repo.get_or_create_job(db, "https://example.com/jobs/9")
    t1 = repo.enqueue_tailoring(db, user_id, job_id, asset_id)
    t2 = repo.enqueue_tailoring(db, user_id, job_id, asset_id)
    assert t1 == t2  # UNIQUE(user_id, job_id)
    row = repo.lookup_tailored(db, user_id, job_id)
    assert row["status"] == "pending"


def test_enqueue_tailoring_refreshes_base_on_conflict(db, seed_user):
    """Re-enqueuing with a different base swaps base_asset_id — needed so a per-job
    re-tailor with a role label actually changes the source resume."""
    user_id, asset_id = seed_user
    other = repo.insert_resume_asset(
        db, user_id, "base", f"users/{user_id}/resumes/base/swe/docx.docx",
        variant="docx", label="swe",
    )
    job_id = repo.get_or_create_job(db, "https://example.com/jobs/rebind")
    repo.enqueue_tailoring(db, user_id, job_id, asset_id)
    repo.enqueue_tailoring(db, user_id, job_id, other)
    assert repo.lookup_tailored(db, user_id, job_id)["base_asset_id"] == other


def _mk_base(db, user_id, label, variant="docx"):
    key = f"users/{user_id}/resumes/base/{label or 'default'}/{variant}.docx"
    return repo.insert_resume_asset(
        db, user_id, "base", key, variant=variant, label=label
    )


def test_get_base_asset_label_selection(db, seed_user):
    """Default selection (label=None) must ignore role bases, and a named label must
    return exactly that base."""
    user_id, _ = seed_user
    default_id = _mk_base(db, user_id, None)
    swe_id = _mk_base(db, user_id, "swe")

    assert repo.get_base_asset(db, user_id, variant="docx")["id"] == default_id
    assert repo.get_base_asset(db, user_id, variant="docx", label="swe")["id"] == swe_id
    assert repo.get_base_asset(db, user_id, variant="docx", label="nope") is None


def test_list_base_assets(db, seed_user):
    user_id, _ = seed_user
    _mk_base(db, user_id, None)
    _mk_base(db, user_id, "swe")
    labels = {b["label"] for b in repo.list_base_assets(db, user_id)}
    assert {None, "swe"} <= labels


def test_claim_pending_skip_locked(db):
    # two pending jobs; two concurrent connections must claim DIFFERENT rows
    repo.get_or_create_job(db, "https://example.com/jobs/a")
    repo.get_or_create_job(db, "https://example.com/jobs/b")

    c1, c2 = dbmod.connect(), dbmod.connect()
    try:
        r1 = queue.claim_pending(c1, "jobs", worker_id="w1")
        r2 = queue.claim_pending(c2, "jobs", worker_id="w2")
        assert r1 is not None and r2 is not None
        assert r1["job_id"] != r2["job_id"]
        # both flipped out of jd_pending
        with db.cursor() as cur:
            cur.execute("SELECT count(*) AS c FROM jobs WHERE status = 'jd_pending'")
            assert cur.fetchone()["c"] == 0
        assert queue.claim_pending(c1, "jobs") is None  # queue drained
    finally:
        c1.close()
        c2.close()


def test_mark_terminal(db):
    job_id = repo.get_or_create_job(db, "https://example.com/jobs/mark")
    queue.claim_pending(db, "jobs")
    queue.mark(db, "jobs", job_id, "jd_ready", jd_s3_key=f"jobs/{job_id}/jd.md")
    with db.cursor() as cur:
        cur.execute("SELECT status, jd_s3_key FROM jobs WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
    assert row["status"] == "jd_ready"
    assert row["jd_s3_key"] == f"jobs/{job_id}/jd.md"


def test_fetch_profile(db, seed_user):
    user_id, _ = seed_user
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO application_profiles (user_id, city, work_eligible_us) VALUES (%s, %s, %s)",
            (user_id, "Omaha", True),
        )
        cur.execute(
            "INSERT INTO work_experiences (user_id, job_title, company_name, bullets) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, "Data Engineer", "Airfold", ["built pipelines", "owned dbt"]),
        )
    db.commit()
    p = repo.fetch_profile(db, user_id)
    assert p.user["full_name"] == "Test User"
    assert p.profile["city"] == "Omaha"
    assert len(p.work_experiences) == 1
    assert p.work_experiences[0]["bullets"] == ["built pipelines", "owned dbt"]


def test_base_label_for_job_reports_the_tailoring_source(db, seed_user):
    """`autoapply ask` follows this to pick the resume it answers from: the base the
    job was tailored with, not the user's default."""
    user_id, _ = seed_user
    aiml = _mk_base(db, user_id, "aiml")
    job_id = repo.get_or_create_job(db, "https://example.com/jobs/label-lookup")
    repo.enqueue_tailoring(db, user_id, job_id, aiml)
    assert repo.base_label_for_job(db, user_id, job_id) == "aiml"


def test_base_label_for_job_is_none_without_a_labelled_source(db, seed_user):
    """No tailoring row, or one on the unlabelled base, both mean 'default base'."""
    user_id, asset_id = seed_user
    job_id = repo.get_or_create_job(db, "https://example.com/jobs/label-none")
    assert repo.base_label_for_job(db, user_id, job_id) is None
    repo.enqueue_tailoring(db, user_id, job_id, asset_id)
    assert repo.base_label_for_job(db, user_id, job_id) is None


def test_base_label_for_job_is_per_user(db, seed_user):
    """Two users can tailor the same posting from different bases; the lookup must
    not leak the other user's label into this user's answers."""
    user_id, _ = seed_user
    aiml = _mk_base(db, user_id, "aiml")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email) VALUES (%s) RETURNING user_id", ("two@e.co",)
        )
        other = cur.fetchone()["user_id"]
    db.commit()
    job_id = repo.get_or_create_job(db, "https://example.com/jobs/label-shared")
    repo.enqueue_tailoring(db, user_id, job_id, aiml)
    assert repo.base_label_for_job(db, other, job_id) is None
