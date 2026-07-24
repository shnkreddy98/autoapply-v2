"""/answer endpoint orchestration: ownership, context loading, error mapping.
Live PG+MinIO with the agent stubbed out (no LLM)."""

import uuid

import pytest
from fastapi.testclient import TestClient

import qa
import service
from common import keys, repo


def _docx_bytes(tmp_path, lines):
    from docx import Document

    doc = Document()
    for line in lines:
        doc.add_paragraph(line)
    path = tmp_path / "base.docx"
    doc.save(str(path))
    return path.read_bytes()


def _seed(db, s3, tmp_path, *, with_jd=True, with_resume=True):
    """The `db` fixture TRUNCATEs Postgres between tests but MinIO is never cleaned,
    and job_id is a uuid5 of the URL — so a fixed URL would reuse the previous test's
    JD object. Every seed gets its own URL."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email) VALUES (%s) RETURNING user_id", ("q@example.com",)
        )
        user_id = cur.fetchone()["user_id"]
    db.commit()
    if with_resume:
        key = keys.base_resume_key(user_id)
        s3.put_bytes(key, _docx_bytes(tmp_path, ["Jane Dev", "Built dbt pipelines"]))
        repo.insert_resume_asset(db, user_id, "base", key, variant="default")
    job_id = repo.add_job_for_user(db, user_id, f"https://example.com/jobs/{uuid.uuid4()}")
    if with_jd:
        s3.put_bytes(keys.jd_key(job_id), b"# Job\nNeed dbt + Airflow.", "text/markdown")
    return user_id, job_id


@pytest.fixture
def client(monkeypatch):
    service._storage.cache_clear()
    return TestClient(service.app)


def _stub_agent(monkeypatch, captured=None, reply="I built dbt pipelines end to end."):
    async def fake(resume, jd, question, max_words):
        if captured is not None:
            captured.update(resume=resume, jd=jd, question=question, max_words=max_words)
        return reply

    monkeypatch.setattr(qa, "run_agent", fake)


def test_answer_success(db, s3, tmp_path, client, monkeypatch):
    user_id, job_id = _seed(db, s3, tmp_path)
    seen = {}
    _stub_agent(monkeypatch, seen)

    r = client.post(
        "/answer",
        json={"user_id": str(user_id), "job_id": str(job_id), "question": "Why you?"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "I built dbt pipelines end to end."
    assert body["word_count"] == 7
    assert body["max_words"] == 100

    assert "dbt pipelines" in seen["resume"]   # resume .docx parsed to text
    assert "Airflow" in seen["jd"]             # captured JD passed through
    assert seen["question"] == "Why you?"
    assert seen["max_words"] == 100


def test_max_words_is_forwarded_and_echoed(db, s3, tmp_path, client, monkeypatch):
    user_id, job_id = _seed(db, s3, tmp_path)
    seen = {}
    _stub_agent(monkeypatch, seen, reply="one two three four five.")

    r = client.post(
        "/answer",
        json={
            "user_id": str(user_id),
            "job_id": str(job_id),
            "question": "Why you?",
            "max_words": 15,
        },
    )
    assert r.status_code == 200
    assert seen["max_words"] == 15
    assert r.json() == {"answer": "one two three four five.", "word_count": 5, "max_words": 15}


def test_answer_rejects_job_the_user_does_not_own(db, s3, tmp_path, client, monkeypatch):
    _seed(db, s3, tmp_path)
    _stub_agent(monkeypatch)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email) VALUES (%s) RETURNING user_id", ("other@e.co",)
        )
        stranger = cur.fetchone()["user_id"]
    db.commit()
    other_job = repo.get_or_create_job(db, f"https://example.com/jobs/{uuid.uuid4()}")

    r = client.post(
        "/answer",
        json={"user_id": str(stranger), "job_id": str(other_job), "question": "Why?"},
    )
    assert r.status_code == 404


def test_answer_without_captured_jd_is_409(db, s3, tmp_path, client, monkeypatch):
    user_id, job_id = _seed(db, s3, tmp_path, with_jd=False)
    _stub_agent(monkeypatch)

    r = client.post(
        "/answer",
        json={"user_id": str(user_id), "job_id": str(job_id), "question": "Why?"},
    )
    assert r.status_code == 409
    assert "JD" in r.json()["detail"]


def test_answer_without_base_resume_is_404(db, s3, tmp_path, client, monkeypatch):
    user_id, job_id = _seed(db, s3, tmp_path, with_resume=False)
    _stub_agent(monkeypatch)

    r = client.post(
        "/answer",
        json={"user_id": str(user_id), "job_id": str(job_id), "question": "Why?"},
    )
    assert r.status_code == 404
    assert "base resume" in r.json()["detail"]


def test_bearer_token_enforced_when_configured(db, s3, tmp_path, monkeypatch):
    user_id, job_id = _seed(db, s3, tmp_path)
    _stub_agent(monkeypatch)
    monkeypatch.setattr(service, "TOKEN", "s3cret")
    c = TestClient(service.app)
    payload = {"user_id": str(user_id), "job_id": str(job_id), "question": "Why?"}

    assert c.post("/answer", json=payload).status_code == 401
    assert c.post(
        "/answer", json=payload, headers={"Authorization": "Bearer wrong"}
    ).status_code == 401
    assert c.post(
        "/answer", json=payload, headers={"Authorization": "Bearer s3cret"}
    ).status_code == 200


def test_max_words_out_of_range_is_422(db, s3, tmp_path, client, monkeypatch):
    user_id, job_id = _seed(db, s3, tmp_path)
    _stub_agent(monkeypatch)
    r = client.post(
        "/answer",
        json={
            "user_id": str(user_id),
            "job_id": str(job_id),
            "question": "Why?",
            "max_words": 5,
        },
    )
    assert r.status_code == 422
