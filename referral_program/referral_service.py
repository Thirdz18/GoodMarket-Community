import os
import hashlib
import logging
import string
import random
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

REFERRER_REWARD = 1000.0
REFEREE_REWARD = 500.0

BASE_URL = os.getenv('BASE_URL', 'https://goodmarket.live')


def _get_supabase():
    from supabase_client import get_supabase_client
    return get_supabase_client()


def _safe(fn, fallback=None, op="db operation"):
    from supabase_client import safe_supabase_operation
    return safe_supabase_operation(fn, fallback_result=fallback, operation_name=op)


class ReferralService:
    def is_wallet_verified_via_goodmarket(self, wallet_address: str) -> dict:
        """Return strict GoodMarket attribution decision for one wallet.

        This uses the same strict attribution helper as overview analytics so
        referral + user_data stay consistent.
        """
        supabase = _get_supabase()
        if not supabase:
            return {"verified_via_goodmarket": False, "reason": "no_db"}

        user_row = _safe(
            lambda: supabase.table('user_data')
                .select('first_login,first_seen_unverified,created_at,face_verified_at,verified_after_goodmarket')
                .ilike('wallet_address', wallet_address)
                .limit(1)
                .execute(),
            op="get user_data for strict GoodMarket attribution"
        )
        if not user_row or not user_row.data:
            return {"verified_via_goodmarket": False, "reason": "no_user_row"}

        row = user_row.data[0]
        try:
            from goodmarket_attribution_backfill import is_attributable_to_goodmarket
            decision = is_attributable_to_goodmarket(wallet_address, row)
        except Exception as e:
            logger.warning(f"Attribution helper failed for {wallet_address[:8]}...: {e}")
            return {"verified_via_goodmarket": False, "reason": "helper_error"}

        if decision.get("attributable"):
            _safe(
                lambda: supabase.table('user_data')
                    .update({'verified_after_goodmarket': True, 'face_verified': True})
                    .ilike('wallet_address', wallet_address)
                    .execute(),
                op="sync verified_after_goodmarket true from strict attribution"
            )
            return {"verified_via_goodmarket": True, "reason": decision.get("reason", "attributable")}

        return {"verified_via_goodmarket": False, "reason": decision.get("reason", "not_attributable")}


    def generate_code_for_wallet(self, wallet_address: str) -> str:
        """Generate a deterministic 8-char alphanumeric referral code from the wallet."""
        seed = f"goodmarket-referral-{wallet_address.lower()}"
        digest = hashlib.sha256(seed.encode()).hexdigest()
        chars = string.ascii_uppercase + string.digits
        code = ''.join(chars[int(digest[i:i+2], 16) % len(chars)] for i in range(0, 16, 2))
        return code[:8]

    def _sync_code_to_user_data(self, wallet_address: str, code: str) -> None:
        """Write my_referral_code into user_data if the column is still NULL."""
        supabase = _get_supabase()
        if not supabase:
            return
        try:
            row = _safe(
                lambda: supabase.table('user_data')
                    .select('my_referral_code')
                    .ilike('wallet_address', wallet_address)
                    .limit(1)
                    .execute(),
                op="check my_referral_code in user_data"
            )
            if row and row.data and row.data[0].get('my_referral_code') is None:
                _safe(
                    lambda: supabase.table('user_data')
                        .update({'my_referral_code': code})
                        .ilike('wallet_address', wallet_address)
                        .execute(),
                    op="sync my_referral_code to user_data"
                )
                logger.info(f"✅ Synced my_referral_code={code} to user_data for {wallet_address[:10]}...")
        except Exception as e:
            logger.warning(f"⚠️ Could not sync my_referral_code to user_data: {e}")

    def get_or_create_referral_code(self, wallet_address: str) -> dict:
        """Return existing referral code for wallet or create a new one.
        Also syncs the code into user_data.my_referral_code for fast single-row lookups.
        """
        supabase = _get_supabase()
        if not supabase:
            return {"success": False, "error": "Database not available"}

        existing = _safe(
            lambda: supabase.table('referral_codes').select('*').eq('wallet_address', wallet_address).limit(1).execute(),
            op="get referral code"
        )
        if existing and existing.data:
            row = existing.data[0]
            code = row['referral_code']
            # Ensure user_data is in sync (handles users created before this feature)
            self._sync_code_to_user_data(wallet_address, code)
            return {
                "success": True,
                "referral_code": code,
                "referral_link": f"{BASE_URL}/?ref={code}",
                "total_referrals": row.get('total_referrals', 0),
                "total_earned": row.get('total_earned', 0),
                "created": False
            }

        code = self.generate_code_for_wallet(wallet_address)

        code_check = _safe(
            lambda: supabase.table('referral_codes').select('wallet_address').eq('referral_code', code).limit(1).execute(),
            op="check code uniqueness"
        )
        if code_check and code_check.data:
            extra = ''.join(random.choices(string.ascii_uppercase + string.digits, k=2))
            code = (code[:6] + extra)[:8]

        insert_result = _safe(
            lambda: supabase.table('referral_codes').insert({
                'wallet_address': wallet_address,
                'referral_code': code,
                'total_referrals': 0,
                'total_earned': 0,
                'created_at': datetime.now(timezone.utc).isoformat()
            }).execute(),
            op="create referral code"
        )

        if not insert_result or not insert_result.data:
            return {"success": False, "error": "Failed to create referral code"}

        # Sync the new code into user_data
        self._sync_code_to_user_data(wallet_address, code)

        return {
            "success": True,
            "referral_code": code,
            "referral_link": f"{BASE_URL}/?ref={code}",
            "total_referrals": 0,
            "total_earned": 0,
            "created": True
        }

    def validate_referral_code(self, referral_code: str) -> dict:
        """Validate a referral code and return the referrer's wallet."""
        if not referral_code or len(referral_code) < 4:
            return {"valid": False, "error": "Invalid referral code format"}

        supabase = _get_supabase()
        if not supabase:
            return {"valid": False, "error": "Database not available"}

        result = _safe(
            lambda: supabase.table('referral_codes').select('*').eq('referral_code', referral_code.upper()).limit(1).execute(),
            op="validate referral code"
        )

        if not result or not result.data:
            return {"valid": False, "error": "Referral code not found"}

        row = result.data[0]
        return {
            "valid": True,
            "referral_code": row['referral_code'],
            "referrer_wallet": row['wallet_address'],
            "total_referrals": row.get('total_referrals', 0)
        }

    def record_referral(self, referral_code: str, referee_wallet: str) -> dict:
        """
        Record a new referral. Status is 'pending_face_verification' until the
        referee completes face verification on GoodMarket.

        Validation rules:
        1. Referral code must be valid and map to a real referrer.
        2. Self-referral not allowed.
        3. Referee must have first_seen_unverified set in user_data — meaning they
           connected to GoodMarket BEFORE being face-verified. If first_seen_unverified
           is NULL, they were already verified when they first arrived: reject.
        4. Referee must not already be externally face-verified on GoodDollar (blockchain
           defense-in-depth, cannot be bypassed by a DB-only exploit).
        5. Referee must not already have a referral record in the referrals table (no duplicates).
        """
        supabase = _get_supabase()
        if not supabase:
            return {"success": False, "error": "Database not available"}

        validation = self.validate_referral_code(referral_code)
        if not validation.get('valid'):
            return {"success": False, "error": validation.get('error', 'Invalid code')}

        referrer_wallet = validation['referrer_wallet']

        if referrer_wallet.lower() == referee_wallet.lower():
            return {"success": False, "error": "Cannot use your own referral code"}

        # PRIMARY GUARD: Check first_seen_unverified in user_data.
        # A legitimate referee must have connected to GoodMarket while unverified
        # (first_seen_unverified is set). If it is NULL, the user was already
        # face-verified on GoodDollar when they first visited GoodMarket — not eligible.
        # This check uses the database and cannot be bypassed by blockchain RPC failures.
        ud_check = _safe(
            lambda: supabase.table('user_data')
                .select('first_seen_unverified, face_verified')
                .ilike('wallet_address', referee_wallet)
                .limit(1)
                .execute(),
            op="check referee first_seen_unverified"
        )
        if ud_check and ud_check.data:
            ud_row = ud_check.data[0]
            if ud_row.get('first_seen_unverified') is None:
                logger.info(
                    f"Referral rejected: {referee_wallet[:8]}... has no first_seen_unverified "
                    f"(was already verified when they first joined GoodMarket)"
                )
                return {
                    "success": False,
                    "already_verified": True,
                    "error": "Referral not valid: user was already face-verified before joining GoodMarket"
                }
        # If no user_data row exists yet (very first request, race condition), we allow
        # and rely on the blockchain check below as the fallback guard.

        # SECONDARY GUARD: on-chain verification check (blockchain defense-in-depth).
        # This catches any edge case where user_data row is missing but the user is
        # already verified on-chain.
        try:
            from blockchain import is_identity_verified
            ext_check = is_identity_verified(referee_wallet)
            if ext_check.get('verified', False):
                logger.info(f"Referral rejected: {referee_wallet[:8]}... is already face-verified on GoodDollar (blockchain check)")
                return {
                    "success": False,
                    "already_verified": True,
                    "error": "Referral not valid: user is already face-verified on GoodDollar"
                }
        except Exception as ext_err:
            logger.warning(f"Could not check external verification for {referee_wallet[:8]}...: {ext_err}")

        # Guard: prevent duplicate referral records
        existing = _safe(
            lambda: supabase.table('referrals').select('id,status').eq('referee_wallet', referee_wallet).limit(1).execute(),
            op="check existing referral"
        )
        if existing and existing.data:
            row = existing.data[0]
            return {
                "success": False,
                "already_exists": True,
                "error": f"Wallet already has a referral record (status: {row.get('status', 'unknown')})"
            }

        insert_result = _safe(
            lambda: supabase.table('referrals').insert({
                'referral_code': referral_code.upper(),
                'referrer_wallet': referrer_wallet,
                'referee_wallet': referee_wallet,
                'status': 'pending_face_verification',
                'created_at': datetime.now(timezone.utc).isoformat()
            }).execute(),
            op="insert referral"
        )

        if not insert_result or not insert_result.data:
            return {"success": False, "error": "Failed to record referral"}

        logger.info(f"Referral recorded: code={referral_code} referrer={referrer_wallet[:8]}... referee={referee_wallet[:8]}...")
        return {
            "success": True,
            "referral_id": insert_result.data[0].get('id'),
            "referrer_wallet": referrer_wallet,
            "status": "pending_face_verification"
        }

    def get_pending_face_verification_referral(self, referee_wallet: str) -> dict:
        """Check if a wallet has a pending referral awaiting face verification."""
        supabase = _get_supabase()
        if not supabase:
            return {"found": False}

        result = _safe(
            lambda: supabase.table('referrals')
                .select('*')
                .eq('referee_wallet', referee_wallet)
                .eq('status', 'pending_face_verification')
                .limit(1)
                .execute(),
            op="get pending face verification referral"
        )

        if result and result.data:
            return {"found": True, "referral": result.data[0]}
        return {"found": False}

    def reconcile_pending_referral_with_onchain(self, referee_wallet: str) -> dict:
        """Attempt automatic recovery for stuck pending referrals.

        If strict GoodMarket attribution says this referee is now verified via
        GoodMarket, claim + disburse immediately.
        """
        pending = self.get_pending_face_verification_referral(referee_wallet)
        if not pending.get("found"):
            return {"success": False, "reason": "no_pending_referral"}

        attribution = self.is_wallet_verified_via_goodmarket(referee_wallet)
        if not attribution.get("verified_via_goodmarket"):
            return {"success": False, "reason": attribution.get("reason", "not_verified_via_goodmarket")}

        claimed = self.claim_pending_referral_for_disbursement(referee_wallet)
        if not claimed.get("claimed"):
            return {"success": False, "reason": "claim_not_acquired"}

        row = claimed.get("referral", {})
        referrer_wallet = row.get("referrer_wallet")
        referral_code = row.get("referral_code")
        if not referrer_wallet or not referral_code:
            self.update_referral_status(
                referee_wallet,
                'failed',
                'Missing referrer/referral code while reconciling pending referral'
            )
            return {"success": False, "reason": "missing_referral_data"}

        disb = self.process_referral_disbursement(
            referrer_wallet=referrer_wallet,
            referee_wallet=referee_wallet,
            referral_code=referral_code
        )
        return {"success": bool(disb.get("success")), "reason": "disbursement_attempted", "disbursement": disb}

    def process_pending_face_verification_referrals(self, limit: int = 500) -> dict:
        """Reconcile stuck pending_face_verification referrals in batch."""
        supabase = _get_supabase()
        if not supabase:
            return {"success": False, "error": "Database not available"}

        pending_referrals = _safe(
            lambda: supabase.table('referrals')
                .select('referee_wallet,status')
                .eq('status', 'pending_face_verification')
                .limit(max(1, int(limit)))
                .execute(),
            op="get pending_face_verification referrals for reconciliation"
        )

        reconciled = 0
        still_waiting_fv = 0
        errors = 0
        for row in (pending_referrals.data if pending_referrals and pending_referrals.data else []):
            rw = row.get('referee_wallet')
            if not rw:
                errors += 1
                continue
            rec = self.reconcile_pending_referral_with_onchain(rw)
            if rec.get('success'):
                reconciled += 1
            else:
                still_waiting_fv += 1

        return {
            "success": True,
            "scanned": len(pending_referrals.data if pending_referrals and pending_referrals.data else []),
            "reconciled_pending_face_verification": reconciled,
            "still_waiting_face_verification": still_waiting_fv,
            "errors": errors,
        }

    def claim_pending_referral_for_disbursement(self, referee_wallet: str) -> dict:
        """
        Atomically transition a pending_face_verification referral to 'disbursing'.
        Returns {"claimed": True, "referral": row} if the record was successfully
        claimed by this call, {"claimed": False} otherwise (already claimed or not found).
        This prevents double-disbursement when fv-callback and verify-ubi fire concurrently.
        """
        supabase = _get_supabase()
        if not supabase:
            return {"claimed": False}

        existing = _safe(
            lambda: supabase.table('referrals')
                .select('*')
                .eq('referee_wallet', referee_wallet)
                .eq('status', 'pending_face_verification')
                .limit(1)
                .execute(),
            op="claim pending referral — fetch"
        )

        if not existing or not existing.data:
            return {"claimed": False}

        row = existing.data[0]
        row_id = row.get('id')

        update_result = _safe(
            lambda: supabase.table('referrals')
                .update({'status': 'disbursing'})
                .eq('id', row_id)
                .eq('status', 'pending_face_verification')
                .execute(),
            op="claim pending referral — atomic update to disbursing"
        )

        # Supabase clients/deployments differ in what UPDATE returns:
        # some return count, some return updated rows in data, and some return
        # neither unless a count/returning option is enabled.  Do not treat a
        # missing count as a failed claim because that leaves admin approval
        # stuck with "already being processed" even though the row was moved
        # to disbursing successfully.
        update_count = getattr(update_result, 'count', None)
        update_data = getattr(update_result, 'data', None) or []
        if (update_count is not None and update_count > 0) or update_data:
            logger.info(f"✅ Claimed pending referral id={row_id} for disbursement (referee={referee_wallet[:8]}...)")
            return {"claimed": True, "referral": row}

        # Fallback verification for Supabase responses without count/data.
        # If this request's conditional update succeeded, the row status is now
        # disbursing; allow the disbursement flow to continue instead of
        # returning a false 409 to the admin dashboard.
        verify = _safe(
            lambda: supabase.table('referrals')
                .select('id,status')
                .eq('id', row_id)
                .limit(1)
                .execute(),
            op="claim pending referral — verify disbursing status"
        )
        if verify and verify.data and verify.data[0].get('status') == 'disbursing':
            logger.info(f"✅ Verified pending referral id={row_id} is disbursing (referee={referee_wallet[:8]}...)")
            return {"claimed": True, "referral": row}

        logger.info(f"ℹ️ Referral id={row_id} already claimed by another process for {referee_wallet[:8]}...")
        return {"claimed": False}

    def update_referral_status(self, referee_wallet: str, status: str, error_message: str = None) -> None:
        """Update the status of a referral record."""
        supabase = _get_supabase()
        if not supabase:
            return

        update_data = {'status': status}
        if status == 'completed':
            update_data['completed_at'] = datetime.now(timezone.utc).isoformat()
        if error_message:
            update_data['error_message'] = error_message

        _safe(
            lambda: supabase.table('referrals').update(update_data).ilike('referee_wallet', referee_wallet).execute(),
            op="update referral status"
        )

        # A completed referral means the referee verified via GoodMarket — mark them accordingly
        if status == 'completed':
            _safe(
                lambda: supabase.table('user_data').update({
                    'verified_after_goodmarket': True
                }).ilike('wallet_address', referee_wallet).execute(),
                op="set verified_after_goodmarket for completed referral"
            )

    def _get_referral_id(self, referral_code: str, referee_wallet: str = None, referrer_wallet: str = None):
        """Return the exact referrals.id for a reward log when available."""
        supabase = _get_supabase()
        if not supabase:
            return None
        def _query():
            query = supabase.table('referrals').select('id').eq('referral_code', referral_code)
            if referee_wallet:
                query = query.ilike('referee_wallet', referee_wallet)
            if referrer_wallet:
                query = query.ilike('referrer_wallet', referrer_wallet)
            return query.limit(1).execute()
        result = _safe(_query, op="get referral id for reward log")
        if result and result.data:
            return result.data[0].get('id')
        return None

    def log_reward(self, wallet_address: str, amount: float, reward_type: str,
                   referral_code: str, tx_hash: str = None, status: str = 'completed',
                   referral_id: int = None) -> dict:
        """Log a referral reward disbursement. Returns the log entry or existing entry."""
        supabase = _get_supabase()
        if not supabase:
            return {"success": False, "error": "no_db"}

        # Check if a referee reward already exists and was completed successfully.
        # Do not apply this shortcut to referrer rewards: the same referral_code
        # is reused by every person an inviter refers, so code+referrer wallet is
        # not a unique referral identity and skipping here can hide real payouts.
        existing = None
        if reward_type == 'referee':
            def _existing_completed_query():
                query = supabase.table('referral_rewards_log') \
                    .select('*') \
                    .eq('wallet_address', wallet_address) \
                    .eq('reward_type', reward_type) \
                    .eq('referral_code', referral_code) \
                    .eq('status', 'completed')
                if referral_id is not None:
                    query = query.eq('referral_id', referral_id)
                return query.limit(1).execute()

            existing = _safe(_existing_completed_query, op="check existing completed reward")
            
            if existing and existing.data:
                logger.info(f"Reward already logged for {wallet_address[:8]}... ({reward_type}) - skipping duplicate")
                return {"success": True, "skipped": True, "existing": existing.data[0]}

        # Check if there's a pending reward log entry
        def _pending_reward_query():
            query = supabase.table('referral_rewards_log') \
                .select('id') \
                .eq('wallet_address', wallet_address) \
                .eq('reward_type', reward_type) \
                .eq('referral_code', referral_code) \
                .in_('status', ['pending', 'pending_disbursed', 'pending_face_verification'])
            if referral_id is not None:
                query = query.eq('referral_id', referral_id)
            return query.limit(1).execute()

        pending = _safe(_pending_reward_query, op="check existing pending reward")
        
        data = {
            'wallet_address': wallet_address,
            'reward_amount': amount,
            'reward_type': reward_type,
            'referral_code': referral_code,
            'status': status,
            'created_at': datetime.now(timezone.utc).isoformat()
        }
        if referral_id is not None:
            data['referral_id'] = referral_id
        if tx_hash:
            data['tx_hash'] = tx_hash
        if status == 'completed':
            data['completed_at'] = datetime.now(timezone.utc).isoformat()

        if pending and pending.data:
            # Update existing pending entry instead of creating new one
            _safe(
                lambda: supabase.table('referral_rewards_log')
                    .update(data)
                    .eq('id', pending.data[0]['id'])
                    .execute(),
                op="update existing pending reward"
            )
            logger.info(f"Updated existing pending reward for {wallet_address[:8]}... ({reward_type})")
        else:
            # Insert new entry
            _safe(
                lambda: supabase.table('referral_rewards_log').insert(data).execute(),
                op="log referral reward"
            )
            logger.info(f"Logged new reward for {wallet_address[:8]}... ({reward_type})")
        
        return {"success": True}

    def increment_referrer_stats(self, referrer_wallet: str, amount: float, referral_code: str = None) -> bool:
        """Increment the referrer's total_referrals and total_earned counters.
        
        Returns True if incremented, False if already counted for this referral.
        """
        supabase = _get_supabase()
        if not supabase:
            return False

        # If referral_code provided, check if this referral was already counted
        if referral_code:
            existing_completed = _safe(
                lambda: supabase.table('referral_rewards_log')
                    .select('id')
                    .eq('wallet_address', referrer_wallet)
                    .eq('reward_type', 'referrer')
                    .eq('referral_code', referral_code)
                    .eq('status', 'completed')
                    .limit(1)
                    .execute(),
                op="check if referrer reward already completed for this referral"
            )
            if existing_completed and existing_completed.data:
                logger.info(f"Referrer stats already incremented for {referral_code} - skipping duplicate")
                return False

        existing = _safe(
            lambda: supabase.table('referral_codes').select('total_referrals,total_earned').eq('wallet_address', referrer_wallet).limit(1).execute(),
            op="get referrer stats"
        )
        if existing and existing.data:
            row = existing.data[0]
            _safe(
                lambda: supabase.table('referral_codes').update({
                    'total_referrals': (row.get('total_referrals') or 0) + 1,
                    'total_earned': (row.get('total_earned') or 0) + amount
                }).eq('wallet_address', referrer_wallet).execute(),
                op="update referrer stats"
            )
            logger.info(f"Incremented referrer stats for {referrer_wallet[:8]}... (+1 referral, +{amount} G$)")
            return True
        return False

    def get_referral_stats(self, wallet_address: str) -> dict:
        """Return referral stats for a wallet (as inviter)."""
        supabase = _get_supabase()
        if not supabase:
            return {"success": False, "error": "Database not available"}

        code_result = self.get_or_create_referral_code(wallet_address)
        if not code_result.get('success'):
            return {"success": False, "error": code_result.get('error')}

        code = code_result['referral_code']

        referrals_result = _safe(
            lambda: supabase.table('referrals').select('*').eq('referrer_wallet', wallet_address).order('created_at', desc=True).execute(),
            op="get referrals for wallet"
        )

        rewards_result = _safe(
            lambda: supabase.table('referral_rewards_log').select('*').eq('wallet_address', wallet_address).eq('reward_type', 'referrer').order('created_at', desc=True).execute(),
            op="get referral rewards for wallet"
        )

        referrals = referrals_result.data if referrals_result else []
        rewards = rewards_result.data if rewards_result else []

        total_earned = sum(float(r.get('reward_amount', 0)) for r in rewards if r.get('status') == 'completed')
        pending_count = sum(1 for r in referrals if r.get('status') in ('pending_face_verification', 'pending_disbursed'))
        completed_count = sum(1 for r in referrals if r.get('status') == 'completed')

        return {
            "success": True,
            "referral_code": code,
            "referral_link": f"{BASE_URL}/?ref={code}",
            "total_referrals": len(referrals),
            "completed_referrals": completed_count,
            "pending_referrals": pending_count,
            "total_earned_g": total_earned,
            "referrals": referrals[:10],
            "rewards": rewards[:10]
        }

    def _is_referral_already_disbursed(self, referral_code: str, referrer_wallet: str = None, referee_wallet: str = None, referral_id: int = None) -> dict:
        """Check if a referral has already been successfully disbursed.
        
        Returns dict with:
            - fully_disbursed: bool - True if both referrer and referee rewards were completed
            - referrer_completed: bool
            - referee_completed: bool
            - referrer_tx: str or None
            - referee_tx: str or None
        """
        supabase = _get_supabase()
        if not supabase:
            return {"fully_disbursed": False}
        
        def _completed_rewards_query():
            query = supabase.table('referral_rewards_log') \
                .select('wallet_address, reward_type, status, tx_hash, referral_id') \
                .eq('referral_code', referral_code) \
                .eq('status', 'completed')
            if referral_id is not None:
                query = query.eq('referral_id', referral_id)
            return query.execute()

        rewards = _safe(_completed_rewards_query, op="check if referral already disbursed")
        
        if not rewards or not rewards.data:
            return {"fully_disbursed": False}
        
        referrer_completed = False
        referee_completed = False
        referrer_tx = None
        referee_tx = None
        
        referrer_wallet_l = referrer_wallet.lower() if referrer_wallet else None
        referee_wallet_l = referee_wallet.lower() if referee_wallet else None

        for r in rewards.data:
            reward_wallet = (r.get('wallet_address') or '').lower()
            if r.get('reward_type') == 'referrer':
                # A referral code belongs to the inviter and is reused for every
                # invite.  Therefore a completed referrer reward for the same
                # code+wallet may belong to an older referee.  Only treat it as
                # duplicate evidence when the caller did not provide the current
                # referee context (legacy callers); otherwise never skip the
                # referrer leg solely from referral_rewards_log.
                if not referee_wallet_l and (not referrer_wallet_l or reward_wallet == referrer_wallet_l):
                    referrer_completed = True
                    referrer_tx = r.get('tx_hash')
            elif r.get('reward_type') == 'referee':
                # The referee wallet is unique per referral, so this side can be
                # safely used to detect an already-paid current referral.
                if not referee_wallet_l or reward_wallet == referee_wallet_l:
                    referee_completed = True
                    referee_tx = r.get('tx_hash')
        
        return {
            "fully_disbursed": referrer_completed and referee_completed,
            "referrer_completed": referrer_completed,
            "referee_completed": referee_completed,
            "referrer_tx": referrer_tx,
            "referee_tx": referee_tx
        }

    def process_referral_disbursement(self, referrer_wallet: str, referee_wallet: str,
                                      referral_code: str) -> dict:
        """
        Single source of truth for disbursing a referral's rewards.

        Sends REFERRER_REWARD G$ to the referrer and REFEREE_REWARD G$ to the
        referee, logs both rewards to referral_rewards_log with proper status,
        updates the referrals row to a terminal/queue state, and increments
        the referrer's lifetime stats whenever the referrer was actually paid
        on-chain (independent of the referee outcome).

        Status mapping per side:
            disburse success         -> 'completed'
            insufficient balance     -> 'pending_disbursed'
            other failure            -> 'failed'

        Referrals row final state:
            both completed           -> 'completed'
            any pending_disbursed    -> 'pending_disbursed'
            otherwise                -> 'failed'

        Reliability:
            The full flow is wrapped in try/except. If anything raises before
            a terminal status is written, the referrals row would otherwise be
            stuck in the intermediate 'disbursing' state forever. The except
            branch reverts the row back to 'pending_face_verification' so the
            next /fv-callback (or admin replay) can claim and retry it.
        """
        from referral_program.blockchain import referral_blockchain_service

        logger.info(f"🔔 [DISBURSEMENT START] referral={referral_code}")
        logger.info(f"   referrer={referrer_wallet[:10]}... ({REFERRER_REWARD} G$)")
        logger.info(f"   referee={referee_wallet[:10]}... ({REFEREE_REWARD} G$)")

        referral_id = self._get_referral_id(referral_code, referee_wallet, referrer_wallet)

        # DUPLICATE CHECK: Prevent double disbursement
        existing = self._is_referral_already_disbursed(referral_code, referrer_wallet, referee_wallet, referral_id)
        logger.info(f"   [DUPLICATE CHECK] existing={existing}")

        if existing.get("fully_disbursed"):
            logger.info(f"Referral {referral_code} already fully disbursed - returning existing results")
            return {
                "success": True,
                "already_disbursed": True,
                "referrer_status": "completed",
                "referee_status": "completed",
                "referrer_tx": existing.get("referrer_tx"),
                "referee_tx": existing.get("referee_tx"),
                "message": "Referral already disbursed"
            }
        
        # Track which side already completed to avoid re-sending
        referrer_already_done = existing.get("referrer_completed", False)
        referee_already_done = existing.get("referee_completed", False)
        logger.info(f"   referrer_already_done={referrer_already_done}, referee_already_done={referee_already_done}")

        try:
            # Preflight the combined amount before sending either leg.  Without
            # this guard a REFERRAL_KEY wallet with only 500-999 G$ can pay the
            # referee first and then leave the referrer unpaid.  The two rewards
            # are intentionally handled as an all-or-queue unit unless one side
            # was already completed by an earlier retry.
            remaining_required = 0.0
            if not referrer_already_done:
                remaining_required += REFERRER_REWARD
            if not referee_already_done:
                remaining_required += REFEREE_REWARD

            if remaining_required > 0:
                balance_check = referral_blockchain_service.get_referral_wallet_balance()
                available_balance = float(balance_check.get('balance') or 0)
                if not balance_check.get('success') or available_balance < remaining_required:
                    logger.warning(
                        f"⚠️ REFERRAL_KEY preflight failed for {referral_code}: "
                        f"available={available_balance:.2f} G$, required={remaining_required:.2f} G$. "
                        "Queueing both remaining rewards instead of making a partial payout."
                    )
                    if not referrer_already_done:
                        self.log_reward(referrer_wallet, REFERRER_REWARD, 'referrer',
                                        referral_code, None, 'pending_disbursed', referral_id)
                    if not referee_already_done:
                        self.log_reward(referee_wallet, REFEREE_REWARD, 'referee',
                                        referral_code, None, 'pending_disbursed', referral_id)
                    self.update_referral_status(
                        referee_wallet, 'pending_disbursed',
                        f"Insufficient REFERRAL_KEY balance: {available_balance:.2f} G$ < {remaining_required:.2f} G$"
                    )
                    return {
                        "success": False,
                        "referrer_status": "completed" if referrer_already_done else "pending_disbursed",
                        "referee_status": "completed" if referee_already_done else "pending_disbursed",
                        "referrer_tx": existing.get("referrer_tx"),
                        "referee_tx": existing.get("referee_tx"),
                        "pending": True,
                        "error": "insufficient_balance",
                        "balance_available": available_balance,
                        "balance_required": remaining_required,
                    }

            # IMPORTANT: Process referee FIRST (500 G$), then referrer (1000 G$).
            # Each call waits for its receipt before the next transaction is built,
            # so a fixed 10-15 second UI delay is not required for nonce ordering.

            if referee_already_done:
                logger.info(f"Referee reward for {referral_code} already completed - skipping blockchain call")
                referee_result = {"success": True, "tx_hash": existing.get("referee_tx"), "skipped": True}
            else:
                logger.info(f"   [REFERE_DISBURSE] sending {REFEREE_REWARD} G$ to {referee_wallet[:10]}...")
                referee_result = referral_blockchain_service.disburse_referral_reward_sync(
                    wallet_address=referee_wallet,
                    amount=REFEREE_REWARD,
                    reward_type='referee'
                )
                logger.info(f"   [REFERE_RESULT] {referee_result}")

            if referrer_already_done:
                logger.info(f"Referrer reward for {referral_code} already completed - skipping blockchain call")
                referrer_result = {"success": True, "tx_hash": existing.get("referrer_tx"), "skipped": True}
            else:
                logger.info(f"   [REFERRER_DISBURSE] sending {REFERRER_REWARD} G$ to {referrer_wallet[:10]}...")
                referrer_result = referral_blockchain_service.disburse_referral_reward_sync(
                    wallet_address=referrer_wallet,
                    amount=REFERRER_REWARD,
                    reward_type='referrer'
                )
                logger.info(f"   [REFERRER_RESULT] {referrer_result}")

            def _status_for(result):
                if result.get('success'):
                    return 'completed'
                if result.get('pending'):
                    return 'pending_disbursed'
                return 'failed'

            referrer_status = _status_for(referrer_result)
            referee_status = _status_for(referee_result)

            self.log_reward(referrer_wallet, REFERRER_REWARD, 'referrer',
                            referral_code, referrer_result.get('tx_hash'), referrer_status, referral_id)
            self.log_reward(referee_wallet, REFEREE_REWARD, 'referee',
                            referral_code, referee_result.get('tx_hash'), referee_status, referral_id)

            if referrer_result.get('success') and referee_result.get('success'):
                self.update_referral_status(referee_wallet, 'completed')
                logger.info(
                    f"✅ Referral rewards disbursed: {referral_code} | "
                    f"referrer={referrer_wallet[:8]}... referee={referee_wallet[:8]}..."
                )
            elif referrer_result.get('pending') or referee_result.get('pending'):
                self.update_referral_status(
                    referee_wallet, 'pending_disbursed',
                    'Insufficient REFERRAL_KEY balance'
                )
                logger.warning(
                    f"⚠️ Referral reward pending disbursement (insufficient balance) "
                    f"for {referral_code} | referrer_status={referrer_status} "
                    f"referee_status={referee_status}"
                )
            else:
                self.update_referral_status(
                    referee_wallet, 'failed',
                    f"Referrer: {referrer_result.get('error', 'unknown')} | "
                    f"Referee: {referee_result.get('error', 'unknown')}"
                )
                logger.error(f"❌ Referral reward disbursement failed for {referral_code}")

            # Whenever the referrer was actually paid on-chain, reflect that in
            # their lifetime stats — even if the referee leg failed. Otherwise
            # the on-chain G$ payment exists with no DB tracking on the inviter.
            # Pass referral_code to prevent duplicate increments
            if referrer_result.get('success') and not referrer_result.get('skipped'):
                self.increment_referrer_stats(referrer_wallet, REFERRER_REWARD, referral_code)

            return {
                "success": referrer_result.get('success') and referee_result.get('success'),
                "referrer_status": referrer_status,
                "referee_status": referee_status,
                "referrer_tx": referrer_result.get('tx_hash'),
                "referee_tx": referee_result.get('tx_hash'),
            }
        except Exception as e:
            # Uncaught exception in the middle of disbursement leaves the row
            # in 'disbursing' forever. Reset to pending_face_verification so a
            # future trigger can retry cleanly.
            logger.error(
                f"❌ Referral disbursement crashed for {referral_code}: {e}",
                exc_info=True
            )
            try:
                self.update_referral_status(
                    referee_wallet, 'pending_face_verification',
                    f"Disbursement crashed and was reset: {e}"
                )
                logger.info(
                    f"↩️ Reset referral {referral_code} to pending_face_verification "
                    f"after disbursement crash so it can be retried."
                )
            except Exception as reset_err:
                logger.error(
                    f"❌ Could not reset referral status after crash for "
                    f"{referral_code}: {reset_err}"
                )
            return {"success": False, "error": str(e)}

    def process_pending_disbursements(self) -> dict:
        """
        Attempt to disburse all pending_disbursed referral rewards.
        Called when admin triggers it or automatically when REFERRAL_KEY is topped up.
        Retries rewards with status 'pending' (awaiting face verification) and 'pending_disbursed' (awaiting balance).
        
        Uses duplicate protection to prevent double disbursement when the same referral
        appears in both referrer and referee pending rewards.
        """
        from referral_program.blockchain import referral_blockchain_service

        supabase = _get_supabase()
        if not supabase:
            return {"success": False, "error": "Database not available"}

        # Fetch both 'pending' (awaiting face verification) and 'pending_disbursed' (awaiting balance) rewards
        pending_rewards = _safe(
            lambda: supabase.table('referral_rewards_log')
                .select('*')
                .in_('status', ['pending', 'pending_disbursed'])
                .order('created_at', desc=False)
                .execute(),
            op="get pending referral rewards"
        )

        if not pending_rewards or not pending_rewards.data:
            reconcile_summary = self.process_pending_face_verification_referrals(limit=500)
            return {
                "success": True,
                "processed": 0,
                "failed": 0,
                "still_pending": 0,
                "message": "No pending rewards",
                "reconciled_pending_face_verification": reconcile_summary.get("reconciled_pending_face_verification", 0),
                "still_waiting_face_verification": reconcile_summary.get("still_waiting_face_verification", 0),
            }

        processed = 0
        failed = 0
        still_pending = 0
        skipped_duplicates = 0
        
        for reward in pending_rewards.data:
            wallet = reward.get('wallet_address')
            amount = float(reward.get('reward_amount', 0))
            reward_type = reward.get('reward_type')
            reward_id = reward.get('id')
            referral_code = reward.get('referral_code')
            
            referral_id = reward.get('referral_id')

            # Check if the OTHER side of this exact referral was already completed
            existing = self._is_referral_already_disbursed(referral_code, referral_id=referral_id)
            
            if existing.get("fully_disbursed"):
                logger.info(f"Referral {referral_code} already fully disbursed - marking this reward as completed")
                _safe(
                    lambda r_id=reward_id: supabase.table('referral_rewards_log').update({
                        'status': 'completed',
                        'completed_at': datetime.now(timezone.utc).isoformat()
                    }).eq('id', r_id).execute(),
                    op="mark already disbursed reward as completed"
                )
                skipped_duplicates += 1
                continue
            
            # Check if this specific side was already completed (but other side wasn't)
            other_side_completed = False
            this_side_already_done = False
            
            if reward_type == 'referrer':
                if existing.get("referrer_completed"):
                    this_side_already_done = True
                if existing.get("referee_completed"):
                    other_side_completed = True
            else:  # referee
                if existing.get("referee_completed"):
                    this_side_already_done = True
                if existing.get("referrer_completed"):
                    other_side_completed = True
            
            if this_side_already_done:
                logger.info(f"{reward_type.capitalize()} reward for {referral_code} already completed - skipping")
                skipped_duplicates += 1
                continue

            # Send blockchain transaction
            result = referral_blockchain_service.disburse_referral_reward(wallet, amount, reward_type)

            if result.get('success'):
                _safe(
                    lambda r_id=reward_id, tx=result.get('tx_hash'): supabase.table('referral_rewards_log').update({
                        'status': 'completed',
                        'tx_hash': tx,
                        'completed_at': datetime.now(timezone.utc).isoformat()
                    }).eq('id', r_id).execute(),
                    op="update pending reward to completed"
                )
                processed += 1
                logger.info(f"Pending referral reward disbursed: {amount} G$ to {wallet[:8]}... TX: {result.get('tx_hash')}")
                
                # Update referrer stats if referrer was paid
                if reward_type == 'referrer':
                    self.increment_referrer_stats(wallet, amount, referral_code)
                
                # If other side is also completed (or was already completed), mark referral as done
                if other_side_completed or existing.get("referrer_completed") or existing.get("referee_completed"):
                    # Both sides should be done now - verify and mark
                    final_check = self._is_referral_already_disbursed(referral_code, referral_id=referral_id)
                    if final_check.get("fully_disbursed"):
                        if referral_id is not None:
                            self.update_referral_status_by_id(referral_id, 'completed')
                        else:
                            self.update_referral_status_by_code(referral_code, 'completed')

            elif result.get('pending'):
                still_pending += 1
                logger.warning(f"Still insufficient balance for {amount} G$ to {wallet[:8]}...")
                break
            else:
                _safe(
                    lambda r_id=reward_id: supabase.table('referral_rewards_log').update({
                        'status': 'failed',
                        'completed_at': datetime.now(timezone.utc).isoformat()
                    }).eq('id', r_id).execute(),
                    op="mark reward as failed"
                )
                failed += 1

        reconcile_summary = self.process_pending_face_verification_referrals(limit=500)

        return {
            "success": True,
            "processed": processed,
            "failed": failed,
            "still_pending": still_pending,
            "skipped_duplicates": skipped_duplicates,
            "reconciled_pending_face_verification": reconcile_summary.get("reconciled_pending_face_verification", 0),
            "still_waiting_face_verification": reconcile_summary.get("still_waiting_face_verification", 0)
        }

    def update_referral_status_by_id(self, referral_id: int, status: str) -> None:
        """Update one exact referral status by primary key."""
        supabase = _get_supabase()
        if not supabase:
            return
        update_data = {'status': status}
        if status == 'completed':
            update_data['completed_at'] = datetime.now(timezone.utc).isoformat()
        _safe(
            lambda: supabase.table('referrals').update(update_data).eq('id', referral_id).execute(),
            op="update referral status by id"
        )

    def update_referral_status_by_code(self, referral_code: str, status: str) -> None:
        """Update referral status by code (used after all rewards disbursed)."""
        supabase = _get_supabase()
        if not supabase:
            return
        update_data = {'status': status}
        if status == 'completed':
            update_data['completed_at'] = datetime.now(timezone.utc).isoformat()
        _safe(
            lambda: supabase.table('referrals').update(update_data).eq('referral_code', referral_code).execute(),
            op="update referral status by code"
        )

    def get_pending_disbursement_summary(self) -> dict:
        """Get summary of pending disbursements waiting for REFERRAL_KEY balance."""
        supabase = _get_supabase()
        if not supabase:
            return {"success": False, "error": "Database not available"}

        pending_result = _safe(
            lambda: supabase.table('referral_rewards_log')
                .select('*')
                .eq('status', 'pending_disbursed')
                .order('created_at', desc=False)
                .execute(),
            op="get pending_disbursed rewards"
        )

        if not pending_result or not pending_result.data:
            return {
                "success": True,
                "total_pending": 0,
                "total_amount": 0.0,
                "rewards": []
            }

        rewards = pending_result.data
        total_amount = sum(float(r.get('reward_amount', 0)) for r in rewards)

        return {
            "success": True,
            "total_pending": len(rewards),
            "total_amount": total_amount,
            "rewards": [
                {
                    "wallet": r.get('wallet_address'),
                    "amount": float(r.get('reward_amount', 0)),
                    "type": r.get('reward_type'),
                    "created_at": r.get('created_at'),
                    "status": r.get('status')
                }
                for r in rewards
            ]
        }


    # =========================================================================
    # PHASE 2: Auto-trigger referral after verification/UBI claim
    # =========================================================================

    def verify_and_disburse_referral(self, wallet_address: str, referral_code: str = None) -> dict:
        """
        PHASE 2 FIX: Called after user completes any verification action 
        (FV callback, UBI claim, etc.). Checks if user is now verified and 
        triggers disbursement if needed.

        This ensures INSTANT disbursement when user verifies or claims UBI G$,
        without waiting for the daily reconciliation script.

        Args:
            wallet_address: The wallet address of the user who just verified/claimed
            referral_code: Optional - the referral code used. If not provided,
                          will check for pending referrals for this wallet.

        Returns:
            dict with success status and details
        """
        if not referral_code:
            # Try to find the referral code from the referral_codes table
            code_result = _safe(
                lambda: _get_supabase().table('referral_codes')
                    .select('referral_code')
                    .eq('wallet_address', wallet_address)
                    .limit(1)
                    .execute(),
                op="get referral code for wallet"
            )
            if code_result and code_result.data:
                # This user is a referrer, not a referee - check if they were referred
                pass
            
            # Check if there's a pending referral where this wallet is the referee
            pending_ref = _safe(
                lambda: _get_supabase().table('referrals')
                    .select('*')
                    .eq('referee_wallet', wallet_address)
                    .in_('status', ['pending_face_verification', 'pending_disbursed'])
                    .limit(1)
                    .execute(),
                op="get pending referral for referee"
            )
            if pending_ref and pending_ref.data:
                referral = pending_ref.data[0]
                referral_code = referral.get('referral_code')
                referrer_wallet = referral.get('referrer_wallet')
            else:
                logger.info(f"No pending referral found for wallet {wallet_address[:8]}...")
                return {"success": False, "reason": "no_pending_referral"}
        else:
            # Get referral details
            ref_result = _safe(
                lambda: _get_supabase().table('referrals')
                    .select('*')
                    .eq('referral_code', referral_code.upper())
                    .eq('referee_wallet', wallet_address)
                    .in_('status', ['pending_face_verification', 'pending_disbursed'])
                    .limit(1)
                    .execute(),
                op="get referral by code and wallet"
            )
            if not ref_result or not ref_result.data:
                logger.info(f"No pending referral found for {referral_code} + {wallet_address[:8]}...")
                return {"success": False, "reason": "no_pending_referral"}
            referral = ref_result.data[0]
            referrer_wallet = referral.get('referrer_wallet')

        logger.info(f"🔔 Triggering referral disbursement for {wallet_address[:8]}... (code: {referral_code})")

        # Step 1: Check verification status - SIMPLE logic
        user = _safe(
            lambda: _get_supabase().table('user_data')
                .select('face_verified, verified_after_goodmarket, face_verified_at')
                .ilike('wallet_address', wallet_address)
                .limit(1)
                .execute(),
            op="get user verification status"
        )
        
        is_verified = False
        verification_reason = "unknown"
        
        if user and user.data:
            row = user.data[0]
            # Simple check: if either flag is true, user is verified
            if row.get('face_verified') == True or row.get('verified_after_goodmarket') == True:
                is_verified = True
                verification_reason = "database_flag"
                logger.info(f"User {wallet_address[:8]} verified via database flag")

        # Step 2: If not verified in DB, try GoodMarket attribution
        if not is_verified:
            attribution = self.is_wallet_verified_via_goodmarket(wallet_address)
            is_verified = attribution.get('verified_via_goodmarket', False)
            verification_reason = attribution.get('reason', 'attribution_failed')
            if is_verified:
                logger.info(f"User {wallet_address[:8]} verified via GoodMarket attribution: {verification_reason}")
            else:
                logger.info(f"User {wallet_address[:8]} NOT verified: {verification_reason}")

        # Step 3: If still not verified, try on-chain check
        if not is_verified:
            try:
                from blockchain import is_identity_verified
                onchain_check = is_identity_verified(wallet_address)
                is_verified = onchain_check.get('verified', False)
                verification_reason = "on_chain_check"
                if is_verified:
                    logger.info(f"User {wallet_address[:8]} verified via on-chain check")
            except Exception as e:
                logger.warning(f"On-chain verification check failed for {wallet_address[:8]}...: {e}")

        # Step 4: If verified, trigger disbursement
        if is_verified:
            logger.info(f"✅ User {wallet_address[:8]} verified! Triggering referral disbursement...")
            
            # Mark user as verified in database if not already
            if verification_reason != "database_flag":
                _safe(
                    lambda: _get_supabase().table('user_data')
                        .update({
                            'verified_after_goodmarket': True,
                            'face_verified': True
                        })
                        .ilike('wallet_address', wallet_address)
                        .execute(),
                    op="mark user as verified"
                )
            
            # Process the disbursement
            result = self.process_referral_disbursement(
                referrer_wallet=referrer_wallet,
                referee_wallet=wallet_address,
                referral_code=referral_code.upper() if referral_code else referral_code
            )
            
            if result.get('success'):
                logger.info(f"🎉 Referral disbursement SUCCESS for {referral_code}!")
            elif result.get('already_disbursed'):
                logger.info(f"ℹ️ Referral {referral_code} was already disbursed")
            else:
                logger.warning(f"⚠️ Referral disbursement returned: {result}")
            
            return {
                "success": result.get('success', False),
                "already_disbursed": result.get('already_disbursed', False),
                "verification_reason": verification_reason,
                "referral_code": referral_code,
                "referrer_wallet": referrer_wallet,
                "referee_wallet": wallet_address,
                "result": result
            }
        else:
            logger.info(f"❌ User {wallet_address[:8]} not verified yet - reason: {verification_reason}")
            return {
                "success": False,
                "reason": "user_not_verified",
                "verification_reason": verification_reason,
                "referral_code": referral_code
            }

    def reconcile_stuck_referrals(self, older_than_hours: int = 1) -> dict:
        """
        PHASE 1 FIX: Reconciliation script to fix stuck pending_face_verification referrals.
        
        This should be called periodically (e.g., via cron job or manual trigger)
        to fix referrals that are stuck despite the user being verified.

        Args:
            older_than_hours: Only process referrals older than this many hours
                              (default: 1 hour to catch actual stuck cases)

        Returns:
            dict with counts of fixed and still stuck referrals
        """
        supabase = _get_supabase()
        if not supabase:
            return {"success": False, "error": "Database not available"}

        from datetime import timedelta
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)

        # Get stuck referrals (pending_face_verification for more than X hours)
        stuck_referrals = _safe(
            lambda: supabase.table('referrals')
                .select('*')
                .eq('status', 'pending_face_verification')
                .lt('created_at', cutoff_time.isoformat())
                .execute(),
            op="get stuck referrals"
        )

        if not stuck_referrals or not stuck_referrals.data:
            return {
                "success": True,
                "fixed": 0,
                "still_stuck": 0,
                "message": "No stuck referrals found"
            }

        fixed = 0
        still_stuck = 0
        errors = 0

        for referral in stuck_referrals.data:
            wallet = referral.get('referee_wallet')
            code = referral.get('referral_code')
            referrer = referral.get('referrer_wallet')

            if not wallet or not code:
                errors += 1
                continue

            logger.info(f"🔧 Reconciling stuck referral: {code} ({wallet[:8]}...)")

            # Try to verify and disburse
            result = self.verify_and_disburse_referral(wallet, code)

            if result.get('success') or result.get('already_disbursed'):
                fixed += 1
                logger.info(f"✅ Fixed stuck referral: {code}")
            else:
                still_stuck += 1
                logger.info(f"⏳ Still stuck: {code} - reason: {result.get('reason')}")

        return {
            "success": True,
            "fixed": fixed,
            "still_stuck": still_stuck,
            "errors": errors,
            "total_checked": len(stuck_referrals.data)
        }


referral_service = ReferralService()
