# api

Multi-user HTTP API for autoapply-v2. Google Sign-In only, cookie sessions,
and Postgres row-level security as defense in depth: user-facing queries run as
the RLS-bound `autoapply_api` role with `SET LOCAL app.user_id` per request, so
even raw SQL cannot cross tenants (policies live in `db/init.sql`).

```sh
cp .env.example .env      # fill GOOGLE_CLIENT_ID/SECRET
uv sync
uv run uvicorn api.app:app --reload
```

Endpoints (v1): `/auth/google/{login,callback}`, `/auth/logout`, `/me`,
`/profile` (GET/PUT, whitelisted fields), `/jobs` (POST/GET),
`/jobs/{id}`, `/jobs/{id}/tailoring`, `/jobs/{id}/resume` (presigned URL),
`/applications`, `/gmail/status`, `/admin/{users,audit}`.

Secrets (`gmail_tokens.refresh_token`, `portal_credentials.login_password`)
have no response-model field anywhere — they cannot serialize.

Tests: `uv run pytest` (needs the root compose Postgres; skips without it).
