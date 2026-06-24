-- =============================================================================
-- P2P Trade Backfill Script
-- Fixes trades stuck at "pending_user_signature" status
-- 
-- Run this in Supabase SQL Editor to fix stuck trades
-- =============================================================================

-- STEP 1: See how many trades need fixing
SELECT 
    COUNT(*) as pending_trades,
    COUNT(*) FILTER (WHERE place_order_tx IS NOT NULL) as with_tx_hash,
    COUNT(*) FILTER (WHERE place_order_tx IS NULL) as missing_tx_hash
FROM public.p2p_trades 
WHERE onchain_status = 'pending_user_signature';

-- STEP 2: For trades WITH place_order_tx but stuck at pending_user_signature
-- Check if the tx exists on-chain and update status accordingly
-- This query shows what would be updated (safe to review first)
SELECT 
    t.trade_id,
    t.buyer_wallet,
    t.seller_wallet,
    t.g_dollar_amount,
    t.place_order_tx,
    t.onchain_status as current_status,
    CASE 
        WHEN t.place_order_tx IS NOT NULL THEN 'payment_pending'
        ELSE 'pending_user_signature'
    END as suggested_status
FROM public.p2p_trades t
WHERE t.onchain_status = 'pending_user_signature'
  AND t.place_order_tx IS NOT NULL;

-- STEP 3: ACTUALLY FIX THEM (trades with tx hash)
-- Update trades that have place_order_tx but stuck at pending_user_signature
UPDATE public.p2p_trades
SET 
    onchain_status = 'payment_pending',
    updated_at = NOW()
WHERE onchain_status = 'pending_user_signature'
  AND place_order_tx IS NOT NULL;

-- STEP 4: For trades WITHOUT tx hash (never got the tx confirmed)
-- These might have failed - mark as cancelled so user can retry
UPDATE public.p2p_trades
SET 
    onchain_status = 'cancelled',
    closed_at = NOW(),
    updated_at = NOW()
WHERE onchain_status = 'pending_user_signature'
  AND place_order_tx IS NULL
  AND created_at < NOW() - INTERVAL '1 hour';  -- Only old stuck trades

-- STEP 5: Verify the fix
SELECT 
    onchain_status,
    COUNT(*) as count
FROM public.p2p_trades 
GROUP BY onchain_status
ORDER BY count DESC;

-- STEP 6: Show remaining stuck trades (for manual review)
SELECT 
    trade_id,
    buyer_wallet,
    seller_wallet,
    g_dollar_amount,
    place_order_tx,
    created_at
FROM public.p2p_trades 
WHERE onchain_status = 'pending_user_signature'
  AND place_order_tx IS NULL;
