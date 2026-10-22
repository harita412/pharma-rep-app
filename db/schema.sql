-- =============================================================================
-- pharma_demo, version 2: one database, several separate "spaces"
--
-- A space is one pharma client (demo1 = Mylin Biotech, demo2 = Eldroga, ...).
-- Every doctor, visit and insight carries the space's code (client_code), and the
-- backend always filters on it, so nothing is shared between spaces.
--
-- You normally do NOT run this file by hand: `python seed.py` runs it for you and
-- then loads the sample data. It is safe to run more than once (it never deletes).
--
-- Coming from version 1 (the old tables)? Run db/migrate_v2.sql first, once.
-- =============================================================================

-- One row per space. "theme" picks the colour scheme in the app.
CREATE TABLE IF NOT EXISTS clients (
    code     TEXT PRIMARY KEY CHECK (code ~ '^[a-z0-9_-]{1,32}$'),
    name     TEXT NOT NULL,
    product  TEXT NOT NULL,
    theme    TEXT NOT NULL DEFAULT 'mylin'
);

-- Logins. Passwords are stored only as argon2 hashes, never as text.
CREATE TABLE IF NOT EXISTS users (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username       TEXT NOT NULL UNIQUE CHECK (username = lower(username) AND char_length(username) BETWEEN 3 AND 64),
    password_hash  TEXT NOT NULL,
    client_code    TEXT NOT NULL REFERENCES clients (code) ON DELETE CASCADE,
    -- lock-out counters (5 wrong tries in 15 minutes = locked for 15 minutes)
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    window_start    TIMESTAMPTZ,
    locked_until    TIMESTAMPTZ
);
-- For a users table made earlier without the counters:
ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS window_start TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ;

-- Doctors belong to one space. The ids d1..d4 repeat in every space on purpose,
-- so the primary key is (client_code, id). "profile" holds the doctor's complete
-- record from the prototype (background, concerns, approach, questions, ...).
CREATE TABLE IF NOT EXISTS doctors (
    client_code  TEXT NOT NULL REFERENCES clients (code) ON DELETE CASCADE,
    id           TEXT NOT NULL,
    name         TEXT NOT NULL,
    specialty    TEXT NOT NULL,
    hospital     TEXT NOT NULL,
    city         TEXT NOT NULL DEFAULT '',
    persona      TEXT NOT NULL,
    profile      JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (client_code, id)
);

-- Visits: sample ("seed") visits and the ones reps save.
--   rep_notes   what happened (the rep's notes)
--   outcome     the result label, for example Positive or Follow-Up Required
--   day_offset  seed visits only: days from today (-9 = nine days ago, 2 = in two days),
--               so upcoming and past visits stay correct whatever today's date is
--   extra       seed visits only: the prototype's other fields (agenda, questions, ...)
CREATE TABLE IF NOT EXISTS visits (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_code  TEXT NOT NULL,
    doctor_id    TEXT NOT NULL,
    visit_date   DATE NOT NULL DEFAULT CURRENT_DATE,
    day_offset   INTEGER,
    visit_time   TIME NOT NULL DEFAULT '09:00',
    objective    TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('completed', 'scheduled', 'tentative')),
    rep_notes    TEXT NOT NULL DEFAULT '' CHECK (char_length(rep_notes) <= 5000),
    outcome      TEXT NOT NULL DEFAULT '' CHECK (char_length(outcome) <= 100),
    extra        JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_seed      BOOLEAN NOT NULL DEFAULT false,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (client_code, doctor_id) REFERENCES doctors (client_code, id) ON DELETE CASCADE
);

-- AI insight for a doctor. A new row is added on every refresh, so "what changed" can
-- be compared with the previous one.
CREATE TABLE IF NOT EXISTS insights (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_code      TEXT NOT NULL,
    doctor_id        TEXT NOT NULL,
    next_visit_plan  TEXT NOT NULL,
    talking_points   JSONB NOT NULL DEFAULT '[]'::jsonb,
    interest_score   SMALLINT NOT NULL CHECK (interest_score BETWEEN 0 AND 100),
    what_changed     TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (client_code, doctor_id) REFERENCES doctors (client_code, id) ON DELETE CASCADE
);

-- Insight refreshes per space per day (limit 30). Kept apart from insights so that
-- "Reset demo" does not reset the limit.
CREATE TABLE IF NOT EXISTS insight_refresh_usage (
    client_code    TEXT NOT NULL REFERENCES clients (code) ON DELETE CASCADE,
    usage_date     DATE NOT NULL,
    refresh_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (client_code, usage_date)
);

-- --------------------------------------------------------------- indexes ----
-- (names start with ix_ so they never clash with the version 1 indexes)

CREATE INDEX IF NOT EXISTS ix_users_client_code    ON users    (client_code);
CREATE INDEX IF NOT EXISTS ix_doctors_client_code  ON doctors  (client_code);
CREATE INDEX IF NOT EXISTS ix_visits_client_code   ON visits   (client_code);
CREATE INDEX IF NOT EXISTS ix_visits_doctor        ON visits   (client_code, doctor_id, visit_date DESC);
CREATE INDEX IF NOT EXISTS ix_insights_client_code ON insights (client_code);
CREATE INDEX IF NOT EXISTS ix_insights_doctor      ON insights (client_code, doctor_id, created_at DESC);
-- Lets seed.py run twice without duplicating the sample visits.
CREATE UNIQUE INDEX IF NOT EXISTS ux_visits_seed_key ON visits (client_code, (extra ->> 'key')) WHERE is_seed;
