"""Budget counter + pagination-cutoff behavior (integration against live Postgres,
Brave API fully faked)."""

import brave
import run as runmod
import usage
from enqueue import enqueue_jobs


def _setup_usage(db):
    usage.ensure_table(db)
    with db.cursor() as cur:
        cur.execute("TRUNCATE search_api_usage")
    db.commit()


# ---------------- usage counter ----------------

def test_try_consume_counts_and_caps(db):
    _setup_usage(db)
    assert usage.try_consume(db, cap=2) == 1
    assert usage.try_consume(db, cap=2) == 2
    assert usage.try_consume(db, cap=2) is None  # cap reached, nothing consumed
    assert usage.used_this_month(db) == 2


def test_used_this_month_empty(db):
    _setup_usage(db)
    assert usage.used_this_month(db) == 0


# ---------------- seen-split ----------------

def test_split_seen_partitions_against_jobs(db):
    enqueue_jobs([{"url": "https://example.com/jobs/old", "title": "old"}], conn=db)
    results = [
        {"url": "https://www.example.com/jobs/old/", "title": "old again"},  # normalizes to seen
        {"url": "https://example.com/jobs/new", "title": "new"},
    ]
    new, seen_count = runmod.split_seen(db, results)
    assert seen_count == 1
    assert [r["url"] for r in new] == ["https://example.com/jobs/new"]


# ---------------- pagination cutoff ----------------

class _NoCloseConn:
    """run() closes the connection it opens; keep the fixture's alive."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _patch_db(monkeypatch, db):
    fake_db = type("D", (), {"connect": staticmethod(lambda: _NoCloseConn(db))})
    monkeypatch.setattr(runmod, "db", fake_db)


def _fake_pages(pages):
    """Return a search_page stub serving `pages[offset]` and recording calls."""
    calls = []

    def fake(query, offset=0, **kw):
        calls.append(offset)
        return pages[offset] if offset < len(pages) else []

    return fake, calls


def test_run_stops_paging_on_seen_url(db, monkeypatch):
    _setup_usage(db)
    enqueue_jobs([{"url": "https://x.test/jobs/seen", "title": "seen"}], conn=db)

    full = brave.PAGE_SIZE
    page0 = [{"url": f"https://x.test/jobs/n{i}", "title": "n"} for i in range(full)]
    page1 = [{"url": "https://x.test/jobs/seen", "title": "seen"}] + [
        {"url": f"https://x.test/jobs/m{i}", "title": "m"} for i in range(full - 1)
    ]
    page2 = [{"url": "https://x.test/jobs/never", "title": "never fetched"}]
    fake, calls = _fake_pages([page0, page1, page2])

    monkeypatch.setattr(brave, "search_page", fake)
    monkeypatch.setattr(runmod, "REQUEST_GAP_S", 0)
    monkeypatch.setattr(runmod, "load_sites", lambda tier: ["x.test"])
    _patch_db(monkeypatch, db)

    assert runmod.run("data engineer", "United States", "1", max_pages=5, cap=100) == 0
    assert calls == [0, 1]  # page2 never fetched: page1 contained a seen URL
    assert usage.used_this_month(db) == 2
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS c FROM jobs")
        # 1 pre-seeded + 20 from page0 + 19 new from page1
        assert cur.fetchone()["c"] == 1 + full + (full - 1)


def test_run_respects_budget_cap(db, monkeypatch):
    _setup_usage(db)
    full = brave.PAGE_SIZE
    pages = [
        [{"url": f"https://y.test/jobs/{p}-{i}", "title": "t"} for i in range(full)]
        for p in range(3)
    ]
    fake, calls = _fake_pages(pages)

    monkeypatch.setattr(brave, "search_page", fake)
    monkeypatch.setattr(runmod, "REQUEST_GAP_S", 0)
    monkeypatch.setattr(runmod, "load_sites", lambda tier: ["y.test", "z.test"])
    _patch_db(monkeypatch, db)

    assert runmod.run("data engineer", "United States", "1", max_pages=5, cap=2) == 0
    assert len(calls) == 2  # third request refused by budget
    assert usage.used_this_month(db) == 2


def test_run_refuses_when_budget_already_spent(db, monkeypatch):
    _setup_usage(db)
    usage.try_consume(db, cap=1)
    _patch_db(monkeypatch, db)
    assert runmod.run("data engineer", "United States", "1", max_pages=5, cap=1) == 2
