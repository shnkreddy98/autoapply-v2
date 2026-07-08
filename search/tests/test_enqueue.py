"""M3: search enqueue + dedup (integration against live Postgres)."""

from common import queue
from enqueue import enqueue_jobs


def test_url_normalize_unit():
    # pure unit — no infra
    assert queue.url_normalize("https://www.boards.greenhouse.io/acme/jobs/5/") == (
        "https://boards.greenhouse.io/acme/jobs/5"
    )


def _count_jobs(db):
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS c FROM jobs")
        return cur.fetchone()["c"]


def test_enqueue_inserts_pending(db):
    results = [
        {"url": "https://example.com/jobs/1", "title": "DE I"},
        {"url": "https://example.com/jobs/2", "title": "DE II"},
        {"title": "no url -> skipped"},
        {"url": "  ", "title": "blank -> skipped"},
    ]
    ids = enqueue_jobs(results, conn=db)
    assert len(ids) == 2
    assert _count_jobs(db) == 2
    with db.cursor() as cur:
        cur.execute("SELECT DISTINCT status FROM jobs")
        assert [r["status"] for r in cur.fetchall()] == ["jd_pending"]


def test_enqueue_dedups_across_runs(db):
    enqueue_jobs([{"url": "https://www.example.com/jobs/9/", "title": "x"}], conn=db)
    # same posting, different casing/query/scheme -> no new row
    enqueue_jobs([{"url": "http://example.com/jobs/9?ref=abc", "title": "x dup"}], conn=db)
    assert _count_jobs(db) == 1


def test_enqueue_links_owner(db):
    from common import repo

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email) VALUES ('search@test.dev') RETURNING user_id"
        )
        uid = cur.fetchone()["user_id"]
    db.commit()
    ids = enqueue_jobs([{"url": "https://example.com/jobs/3", "title": "DE"}], conn=db, user_id=uid)
    assert repo.job_owner_ids(db, ids[0]) == [uid]
    with db.cursor() as cur:
        cur.execute("SELECT source FROM user_jobs WHERE job_id = %s", (ids[0],))
        assert cur.fetchone()["source"] == "search"
