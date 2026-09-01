"""API behavior: auth, per-user isolation, whitelists, secret non-exposure."""

from common import repo


def _uid(client) -> str:
    return client.get("/me").json()["user_id"]


# ---------------------------------------------------------------------------
# auth


def test_unauthenticated_401(client_factory):
    anon = client_factory()
    for path in ("/me", "/jobs", "/profile", "/applications", "/gmail/status"):
        assert anon.get(path).status_code == 401, path


def test_login_sets_session_and_me_works(client_factory):
    c = client_factory("a@test.dev")
    r = c.get("/me")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "a@test.dev"
    assert body["role"] == "user"


def test_login_attaches_to_seeded_profile(db, client_factory):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, full_name) VALUES ('seed@test.dev', 'Seeded') "
            "RETURNING user_id"
        )
        seeded = str(cur.fetchone()["user_id"])
    db.commit()
    c = client_factory("seed@test.dev")
    assert _uid(c) == seeded


def test_logout_revokes_session(client_factory):
    c = client_factory("a@test.dev")
    assert c.get("/me").status_code == 200
    assert c.post("/auth/logout").status_code == 200
    assert c.get("/me").status_code == 401


def test_bad_state_rejected(client_factory):
    c = client_factory("a@test.dev")
    r = c.get("/auth/google/callback?code=x&state=forged", follow_redirects=False)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# jobs isolation (through the RLS-bound connection)


def test_jobs_isolated_between_sessions(client_factory):
    a = client_factory("a@test.dev")
    b = client_factory("b@test.dev")

    r = a.post("/jobs", json={"urls": ["https://example.com/jobs/1"]})
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1
    job_id = r.json()[0]["job_id"]

    assert [j["job_id"] for j in a.get("/jobs").json()] == [job_id]
    assert b.get("/jobs").json() == []
    assert b.get(f"/jobs/{job_id}").status_code == 404
    assert b.get(f"/jobs/{job_id}/tailoring").status_code == 404
    assert b.get(f"/jobs/{job_id}/resume").status_code == 404


def test_same_url_deduped_across_users(client_factory):
    a = client_factory("a@test.dev")
    b = client_factory("b@test.dev")
    ja = a.post("/jobs", json={"urls": ["https://example.com/jobs/1"]}).json()[0]
    jb = b.post("/jobs", json={"urls": ["http://www.example.com/jobs/1/"]}).json()[0]
    assert ja["job_id"] == jb["job_id"]  # global dedup, both owners


# ---------------------------------------------------------------------------
# profile whitelist


def test_profile_roundtrip_and_whitelist(client_factory):
    c = client_factory("a@test.dev")
    assert c.get("/profile").status_code == 200

    r = c.put("/profile", json={"city": "Berlin", "willing_relocate": True})
    assert r.status_code == 200
    assert r.json()["city"] == "Berlin"
    assert c.get("/profile").json()["willing_relocate"] is True

    # unknown / managed fields are rejected outright, not silently dropped
    assert c.put("/profile", json={"user_id": "x"}).status_code == 422
    assert c.put("/profile", json={"nonsense": 1}).status_code == 422


# ---------------------------------------------------------------------------
# secrets never serialize


def test_no_secret_fields_in_any_response(db, client_factory):
    c = client_factory("a@test.dev")
    uid = _uid(c)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO portal_credentials (user_id, login_email, login_password) "
            "VALUES (%s, 'a@test.dev', 'portal-hunter2')",
            (uid,),
        )
    db.commit()
    repo.save_gmail_token(db, uid, "a@gmail.com", "google-refresh-hunter2")
    c.post("/jobs", json={"urls": ["https://example.com/jobs/1"]})

    for path in ("/me", "/profile", "/jobs", "/applications", "/gmail/status"):
        text = c.get(path).text
        assert "hunter2" not in text, path
        assert "refresh_token" not in text, path
        assert "login_password" not in text, path

    g = c.get("/gmail/status").json()
    assert g == {
        "connected": True,
        "gmail_address": g["gmail_address"],
        "updated_at": g["updated_at"],
    }


# ---------------------------------------------------------------------------
# tailored resume link


def test_resume_presigned_link(db, client_factory, tmp_path):
    import os

    from api import routes

    routes._storage.cache_clear()
    os.environ["STORAGE_LOCAL_ROOT"] = str(tmp_path)

    c = client_factory("a@test.dev")
    uid = _uid(c)
    job_id = c.post("/jobs", json={"urls": ["https://example.com/jobs/1"]}).json()[0]["job_id"]

    key = f"users/{uid}/resumes/tailored/{job_id}.pdf"
    (tmp_path / key).parent.mkdir(parents=True)
    (tmp_path / key).write_bytes(b"%PDF")
    asset_id = repo.insert_resume_asset(db, uid, "tailored", key, job_id=job_id)
    tid = repo.enqueue_tailoring(db, uid, job_id)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE tailored_resumes SET status = 'ready', tailored_asset_id = %s "
            "WHERE id = %s",
            (asset_id, tid),
        )
    db.commit()

    r = c.get(f"/jobs/{job_id}/resume")
    assert r.status_code == 200, r.text
    assert r.json()["url"].endswith(f"{job_id}.pdf")


# ---------------------------------------------------------------------------
# admin


def test_admin_endpoints_gated(db, client_factory):
    user = client_factory("a@test.dev")
    assert user.get("/admin/users").status_code == 403
    assert user.get("/admin/audit").status_code == 403

    admin = client_factory("boss@test.dev")
    with db.cursor() as cur:
        cur.execute("UPDATE users SET role = 'admin' WHERE email = 'boss@test.dev'")
    db.commit()
    r = admin.get("/admin/users")
    assert r.status_code == 200
    assert {u["email"] for u in r.json()} == {"a@test.dev", "boss@test.dev"}


def test_mutations_audited(db, client_factory):
    c = client_factory("a@test.dev")
    c.post("/jobs", json={"urls": ["https://example.com/jobs/1"]})
    c.put("/profile", json={"city": "Berlin"})
    with db.cursor() as cur:
        cur.execute("SELECT action FROM audit_log ORDER BY id")
        actions = [r["action"] for r in cur.fetchall()]
    assert "auth.login" in actions
    assert "POST /jobs" in actions
    assert "PUT /profile" in actions


# ---------------------------------------------------------------------------
# /jobs/{id}/answer — proxy to ai-assist (upstream stubbed; no LLM, no network)


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {"content-type": "application/json"} if payload is not None else {}

    def json(self):
        return self._payload


def _stub_ai_assist(monkeypatch, response, captured=None):
    """Replace the httpx client the route builds with one returning `response`."""
    import httpx

    from api import routes

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            if captured is not None:
                captured.update(url=url, json=json, headers=headers)
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(routes.httpx, "AsyncClient", FakeClient)
    return httpx


def test_answer_requires_auth(client_factory, monkeypatch):
    anon = client_factory()
    r = anon.post(
        "/jobs/00000000-0000-0000-0000-000000000000/answer", json={"question": "Why?"}
    )
    assert r.status_code == 401


def test_answer_404s_for_a_job_the_user_does_not_own(db, client_factory, monkeypatch):
    a = client_factory("a@test.dev")
    b = client_factory("b@test.dev")
    job_id = b.post("/jobs", json={"urls": ["https://example.com/jobs/private"]}).json()[0]["job_id"]
    _stub_ai_assist(monkeypatch, _FakeResponse(200, {"answer": "x", "word_count": 1, "max_words": 100}))

    assert a.post(f"/jobs/{job_id}/answer", json={"question": "Why?"}).status_code == 404


def test_answer_forwards_identity_and_returns_upstream_body(client_factory, monkeypatch):
    c = client_factory("a@test.dev")
    user_id = _uid(c)
    job_id = c.post("/jobs", json={"urls": ["https://example.com/jobs/1"]}).json()[0]["job_id"]
    seen = {}
    _stub_ai_assist(
        monkeypatch,
        _FakeResponse(200, {"answer": "I built it.", "word_count": 3, "max_words": 60}),
        seen,
    )
    monkeypatch.setenv("AI_ASSIST_TOKEN", "tok")

    r = c.post(f"/jobs/{job_id}/answer", json={"question": "Why you?", "max_words": 60})
    assert r.status_code == 200
    # base_label is null here because this stubbed upstream predates it; the field is
    # optional precisely so an older ai-assist still validates against the model
    assert r.json() == {
        "answer": "I built it.", "word_count": 3, "max_words": 60, "base_label": None,
    }

    # the caller never supplies user_id — it comes from the session
    assert seen["json"] == {
        "user_id": user_id,
        "job_id": job_id,
        "question": "Why you?",
        "max_words": 60,
        "label": None,  # unset -> ai-assist follows the job's own tailoring base
    }
    assert seen["headers"]["Authorization"] == "Bearer tok"


def test_answer_forwards_the_label_and_returns_the_base_used(client_factory, monkeypatch):
    """The role base has to survive the proxy in both directions, or an API caller
    cannot pick the resume and cannot tell which one answered."""
    c = client_factory("labels@test.dev")
    job_id = c.post("/jobs", json={"urls": ["https://example.com/jobs/lbl"]}).json()[0]["job_id"]
    seen = {}
    _stub_ai_assist(
        monkeypatch,
        _FakeResponse(
            200,
            {"answer": "RAG.", "word_count": 1, "max_words": 100, "base_label": "aiml"},
        ),
        seen,
    )

    r = c.post(f"/jobs/{job_id}/answer", json={"question": "RAG?", "label": "aiml"})
    assert r.status_code == 200
    assert seen["json"]["label"] == "aiml"
    assert r.json()["base_label"] == "aiml"


def test_answer_passes_through_upstream_4xx(client_factory, monkeypatch):
    c = client_factory("a@test.dev")
    job_id = c.post("/jobs", json={"urls": ["https://example.com/jobs/1"]}).json()[0]["job_id"]
    _stub_ai_assist(monkeypatch, _FakeResponse(409, {"detail": "no captured JD for this job yet"}))

    r = c.post(f"/jobs/{job_id}/answer", json={"question": "Why?"})
    assert r.status_code == 409
    assert "no captured JD" in r.json()["detail"]


def test_answer_maps_upstream_5xx_to_502(client_factory, monkeypatch):
    c = client_factory("a@test.dev")
    job_id = c.post("/jobs", json={"urls": ["https://example.com/jobs/1"]}).json()[0]["job_id"]
    _stub_ai_assist(monkeypatch, _FakeResponse(500, None, text="boom"))

    assert c.post(f"/jobs/{job_id}/answer", json={"question": "Why?"}).status_code == 502


def test_answer_503_when_ai_assist_is_down(client_factory, monkeypatch):
    c = client_factory("a@test.dev")
    job_id = c.post("/jobs", json={"urls": ["https://example.com/jobs/1"]}).json()[0]["job_id"]
    httpx = _stub_ai_assist(monkeypatch, None)
    _stub_ai_assist(monkeypatch, httpx.ConnectError("connection refused"))

    r = c.post(f"/jobs/{job_id}/answer", json={"question": "Why?"})
    assert r.status_code == 503
    assert "ai-assist unreachable" in r.json()["detail"]
