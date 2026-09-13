"""Offline unit tests: storage roundtrip + url normalization. No infra needed."""

import uuid

from common import keys, queue, storage


def test_base_resume_key_is_collision_free():
    swe = keys.base_resume_key("u1", "swe", "docx", "docx")
    de = keys.base_resume_key("u1", "de", "docx", "docx")
    swe_pdf = keys.base_resume_key("u1", "swe", "pdf", "pdf")
    assert swe == "users/u1/resumes/base/swe/docx.docx"
    assert len({swe, de, swe_pdf}) == 3  # no two role/format pairs share a key


def test_localfs_roundtrip(tmp_path):
    s = storage.LocalFSStorage(str(tmp_path))
    s.put_bytes("users/abc/resumes/base/aws.docx", b"hello", "application/octet-stream")
    assert s.exists("users/abc/resumes/base/aws.docx")
    assert s.get_bytes("users/abc/resumes/base/aws.docx") == b"hello"
    assert not s.exists("nope/missing.bin")


def test_localfs_file_roundtrip(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("payload")
    s = storage.LocalFSStorage(str(tmp_path / "store"))
    s.put_file("jobs/j1/jd.md", str(src))
    dest = tmp_path / "out" / "jd.md"
    s.get_file("jobs/j1/jd.md", str(dest))
    assert dest.read_text() == "payload"


def test_url_normalize():
    n = queue.url_normalize
    assert n("https://www.example.com/jobs/123/") == "https://example.com/jobs/123"
    assert n("http://Example.com/jobs/123?utm=x#frag") == "https://example.com/jobs/123"
    assert n("example.com/a/b/") == "https://example.com/a/b"
    # same posting, different casing/scheme/tracking -> identical key
    assert n("HTTP://WWW.Foo.com/x?utm_source=li") == n("https://foo.com/x/")
    assert n("https://foo.com/x?lang=en&iis=LinkedIn&mode=apply") == "https://foo.com/x"


def test_url_normalize_keeps_posting_identity():
    """The job id often lives in the query or fragment. Dropping either merged two
    real postings onto one dedupe key, so the second was silently swallowed."""
    n = queue.url_normalize
    # greenhouse-embedded: every posting shares the /apply path
    assert n("https://www.inroadsadvisory.com/apply?gh_jid=51946") != n(
        "https://www.inroadsadvisory.com/apply?gh_jid=99999"
    )
    assert n("https://inroadsadvisory.com/apply?gh_jid=51946") == (
        "https://inroadsadvisory.com/apply?gh_jid=51946"
    )
    # SPA board: id in the fragment
    assert n("https://motional.com/open-positions#/6676421003") != n(
        "https://motional.com/open-positions#/1111111111"
    )
    # plain anchors are page furniture, not identity
    assert n("https://boards.greenhouse.io/acme/jobs/123#apply") == (
        "https://boards.greenhouse.io/acme/jobs/123"
    )
    # param order must not split one posting into two keys
    assert n("https://foo.com/x?b=2&a=1") == n("https://foo.com/x?a=1&b=2")
    # identity params survive alongside stripped trackers
    assert n("https://secure7.saashr.com/ta/620.careers?ShowJob=621&lang=en") == (
        "https://secure7.saashr.com/ta/620.careers?ShowJob=621"
    )


def test_canonical_url_drops_apply_form_suffix():
    """The apply page has the form and no JD, so capturing it fails — store the
    posting page instead."""
    c = queue.canonical_url
    ashby = "https://jobs.ashbyhq.com/ostium/f4b646e5-cec6-4716-af52-ce9fa02ff6e2"
    assert c(ashby + "/application?utm_source=2JnBOk4N8m") == (
        ashby + "?utm_source=2JnBOk4N8m"
    )
    assert c("https://jobs.lever.co/acme/abc-123/apply") == (
        "https://jobs.lever.co/acme/abc-123"
    )
    assert c("https://boards.greenhouse.io/acme/jobs/123/apply/") == (
        "https://boards.greenhouse.io/acme/jobs/123"
    )
    # already canonical / non-apply paths are untouched, scheme filled in
    assert c(ashby) == ashby
    assert c("jobs.ashbyhq.com/ostium/x") == "https://jobs.ashbyhq.com/ostium/x"


def test_canonical_url_guards_real_postings():
    """Stripping must not eat a posting whose id is in the query, or a board whose
    postings live at a bare /apply."""
    c = queue.canonical_url
    embedded = "https://www.inroadsadvisory.com/apply?gh_jid=51946"
    assert c(embedded) == embedded
    assert c("https://foo.com/apply") == "https://foo.com/apply"


def test_url_normalize_merges_apply_form_with_posting():
    n = queue.url_normalize
    ashby = "https://jobs.ashbyhq.com/ostium/f4b646e5-cec6-4716-af52-ce9fa02ff6e2"
    assert n(ashby + "/application?utm_source=2JnBOk4N8m") == n(ashby)
    # so re-adding the apply link resolves to the same job row
    assert queue.job_id_for_url(ashby + "/application") == queue.job_id_for_url(ashby)


def test_job_id_for_url_deterministic():
    a = queue.job_id_for_url("https://www.example.com/jobs/1/")
    b = queue.job_id_for_url("http://example.com/jobs/1?ref=z")
    assert a == b
    uuid.UUID(a)  # valid uuid
    assert a != queue.job_id_for_url("https://example.com/jobs/2")
