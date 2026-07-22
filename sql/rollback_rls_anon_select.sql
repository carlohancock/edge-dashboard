-- =============================================================================
-- Edge — Rollback: disable RLS and drop anon SELECT policies
-- =============================================================================
-- Run in: Supabase Dashboard → SQL Editor
-- Idempotent: DROP POLICY IF EXISTS and DISABLE ROW LEVEL SECURITY are safe
--             if policies/RLS were never applied or were already removed.
--
-- Reverts sql/enable_rls_anon_select.sql only:
--   • Drops policy `edge_anon_public_select` on each in-scope table.
--   • Disables RLS on each table (returns to Phase 2 pre-RLS state for these
--     tables; anon SELECT GRANTs from the enable script are left in place —
--     Supabase projects typically grant anon SELECT by default anyway).
--
-- Pipeline (`service_role`) behavior unchanged in either direction — service_role
-- bypasses RLS whenever it is enabled; disabling RLS simply removes the frontend
-- read boundary again.
-- =============================================================================

-- players
DROP POLICY IF EXISTS edge_anon_public_select ON public.players;
ALTER TABLE public.players DISABLE ROW LEVEL SECURITY;

-- teams
DROP POLICY IF EXISTS edge_anon_public_select ON public.teams;
ALTER TABLE public.teams DISABLE ROW LEVEL SECURITY;

-- games
DROP POLICY IF EXISTS edge_anon_public_select ON public.games;
ALTER TABLE public.games DISABLE ROW LEVEL SECURITY;

-- edge_scores
DROP POLICY IF EXISTS edge_anon_public_select ON public.edge_scores;
ALTER TABLE public.edge_scores DISABLE ROW LEVEL SECURITY;

-- adp
DROP POLICY IF EXISTS edge_anon_public_select ON public.adp;
ALTER TABLE public.adp DISABLE ROW LEVEL SECURITY;

-- user_roster
DROP POLICY IF EXISTS edge_anon_public_select ON public.user_roster;
ALTER TABLE public.user_roster DISABLE ROW LEVEL SECURITY;

-- trades
DROP POLICY IF EXISTS edge_anon_public_select ON public.trades;
ALTER TABLE public.trades DISABLE ROW LEVEL SECURITY;

-- trade_players
DROP POLICY IF EXISTS edge_anon_public_select ON public.trade_players;
ALTER TABLE public.trade_players DISABLE ROW LEVEL SECURITY;
