-- autoapply-v2 canonical schema (root-owned shared infra).
-- Loaded by the postgres container at first boot via /docker-entrypoint-initdb.d/.
-- All file bytes live in MinIO; these tables hold metadata + queue state only.

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS citext;    -- case-insensitive email

-- ============================ identity ============================
CREATE TABLE users (
    user_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email      citext UNIQUE NOT NULL,
    google_sub text UNIQUE,               -- Google Sign-In subject; NULL until first login
    role       text NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    full_name  text,
    phone      text,
    linkedin   text,
    github     text,
    location   text,
    summary    text,                      -- professional summary (resume content)
    is_active  boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- API sessions. Only sha256(token) is stored — a DB dump cannot mint cookies.
CREATE TABLE sessions (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    token_hash text UNIQUE NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);
CREATE INDEX ix_sessions_user ON sessions(user_id);

-- Append-only trail of who did what (written by the API on mutations).
CREATE TABLE audit_log (
    id            bigserial PRIMARY KEY,
    user_id       uuid REFERENCES users(user_id) ON DELETE SET NULL,
    action        text NOT NULL,
    resource_type text,
    resource_id   text,
    detail        jsonb,
    at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_log_user ON audit_log(user_id, at);

-- Per-user portal login (ATS accounts). Plaintext in v1; KMS/encryption deferred.
CREATE TABLE portal_credentials (
    user_id        uuid PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    login_email    text,
    login_password text
);

-- Per-user Gmail OAuth grant (gmail.readonly) — lets the apply agent read
-- verification codes / confirmation links sent to the applicant's inbox.
-- Plaintext refresh token in v1, same posture as portal_credentials.
CREATE TABLE gmail_tokens (
    user_id       uuid PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    gmail_address citext,
    refresh_token text NOT NULL,
    scopes        text[] NOT NULL DEFAULT '{}',
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- ===================== application form data (1:1) =====================
CREATE TABLE application_profiles (
    user_id               uuid PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    -- Forms split the name; users.full_name cannot be split reliably ("Mary Ann
    -- van der Berg"), so the parts are stored as the applicant confirmed them.
    first_name            text,
    middle_name           text,
    last_name             text,
    preferred_name        text,
    pronouns              text,
    phone_country_code    text,          -- "+1"; phone-country selects are separate controls
    portfolio_url         text,
    street_address        text,
    city                  text,
    state                 text,
    zip_code              text,
    country               text,          -- full name ("United States"); the filler maps to ISO codes
    date_of_birth         date,
    work_eligible_us      boolean,
    visa_sponsorship      boolean,
    security_clearance    boolean,
    over_18               boolean,
    available_start_date  date,
    notice_period         text,          -- "2 weeks"; free text because forms ask in days, weeks or dates
    desired_salary        text,
    employment_type       text,
    years_of_experience   integer,
    willing_relocate      boolean,
    willing_travel        boolean,
    travel_percentage     text,
    current_employee      boolean,
    ever_terminated       boolean,
    termination_explanation text,
    gender                text,
    race_ethnicity        text,
    -- Many forms ask Hispanic/Latino as its own yes/no before race (the EEO-1
    -- two-question shape), so it cannot be read out of race_ethnicity.
    hispanic_latino       text,
    veteran_status        text,
    disability_status     text,
    how_did_you_hear      text,          -- default source answer, e.g. "LinkedIn"
    cert_accuracy         boolean,
    cert_dismissal        boolean,
    cert_background_check boolean,
    cert_drug_testing     boolean,
    cert_at_will          boolean,
    cert_job_description   boolean,
    cert_privacy_notice   boolean,
    cert_data_processing  boolean,
    electronic_signature  text,
    signature_date        date,
    updated_at            timestamptz NOT NULL DEFAULT now()
);

-- ===================== resume content (normalized) =====================
CREATE TABLE work_experiences (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    job_title     text,
    company_name  text,
    location      text,
    from_date     date,
    to_date       date,
    is_current    boolean NOT NULL DEFAULT false,
    bullets       text[] NOT NULL DEFAULT '{}',
    display_order integer NOT NULL DEFAULT 0
);
CREATE INDEX ix_work_experiences_user ON work_experiences(user_id);

CREATE TABLE education (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    degree        text,
    major         text,
    school        text,
    from_date     date,
    to_date       date,
    gpa           text,              -- "3.8/4.0" as printed; scales differ, so not numeric
    display_order integer NOT NULL DEFAULT 0
);
CREATE INDEX ix_education_user ON education(user_id);

CREATE TABLE skill_groups (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    category      text,
    skills        text[] NOT NULL DEFAULT '{}',
    display_order integer NOT NULL DEFAULT 0
);
CREATE INDEX ix_skill_groups_user ON skill_groups(user_id);

CREATE TABLE professional_references (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name           text,
    email          text,
    relationship   text,
    known_duration text,
    display_order  integer NOT NULL DEFAULT 0
);
CREATE INDEX ix_professional_references_user ON professional_references(user_id);

-- ===================== jobs (search + manual) =====================
CREATE TABLE jobs (
    job_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source         text NOT NULL DEFAULT 'search'
                     CHECK (source IN ('search', 'manual')),
    url            text NOT NULL,
    url_normalized text NOT NULL UNIQUE,        -- dedupe key
    title          text,
    company        text,
    status         text NOT NULL DEFAULT 'jd_pending'
                     CHECK (status IN ('jd_pending', 'jd_capturing', 'jd_ready', 'failed')),
    jd_s3_key      text,
    jd_captured_at timestamptz,
    posted_at      date,
    error          text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_jobs_status ON jobs(status, created_at);

-- Ownership: jobs stay globally deduped; visibility and tailoring are owners-only.
CREATE TABLE user_jobs (
    user_id  uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    job_id   uuid NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    source   text NOT NULL DEFAULT 'manual' CHECK (source IN ('search', 'manual')),
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, job_id)
);
CREATE INDEX ix_user_jobs_job ON user_jobs(job_id);

-- Brave Search query budget: one row per calendar month, incremented per billed
-- API request by search/usage.py (also created there via IF NOT EXISTS for
-- databases predating this file).
CREATE TABLE IF NOT EXISTS search_api_usage (
    month   date PRIMARY KEY,
    queries int  NOT NULL DEFAULT 0
);

-- ===================== resume files (MinIO artifacts) =====================
CREATE TABLE resume_assets (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('base', 'tailored')),
    variant      text,                            -- file format: 'pdf' / 'docx'
    label        text,                            -- role/track base: 'swe','de',... ; null = default base
    job_id       uuid REFERENCES jobs(job_id) ON DELETE CASCADE,  -- null for base
    s3_key       text NOT NULL,
    content_type text,
    status       text NOT NULL DEFAULT 'ready'
                   CHECK (status IN ('pending', 'ready', 'failed')),
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_resume_assets_user ON resume_assets(user_id);

-- ===================== tailoring queue + index =====================
CREATE TABLE tailored_resumes (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    job_id             uuid NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    base_asset_id      uuid REFERENCES resume_assets(id) ON DELETE SET NULL,
    tailored_asset_id  uuid REFERENCES resume_assets(id) ON DELETE SET NULL,
    status             text NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'tailoring', 'ready', 'failed')),
    resume_score       integer,
    new_resume_score   integer,
    job_match_summary  text,
    cloud              text,
    error              text,
    claimed_by         text,
    claimed_at         timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, job_id)
);
CREATE INDEX ix_tailored_resumes_status ON tailored_resumes(status, created_at);

-- ===================== apply runs (tracking) =====================
CREATE TABLE applications (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    job_id             uuid NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    tailored_resume_id uuid REFERENCES tailored_resumes(id) ON DELETE SET NULL,
    run_id             text,
    status             text,
    outcome            text,
    started_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz
);
CREATE INDEX ix_applications_user ON applications(user_id);

-- ===================== saved form answers =====================
-- Answers to application questions the profile columns do not cover, so a question
-- answered once (by the LLM tier or by hand) fills deterministically next time.
--   reusable: one row per (user, question) — "Years of experience with Spark?"
--   per_job:  tied to one job and never replayed elsewhere — "Why do you want to
--             work at <company>?" must be written fresh for every application.
--             Kept so a `continue` on the same job does not pay for it again.
-- question_key is the normalized question text (lowercased, punctuation and
-- whitespace collapsed); options is the choice list the answer was picked from,
-- NULL for free text, so a lookup can refuse a match whose options differ.
CREATE TABLE saved_answers (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    question_key  text NOT NULL,
    question_text text NOT NULL,
    answer        text NOT NULL,
    options       text[],
    source        text NOT NULL CHECK (source IN ('profile', 'rule', 'llm', 'human')),
    scope         text NOT NULL CHECK (scope IN ('reusable', 'per_job')),
    job_id        uuid REFERENCES jobs(job_id) ON DELETE CASCADE,
    times_used    integer NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CHECK ((scope = 'per_job') = (job_id IS NOT NULL))
);
CREATE UNIQUE INDEX ux_saved_answers_reusable ON saved_answers(user_id, question_key)
    WHERE scope = 'reusable';
CREATE UNIQUE INDEX ux_saved_answers_per_job ON saved_answers(user_id, job_id, question_key)
    WHERE scope = 'per_job';

-- ===================== roles + row-level security =====================
-- Two planes of access:
--   * workers/CLI connect as the container superuser (`admin`) — table owner,
--     bypasses RLS; behavior unchanged.
--   * the API connects as `autoapply_api` and, per request, runs
--       SET LOCAL app.user_id = '<uuid>'
--     inside a transaction. Every policy below filters on that setting, so even
--     a SQL bug in the API cannot read another user's rows.
-- Dev password below; override in prod with: ALTER ROLE autoapply_api PASSWORD '...'.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'autoapply_api') THEN
        CREATE ROLE autoapply_api LOGIN PASSWORD 'autoapply_api_dev';
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO autoapply_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO autoapply_api;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO autoapply_api;

-- Per-user tables: a row is visible/writable only when its user_id matches the
-- request's app.user_id (NULLIF guards the unset/'' cases -> policy is false).
ALTER TABLE users                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE portal_credentials      ENABLE ROW LEVEL SECURITY;
ALTER TABLE gmail_tokens            ENABLE ROW LEVEL SECURITY;
ALTER TABLE application_profiles    ENABLE ROW LEVEL SECURITY;
ALTER TABLE work_experiences        ENABLE ROW LEVEL SECURITY;
ALTER TABLE education               ENABLE ROW LEVEL SECURITY;
ALTER TABLE skill_groups            ENABLE ROW LEVEL SECURITY;
ALTER TABLE professional_references ENABLE ROW LEVEL SECURITY;
ALTER TABLE resume_assets           ENABLE ROW LEVEL SECURITY;
ALTER TABLE tailored_resumes        ENABLE ROW LEVEL SECURITY;
ALTER TABLE applications            ENABLE ROW LEVEL SECURITY;
ALTER TABLE saved_answers           ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_jobs               ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs                    ENABLE ROW LEVEL SECURITY;
-- RLS enabled with NO policies = deny-all for autoapply_api. Session lookup and
-- audit writes happen on the API's small admin-role pool instead.
ALTER TABLE sessions                ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log               ENABLE ROW LEVEL SECURITY;

CREATE POLICY self_row ON users
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY user_isolation ON portal_credentials
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY user_isolation ON gmail_tokens
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY user_isolation ON application_profiles
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY user_isolation ON work_experiences
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY user_isolation ON education
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY user_isolation ON skill_groups
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY user_isolation ON professional_references
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY user_isolation ON resume_assets
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY user_isolation ON tailored_resumes
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY user_isolation ON applications
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY user_isolation ON saved_answers
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY user_isolation ON user_jobs
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

-- jobs: globally deduped rows, visible only through ownership. INSERT is open
-- (add-then-link: ids are deterministic uuid5(url), see common.queue.job_id_for_url,
-- so the caller never needs to read a row it doesn't yet own). No UPDATE/DELETE
-- policy -> denied for the API role; only workers mutate job status.
CREATE POLICY jobs_select_owned ON jobs FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM user_jobs uj
        WHERE uj.job_id = jobs.job_id
          AND uj.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
    ));
CREATE POLICY jobs_insert_any ON jobs FOR INSERT WITH CHECK (true);
