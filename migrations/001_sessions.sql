-- Run this in the Supabase SQL Editor to create the sessions + messages tables.

-- Sessions table
CREATE TABLE IF NOT EXISTS sessions (
    id          UUID PRIMARY KEY,
    user_id     TEXT NOT NULL,
    persona_id  TEXT NOT NULL,
    algee_stage INTEGER NOT NULL DEFAULT 0,
    safety_level INTEGER NOT NULL DEFAULT 0,
    turns_in_stage INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Messages table (content encrypted at application level)
CREATE TABLE IF NOT EXISTS messages (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id  UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content_enc TEXT NOT NULL,  -- Fernet-encrypted message content
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions(user_id);

-- Row Level Security
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;

-- Only the service role (backend) can access these tables
CREATE POLICY "Service role full access on sessions"
    ON sessions FOR ALL
    USING (true)
    WITH CHECK (true);

CREATE POLICY "Service role full access on messages"
    ON messages FOR ALL
    USING (true)
    WITH CHECK (true);

-- Auto-update updated_at on sessions
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sessions_updated_at
    BEFORE UPDATE ON sessions
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();
