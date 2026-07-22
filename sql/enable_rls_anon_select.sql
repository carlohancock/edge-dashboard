-- =============================================================================
-- Edge — Enable Row-Level Security with public SELECT for anon role
-- =============================================================================
-- Run in: Supabase Dashboard → SQL Editor
-- Idempotent: safe to re-run (ENABLE RLS is no-op if already on; policies
--            recreated via DROP POLICY IF EXISTS + CREATE POLICY).
--
-- WHAT THIS DOES
--   • Enables RLS on every table the Lovable frontend reads (or will read soon).
--   • Adds one SELECT-only policy per table for the `anon` role, USING (true).
--     Fully public read — correct for single-user v1; no per-row restriction.
--
-- WHAT THIS DOES NOT DO
--   (a) No INSERT / UPDATE / DELETE policies for `anon`. The frontend publishable
--       key can read rows that pass the SELECT policy; any write attempt from the
--       client will fail at Postgres (no matching policy → denied).
--   (b) Does not affect the Python pipeline. Requests authenticated with the
--       `service_role` key map to a Postgres role with the BYPASSRLS attribute
--       (Supabase docs: "Service keys can be used to bypass RLS"). Pipeline
--       scripts use SUPABASE_SERVICE_ROLE_KEY via config/supabase_client.py and
--       therefore ignore these policies entirely — zero pipeline code changes.
--
-- PREREQUISITE: tables live in schema `public` (Edge v1 default).
-- =============================================================================

-- Ensure anon can attempt SELECT once a policy allows it (GRANT is idempotent).
GRANT USAGE ON SCHEMA public TO anon;
GRANT SELECT ON public.players TO anon;
GRANT SELECT ON public.teams TO anon;
GRANT SELECT ON public.games TO anon;
GRANT SELECT ON public.edge_scores TO anon;
GRANT SELECT ON public.adp TO anon;
GRANT SELECT ON public.user_roster TO anon;
GRANT SELECT ON public.trades TO anon;
GRANT SELECT ON public.trade_players TO anon;

-- -----------------------------------------------------------------------------
-- players
-- -----------------------------------------------------------------------------
ALTER TABLE public.players ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS edge_anon_public_select ON public.players;
CREATE POLICY edge_anon_public_select
  ON public.players
  FOR SELECT
  TO anon
  USING (true);

-- -----------------------------------------------------------------------------
-- teams
-- -----------------------------------------------------------------------------
ALTER TABLE public.teams ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS edge_anon_public_select ON public.teams;
CREATE POLICY edge_anon_public_select
  ON public.teams
  FOR SELECT
  TO anon
  USING (true);

-- -----------------------------------------------------------------------------
-- games
-- -----------------------------------------------------------------------------
ALTER TABLE public.games ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS edge_anon_public_select ON public.games;
CREATE POLICY edge_anon_public_select
  ON public.games
  FOR SELECT
  TO anon
  USING (true);

-- -----------------------------------------------------------------------------
-- edge_scores
-- -----------------------------------------------------------------------------
ALTER TABLE public.edge_scores ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS edge_anon_public_select ON public.edge_scores;
CREATE POLICY edge_anon_public_select
  ON public.edge_scores
  FOR SELECT
  TO anon
  USING (true);

-- -----------------------------------------------------------------------------
-- adp
-- -----------------------------------------------------------------------------
ALTER TABLE public.adp ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS edge_anon_public_select ON public.adp;
CREATE POLICY edge_anon_public_select
  ON public.adp
  FOR SELECT
  TO anon
  USING (true);

-- -----------------------------------------------------------------------------
-- user_roster (not frontend-read yet; policy now to avoid revisiting later)
-- -----------------------------------------------------------------------------
ALTER TABLE public.user_roster ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS edge_anon_public_select ON public.user_roster;
CREATE POLICY edge_anon_public_select
  ON public.user_roster
  FOR SELECT
  TO anon
  USING (true);

-- -----------------------------------------------------------------------------
-- trades
-- -----------------------------------------------------------------------------
ALTER TABLE public.trades ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS edge_anon_public_select ON public.trades;
CREATE POLICY edge_anon_public_select
  ON public.trades
  FOR SELECT
  TO anon
  USING (true);

-- -----------------------------------------------------------------------------
-- trade_players
-- -----------------------------------------------------------------------------
ALTER TABLE public.trade_players ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS edge_anon_public_select ON public.trade_players;
CREATE POLICY edge_anon_public_select
  ON public.trade_players
  FOR SELECT
  TO anon
  USING (true);

-- -----------------------------------------------------------------------------
-- Verification (optional — comment out if running via API that rejects result sets)
-- SELECT schemaname, tablename, rowsecurity
-- FROM pg_tables
-- WHERE schemaname = 'public'
--   AND tablename IN (
--     'players', 'teams', 'games', 'edge_scores', 'adp',
--     'user_roster', 'trades', 'trade_players'
--   )
-- ORDER BY tablename;
