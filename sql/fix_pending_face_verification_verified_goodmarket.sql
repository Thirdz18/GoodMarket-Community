-- One-by-one manual fix for stuck referrals.
-- Run in Supabase SQL Editor AFTER you verify on-chain + login/claim evidence.
--
-- REQUIRED: edit the 3 values in params CTE before running:
--   1) referral_code
--   2) referee_wallet
--   3) referrer_wallet
--
-- Safety guards:
-- - Updates ONLY one exact referral tuple (code + referee + referrer)
-- - Requires current status = 'pending_face_verification'
-- - Requires user_data shows verified_after_goodmarket=true OR face_verified=true
-- - Returns updated row for audit

begin;

with params as (
    select
        'PUT_REFERRAL_CODE_HERE'::text  as referral_code,
        '0xPUT_REFEREE_WALLET_HERE'::text as referee_wallet,
        '0xPUT_REFERRER_WALLET_HERE'::text as referrer_wallet
), candidate as (
    select r.id, r.referral_code, r.referee_wallet, r.referrer_wallet, r.status
    from referrals r
    join params p
      on r.referral_code = p.referral_code
     and lower(r.referee_wallet) = lower(p.referee_wallet)
     and lower(r.referrer_wallet) = lower(p.referrer_wallet)
    join user_data u
      on lower(u.wallet_address) = lower(r.referee_wallet)
    where r.status = 'pending_face_verification'
      and (
        coalesce(u.verified_after_goodmarket, false) = true
        or coalesce(u.face_verified, false) = true
      )
), updated as (
    update referrals r
       set status = 'completed',
           completed_at = coalesce(r.completed_at, now()),
           error_message = null
      where r.id in (select id from candidate)
    returning r.id, r.referral_code, r.referee_wallet, r.referrer_wallet, r.status, r.completed_at
)
select * from updated;

commit;
