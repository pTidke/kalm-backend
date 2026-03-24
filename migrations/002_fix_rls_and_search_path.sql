-- Fix Supabase linter warnings:
-- 1. Restrict RLS policies to service_role only (instead of USING true for all)
-- 2. Set search_path on update_updated_at function

-- ─── Drop overly permissive policies ─────────────────────────
DROP POLICY IF EXISTS "Service role full access on sessions" ON sessions;
DROP POLICY IF EXISTS "Service role full access on messages" ON messages;

-- ─── Restrict access to service_role only ────────────────────
-- The backend uses the service_role key, so only it can read/write.
-- No anonymous or authenticated user can access these tables directly.

CREATE POLICY "Service role only on sessions"
    ON sessions FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

CREATE POLICY "Service role only on messages"
    ON messages FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- ─── Fix mutable search_path on function ─────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;
