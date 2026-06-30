-- autoapply-v2 canonical schema (root-owned shared infra).
-- Loaded by the postgres container at first boot via /docker-entrypoint-initdb.d/.
-- All file bytes live in MinIO; these tables hold metadata + queue state only.

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS citext;    -- case-insensitive email

-- ============================ identity ============================
CREATE TABLE users (
    user_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email      citext UNIQUE NOT NULL,
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

-- Per-user portal login (ATS accounts). Plaintext in v1; KMS/encryption deferred.
CREATE TABLE portal_credentials (
    user_id        uuid PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    login_email    text,
    login_password text
);

-- ===================== application form data (1:1) =====================
CREATE TABLE application_profiles (
    user_id               uuid PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    street_address        text,
    city                  text,
    state                 text,
    zip_code              text,
    date_of_birth         date,
    work_eligible_us      boolean,
    visa_sponsorship      boolean,
    security_clearance    boolean,
    available_start_date  date,
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
    veteran_status        text,
    disability_status     text,
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

-- ===================== resume files (MinIO artifacts) =====================
CREATE TABLE resume_assets (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('base', 'tailored')),
    variant      text,                            -- aws/gcp/azu or null
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
