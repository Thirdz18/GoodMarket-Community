-- PHASE 1 FIX: SQL Script to fix ALL existing stuck referrals
-- 
-- This script finds all referrals with status 'pending_face_verification' 
-- where the referee is already verified (face_verified=true OR verified_after_goodmarket=true)
-- and marks them as 'completed'.
--
-- ALSO fixes the double-payment issue for referrer:
-- - Checks referral_rewards_log for duplicate entries
-- - Removes duplicate entries that were logged incorrectly
-- - Corrects referral_codes stats for referrers who got double rewards
--
-- RUN THIS IN SUPABASE SQL EDITOR

begin;

-- ============================================================================
-- PART 1: Fix referrals where user is already verified but stuck in pending
-- ============================================================================

select '=== PART 1: Fixing stuck verified referrals ===' as info;

-- Preview: Count of referrals to fix
select 
    count(*) as referrals_to_fix,
    'pending_face_verification referrals with verified users' as description
from referrals r
join user_data u on lower(r.referee_wallet) = lower(u.wallet_address)
where r.status = 'pending_face_verification'
  and (u.face_verified = true or u.verified_after_goodmarket = true);

-- Fix: Update status to completed for verified users
update referrals
set 
    status = 'completed',
    completed_at = coalesce(completed_at, now()),
    error_message = null
where id in (
    select r.id
    from referrals r
    join user_data u on lower(r.referee_wallet) = lower(u.wallet_address)
    where r.status = 'pending_face_verification'
      and (u.face_verified = true or u.verified_after_goodmarket = true)
);

-- Mark affected users as verified (if not already)
update user_data u
set 
    verified_after_goodmarket = true,
    face_verified = coalesce(face_verified, true),
    face_verified_at = coalesce(face_verified_at, now())
where exists (
    select 1 from referrals r
    where lower(r.referee_wallet) = lower(u.wallet_address)
      and r.status = 'completed'
      and r.completed_at = now()
);

-- ============================================================================
-- PART 2: Fix duplicate referral_rewards_log entries
-- ============================================================================

select '=== PART 2: Fixing duplicate reward log entries ===' as info;

-- Find referrals with multiple completed entries for same wallet+type
with duplicates as (
    select 
        referral_code,
        wallet_address,
        reward_type,
        count(*) as cnt,
        array_agg(id) as ids
    from referral_rewards_log
    where status = 'completed'
    group by referral_code, wallet_address, reward_type
    having count(*) > 1
)
select 
    d.referral_code,
    d.wallet_address,
    d.reward_type,
    d.cnt as duplicate_count,
    d.ids as all_ids
from duplicates d;

-- Keep only the FIRST completed entry, delete the rest
with duplicates as (
    select 
        referral_code,
        wallet_address,
        reward_type,
        min(id) as keep_id,
        array_agg(id) as all_ids
    from referral_rewards_log
    where status = 'completed'
    group by referral_code, wallet_address, reward_type
    having count(*) > 1
)
delete from referral_rewards_log
where id in (
    select d.id
    from (
        select unnest(all_ids) as id, keep_id
        from duplicates
    ) d
    where d.id != d.keep_id
);

-- ============================================================================
-- PART 3: Correct referral_codes stats for referrers
-- ============================================================================

select '=== PART 3: Correcting referrer stats ===' as info;

-- The correct count should be the number of completed referrals
-- Update total_referrals based on actual completed referrals
update referral_codes rc
set 
    total_referrals = coalesce(
        (select count(*) 
         from referrals r 
         where lower(r.referrer_wallet) = lower(rc.wallet_address) 
           and r.status = 'completed'
        ), 0
    ),
    total_earned = coalesce(
        (select count(*) 
         from referrals r 
         where lower(r.referrer_wallet) = lower(rc.wallet_address) 
           and r.status = 'completed'
        ), 0
    ) * 1000.0;  -- 1000 G$ per referral

-- ============================================================================
-- PART 4: Summary
-- ============================================================================

select '=== SUMMARY ===' as info;

-- Show final stats
select 
    'Referrals fixed' as metric,
    count(*)::text as value
from referrals
where status = 'completed'
  and completed_at = now();

select 
    'Total completed referrals' as metric,
    count(*)::text as value
from referrals
where status = 'completed';

select 
    'Still pending_face_verification' as metric,
    count(*)::text as value
from referrals
where status = 'pending_face_verification';

-- Show referral_codes stats
select 
    wallet_address,
    total_referrals,
    total_earned
from referral_codes
order by total_earned desc
limit 20;

commit;
