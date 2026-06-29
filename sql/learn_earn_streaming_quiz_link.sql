-- Migration: Add stream_id column to learnearn_log for streaming rewards
-- Purpose: Link quiz attempts to their stream records for proper tracking

-- Add stream_id column to link quiz with its stream
ALTER TABLE IF EXISTS public.learnearn_log
ADD COLUMN IF NOT EXISTS stream_id uuid REFERENCES public.learn_earn_streams(id) ON DELETE SET NULL;

-- Add payout_mode column to track how reward was delivered
ALTER TABLE IF EXISTS public.learnearn_log
ADD COLUMN IF NOT EXISTS payout_mode text DEFAULT 'instant';

-- Add stream_status column for real-time stream tracking
ALTER TABLE IF EXISTS public.learnearn_log
ADD COLUMN IF NOT EXISTS stream_status text;

-- Add stream_started_at and stream_ended_at for timing info
ALTER TABLE IF EXISTS public.learnearn_log
ADD COLUMN IF NOT EXISTS stream_started_at timestamptz;
ALTER TABLE IF EXISTS public.learnearn_log
ADD COLUMN IF NOT EXISTS stream_ended_at timestamptz;

-- Add index for faster lookups by stream_id
CREATE INDEX IF NOT EXISTS idx_learnearn_log_stream_id ON public.learnearn_log(stream_id);

-- Add comment
COMMENT ON COLUMN public.learnearn_log.stream_id IS 'UUID of the stream record in learn_earn_streams table';
COMMENT ON COLUMN public.learnearn_log.payout_mode IS 'How reward was delivered: instant or stream';
COMMENT ON COLUMN public.learnearn_log.stream_status IS 'Current stream status: pending_start, active, stopped, etc.';

-- Migration complete marker
DO $$
BEGIN
    RAISE NOTICE 'Migration completed: Added stream tracking columns to learnearn_log';
END $$;