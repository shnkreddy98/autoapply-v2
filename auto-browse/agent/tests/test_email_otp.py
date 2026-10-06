"""Tests for the Gmail 2FA tool: deterministic extraction/ranking (pure, offline),
the fetch flow against a fake Gmail API, token storage, and loop wiring."""

import base64
import json

import httpx
import pytest

from agent import email_otp
from agent.loop import EMAIL_TOOL, EMAIL_TOOL_NAME


# ---------------------------------------------------------------------------
# extract_code


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Your verification code is 483920.", "483920"),
        ("Use code 7712 to continue", "7712"),
        ("OTP: 123456", "123456"),
        ("Enter this one-time passcode: 90817263", "90817263"),
        ("Your Workday PIN is\n\n  5531", "5531"),
        ("verification code\n483-920\nexpires in 10 minutes", "483920"),
        ("To verify your email, enter 337 401", "337401"),
        ("Please verify your account.\n\n604512\n\nThis code expires soon.", "604512"),
    ],
)
def test_extract_code_hits(text, code):
    assert email_otp.extract_code(text) == code


@pytest.mark.parametrize(
    "text",
    [
        "Thanks for applying! We received your application on 2026-07-07.",
        "Our office: 94105 Market St, salary range 120000-150000.",  # numbers, no OTP context
        "Click the link below to confirm your email address.",
        "",
    ],
)
def test_extract_code_misses(text):
    assert email_otp.extract_code(text) is None


def test_extract_code_prefers_keyword_adjacent():
    text = "Ref 999999. Your verification code is 123456."
    assert email_otp.extract_code(text) == "123456"


# ---------------------------------------------------------------------------
# rank_messages


def _meta(mid, frm, subject, ms=0):
    return {"id": mid, "from": frm, "subject": subject, "internal_ms": ms}


def test_rank_from_domain_beats_subject_only():
    metas = [
        _meta("a", "news@random.dev", "Your Acme verification code", ms=2),
        _meta("b", "no-reply@acme.com", "Verify your email", ms=1),
    ]
    ranked = email_otp.rank_messages(metas, "Acme", None)
    assert [m["id"] for m in ranked] == ["b", "a"]


def test_rank_zero_score_filtered():
    metas = [_meta("a", "friend@gmail.com", "lunch?")]
    assert email_otp.rank_messages(metas, "Acme", None) == []


def test_rank_newest_wins_ties():
    metas = [
        _meta("old", "no-reply@acme.com", "code", ms=1),
        _meta("new", "no-reply@acme.com", "code", ms=2),
    ]
    assert email_otp.rank_messages(metas, "acme", None)[0]["id"] == "new"


def test_rank_sender_hint_counts():
    metas = [
        _meta("a", "no-reply@myworkday.com", "Sign-in code", ms=1),
        _meta("b", "jobs@other.dev", "Sign-in code for workday", ms=2),
    ]
    ranked = email_otp.rank_messages(metas, "SomeCo", "myworkday.com")
    assert ranked[0]["id"] == "a"


# ---------------------------------------------------------------------------
# body_text / format_result


def _part(mime, text):
    return {
        "mimeType": mime,
        "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()},
    }


def test_body_text_prefers_plain():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            _part("text/plain", "plain body"),
            _part("text/html", "<b>html body</b>"),
        ],
    }
    assert email_otp.body_text(payload) == "plain body"


def test_body_text_html_stripped_keeps_links():
    payload = _part(
        "text/html", '<p>Confirm: <a href="https://x.dev/verify?t=1">here</a></p>'
    )
    text = email_otp.body_text(payload)
    assert "https://x.dev/verify?t=1" in text
    assert "<a" not in text


def test_format_result_code_path_hides_body():
    out = email_otp.format_result(
        "no-reply@acme.com", "Your code", "code is 123456\nSECRETBODY"
    )
    assert "VERIFICATION CODE: 123456" in out
    assert "SECRETBODY" not in out


def test_format_result_link_path_fenced():
    out = email_otp.format_result(
        "no-reply@acme.com", "Confirm email", "click https://a.dev/v?t=9"
    )
    assert "<EMAIL_CONTENT untrusted>" in out
    assert "https://a.dev/v?t=9" in out


# ---------------------------------------------------------------------------
# fetch_email against a fake Gmail API


def _fake_transport(messages):
    """messages: list of dicts {id, from, subject, body, internal_ms}."""

    def handler(request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, dict(request.url.params)
        if path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok"})
        if path.endswith("/messages"):
            return httpx.Response(
                200, json={"messages": [{"id": m["id"]} for m in messages]}
            )
        mid = path.rsplit("/", 1)[-1]
        m = next(x for x in messages if x["id"] == mid)
        payload = {
            "headers": [
                {"name": "From", "value": m["from"]},
                {"name": "Subject", "value": m["subject"]},
            ],
        }
        if q.get("format") == "full":
            payload.update(_part("text/plain", m["body"]))
        return httpx.Response(
            200,
            json={"id": mid, "internalDate": str(m["internal_ms"]), "payload": payload},
        )

    return httpx.MockTransport(handler)


_REAL_ASYNC_CLIENT = httpx.AsyncClient  # bound before any monkeypatching


async def _fetch(messages, company, *, seen=None, timeout=0.0, monkeypatch=None):
    transport = _fake_transport(messages)

    def patched_client(**kw):
        return _REAL_ASYNC_CLIENT(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)

    async def no_sleep(_):
        return None

    return await email_otp.fetch_email(
        "rt",
        company,
        None,
        0,
        seen if seen is not None else set(),
        timeout=timeout,
        poll=0.01,
        sleep=no_sleep,
    )


@pytest.fixture(autouse=True)
def _gmail_env(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "sec")


async def test_fetch_code_email(monkeypatch):
    msgs = [
        {
            "id": "1",
            "from": "no-reply@acme.com",
            "subject": "Acme code",
            "body": "Your verification code is 314159",
            "internal_ms": 5,
        },
    ]
    out = await _fetch(msgs, "Acme", monkeypatch=monkeypatch)
    assert "VERIFICATION CODE: 314159" in out


async def test_fetch_link_email_returns_fenced_body(monkeypatch):
    msgs = [
        {
            "id": "1",
            "from": "no-reply@acme.com",
            "subject": "Confirm your Acme account",
            "body": "Confirm here: https://acme.com/verify?t=abc",
            "internal_ms": 5,
        },
    ]
    out = await _fetch(msgs, "Acme", monkeypatch=monkeypatch)
    assert "<EMAIL_CONTENT untrusted>" in out
    assert "https://acme.com/verify?t=abc" in out


async def test_fetch_no_match_times_out(monkeypatch):
    msgs = [
        {
            "id": "1",
            "from": "x@other.dev",
            "subject": "hi",
            "body": "hey",
            "internal_ms": 5,
        }
    ]
    out = await _fetch(msgs, "Acme", monkeypatch=monkeypatch)
    assert out == email_otp.NOT_FOUND


async def test_fetch_seen_set_prevents_regrab(monkeypatch):
    msgs = [
        {
            "id": "1",
            "from": "no-reply@acme.com",
            "subject": "Acme code",
            "body": "code is 111111",
            "internal_ms": 5,
        },
    ]
    seen = set()
    first = await _fetch(msgs, "Acme", seen=seen, monkeypatch=monkeypatch)
    assert "111111" in first
    second = await _fetch(msgs, "Acme", seen=seen, monkeypatch=monkeypatch)
    assert second == email_otp.NOT_FOUND


def test_client_config_missing_env(monkeypatch):
    monkeypatch.delenv("GMAIL_CLIENT_ID", raising=False)
    monkeypatch.delenv("GMAIL_CLIENT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="GMAIL_CLIENT_ID"):
        email_otp.client_config()


# ---------------------------------------------------------------------------
# make_fetcher degrades instead of raising


async def test_make_fetcher_degrades_on_error(monkeypatch):
    def boom(**kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(httpx, "AsyncClient", boom)
    fetcher = email_otp.make_fetcher("rt", 0)
    out = await fetcher("Acme")
    assert "Email lookup failed" in out
    assert email_otp.NOT_FOUND in out


# ---------------------------------------------------------------------------
# token storage (db fixture; skips without postgres)


def test_gmail_token_roundtrip(db):
    from common import repo

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, is_active) VALUES ('g@t.dev', true) RETURNING user_id"
        )
        uid = cur.fetchone()["user_id"]
    db.commit()

    assert repo.get_gmail_token(db, uid) is None
    repo.save_gmail_token(db, uid, "g@t.dev", "rt-1", [email_otp.SCOPE])
    tok = repo.get_gmail_token(db, uid)
    assert tok["refresh_token"] == "rt-1"
    assert tok["gmail_address"] == "g@t.dev"

    # At-rest value must be ciphertext, not the plaintext token.
    with db.cursor() as cur:
        cur.execute("SELECT refresh_token FROM gmail_tokens WHERE user_id = %s", (uid,))
        stored = cur.fetchone()["refresh_token"]
    assert stored != "rt-1" and "rt-1" not in stored

    repo.save_gmail_token(db, uid, "g@t.dev", "rt-2", [email_otp.SCOPE])  # upsert
    assert repo.get_gmail_token(db, uid)["refresh_token"] == "rt-2"


# ---------------------------------------------------------------------------
# loop wiring


def test_email_tool_schema_shape():
    fn = EMAIL_TOOL["function"]
    assert fn["name"] == EMAIL_TOOL_NAME
    assert fn["parameters"]["required"] == ["company"]
    assert set(fn["parameters"]["properties"]) == {"company", "sender_hint"}


def test_email_tool_json_serializable():
    json.dumps(EMAIL_TOOL)


def test_system_prompt_mentions_email_tool():
    from agent.loop import SYSTEM_PROMPT

    assert EMAIL_TOOL_NAME in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# verification links, ranking, host hints (2026-10-01: filters trash portal mail;
# Workday verifies accounts with a link, not a code)


def test_extract_links_keeps_verification_and_drops_footer():
    text = (
        "Click this link to confirm your email address and complete setup for your candidate\n"
        "account https://acme.wd5.myworkdayjobs.com/AcmeCareers/activate/tok123?x=1 The link will expire.\n"
        "Unsubscribe https://acme.com/unsubscribe?u=1  Privacy https://acme.com/privacy\n"
        "Follow us https://www.linkedin.com/company/acme"
    )
    assert email_otp.extract_links(text) == ["https://acme.wd5.myworkdayjobs.com/AcmeCareers/activate/tok123?x=1"]


def test_format_result_gives_links_on_their_own_lines():
    out = email_otp.format_result("Acme <alto@otp.workday.com>", "Verify your candidate account",
                                  "Verify: https://acme.wd5.myworkdayjobs.com/x/activate/abc")
    assert out.startswith("CONFIRMATION LINK: https://acme.wd5.myworkdayjobs.com/x/activate/abc\n")


def test_verification_subject_beats_a_newer_thank_you_from_the_same_sender():
    metas = [
        {"id": "thanks", "from": "Acme <alto@otp.workday.com>", "subject": "Thank you for applying", "internal_ms": 2},
        {"id": "verify", "from": "Acme <alto@otp.workday.com>", "subject": "Verify your candidate account", "internal_ms": 1},
    ]
    assert email_otp.rank_messages(metas, "Acme", None)[0]["id"] == "verify"


@pytest.mark.parametrize(("host", "words"), [
    ("secure7.saashr.com", "saashr"),
    ("https://acme.wd5.myworkdayjobs.com/en-US/careers", "acme myworkdayjobs"),
    ("127.0.0.1:41234", ""),
])
def test_host_words(host, words):
    assert email_otp.host_words(host) == words


async def test_search_covers_read_mail_and_trash(monkeypatch):
    seen_params = {}

    async def fake_get(self, url, params=None, headers=None):
        seen_params.update(params or {})
        return httpx.Response(200, json={}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as client:
        await email_otp._search_once(client, "tok", "Acme", None, 1000, set())
    assert seen_params["includeSpamTrash"] == "true"
    assert "is:unread" not in seen_params["q"] and "after:1000" in seen_params["q"]


# ---------------------------------------------------------------------------
# handoff auto-resume and the open-link guard


def test_handoff_cleared_rules():
    from agent.loop import handoff_cleared

    cap = {"captcha": True, "solved": False, "code": False, "sig": 1}
    assert not handoff_cleared(cap, cap)
    assert handoff_cleared(cap, cap | {"solved": True})  # reCAPTCHA keeps its iframe once ticked
    assert handoff_cleared(cap, cap | {"captcha": False})
    code = {"captcha": False, "solved": False, "code": True, "sig": 1}
    assert not handoff_cleared(code, code | {"sig": 2})  # the code box is still there
    assert handoff_cleared(code, code | {"code": False})
    other = {"captcha": False, "solved": False, "code": False, "sig": 1}
    assert handoff_cleared(other, other | {"sig": 2})
    assert not handoff_cleared(None, other)


def test_open_link_tool_only_exposed_with_the_email_tool():
    from agent.loop import OPEN_LINK_TOOL, OPEN_LINK_TOOL_NAME, build_system_prompt

    assert OPEN_LINK_TOOL["function"]["name"] == OPEN_LINK_TOOL_NAME
    assert OPEN_LINK_TOOL_NAME in build_system_prompt()
