"""Offline unit tests: storage roundtrip + url normalization. No infra needed."""

import uuid

from common import queue, storage


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
    # same posting, different casing/query/scheme -> identical key
    assert n("HTTP://WWW.Foo.com/x?a=1") == n("https://foo.com/x/")


def test_job_id_for_url_deterministic():
    a = queue.job_id_for_url("https://www.example.com/jobs/1/")
    b = queue.job_id_for_url("http://example.com/jobs/1?ref=z")
    assert a == b
    uuid.UUID(a)  # valid uuid
    assert a != queue.job_id_for_url("https://example.com/jobs/2")
