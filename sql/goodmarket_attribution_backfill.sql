-- GoodMarket Attribution Backfill — sentinel state table
-- =======================================================
-- Tracks one-shot bulk runs of goodmarket_attribution_backfill.run_full_backfill().
-- The UNIQUE constraint on (run_key) is what makes the auto-run-on-boot path
-- safe across multiple Gunicorn workers / Vercel function instances: only the
-- first worker successfully INSERTs and runs the backfill; everyone else hits
-- the unique violation and skips.
--
-- Idempotent — safe to re-run in the Supabase SQL editor.

CREATE TABLE IF NOT EXISTS public.goodmarket_attribution_backfill_runs (
    id               SERIAL PRIMARY KEY,
    run_key          TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running', 'completed', 'errored')),
    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at     TIMESTAMPTZ,
    wallets_examined INTEGER NOT NULL DEFAULT 0,
    wallets_updated  INTEGER NOT NULL DEFAULT 0,
    errors           INTEGER NOT NULL DEFAULT 0,
    notes            TEXT
);

CREATE INDEX IF NOT EXISTS idx_gm_attr_backfill_runs_started_at
    ON public.goodmarket_attribution_backfill_runs (started_at DESC);

-- RLS: writes only happen via the service-role key from the backend, so we
-- can enable RLS without an anon policy. Reading is admin-only via the
-- protected /api/admin/backfill-gm-attribution endpoint.
ALTER TABLE public.goodmarket_attribution_backfill_runs ENABLE ROW LEVEL SECURITY;

-- ── Optional: force the auto-run to re-execute on next boot ─────────────────
-- If you ever need to re-run the bulk attribution after a schema change or
-- a fix to the on-chain check, either:
--   1. Bump GOODMARKET_ATTRIBUTION_BACKFILL_RUN_KEY in your env vars to a
--      new string (e.g. "auto_v2"), OR
--   2. Run this single line to delete the sentinel:
--        DELETE FROM public.goodmarket_attribution_backfill_runs WHERE run_key = 'auto_v1';
--      Then redeploy / restart the workers.
