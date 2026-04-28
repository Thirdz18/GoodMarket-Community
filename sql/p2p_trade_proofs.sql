-- Schema for P2P trade payment proof attachments stored in Supabase Storage.
--
-- Each row represents a single uploaded file (image / PDF) for a given
-- p2p_trades.trade_id. Files themselves live in the private Storage bucket
-- "payment-proofs" — this table stores only the metadata + bucket path.
--
-- Bucket setup (do this in the Supabase dashboard before running this SQL):
--   1. Storage → New bucket → name: "payment-proofs", Public: OFF.
--   2. (Optional) Bucket settings → File size limit: 5 MB.
--      Allowed MIME types: image/png, image/jpeg, image/webp, application/pdf.
--
-- The Flask backend uses the service-role key to upload / list / sign URLs,
-- so we don't define RLS policies here (service role bypasses RLS). If you
-- ever wire client-side Supabase access, add per-row policies that scope
-- access to buyer + seller + arbiter via auth.uid().
--
-- Safe to run multiple times in Supabase SQL Editor.

BEGIN;

CREATE TABLE IF NOT EXISTS public.p2p_trade_proofs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_id        text NOT NULL,
    uploader_wallet text NOT NULL,
    storage_bucket  text NOT NULL DEFAULT 'payment-proofs',
    storage_path    text NOT NULL,
    mime_type       text NOT NULL,
    size_bytes      bigint NOT NULL,
    original_name   text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);

CREATE INDEX IF NOT EXISTS p2p_trade_proofs_trade_id_idx
    ON public.p2p_trade_proofs (trade_id, created_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS p2p_trade_proofs_uploader_idx
    ON public.p2p_trade_proofs (lower(uploader_wallet));

COMMIT;
