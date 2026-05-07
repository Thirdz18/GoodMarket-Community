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

        if update_result and update_result.data:
            logger.info(f"✅ Claimed pending referral id={row_id} for disbursement (referee={referee_wallet[:8]}...)")
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

    def log_reward(self, wallet_address: str, amount: float, reward_type: str,
                   referral_code: str, tx_hash: str = None, status: str = 'completed') -> None:
        """Log a referral reward disbursement."""
        supabase = _get_supabase()
        if not supabase:
            return

        data = {
            'wallet_address': wallet_address,
            'reward_amount': amount,
            'reward_type': reward_type,
            'referral_code': referral_code,
            'status': status,
            'created_at': datetime.now(timezone.utc).isoformat()
        }
        if tx_hash:
            data['tx_hash'] = tx_hash
        if status == 'completed':
            data['completed_at'] = datetime.now(timezone.utc).isoformat()

        _safe(
            lambda: supabase.table('referral_rewards_log').insert(data).execute(),
            op="log referral reward"
        )

    def increment_referrer_stats(self, referrer_wallet: str, amount: float) -> None:
        """Increment the referrer's total_referrals and total_earned counters."""
        supabase = _get_supabase()
        if not supabase:
            return

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

        try:
            referrer_result = referral_blockchain_service.disburse_referral_reward_sync(
                wallet_address=referrer_wallet,
                amount=REFERRER_REWARD,
                reward_type='referrer'
            )
            referee_result = referral_blockchain_service.disburse_referral_reward_sync(
                wallet_address=referee_wallet,
                amount=REFEREE_REWARD,
                reward_type='referee'
            )

            def _status_for(result):
                if result.get('success'):
                    return 'completed'
                if result.get('pending'):
                    return 'pending_disbursed'
                return 'failed'

            referrer_status = _status_for(referrer_result)
            referee_status = _status_for(referee_result)

            self.log_reward(referrer_wallet, REFERRER_REWARD, 'referrer',
                            referral_code, referrer_result.get('tx_hash'), referrer_status)
            self.log_reward(referee_wallet, REFEREE_REWARD, 'referee',
                            referral_code, referee_result.get('tx_hash'), referee_status)

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
            if referrer_result.get('success'):
                self.increment_referrer_stats(referrer_wallet, REFERRER_REWARD)

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
            return {"success": True, "processed": 0, "message": "No pending rewards"}

        processed = 0
        failed = 0
        still_pending = 0

        for reward in pending_rewards.data:
            wallet = reward.get('wallet_address')
            amount = float(reward.get('reward_amount', 0))
            reward_type = reward.get('reward_type')
            reward_id = reward.get('id')
            referral_code = reward.get('referral_code')

            result = referral_blockchain_service.disburse_referral_reward(wallet, amount, reward_type)

            if result.get('success'):
                _safe(
                    lambda: supabase.table('referral_rewards_log').update({
                        'status': 'completed',
                        'tx_hash': result.get('tx_hash'),
                        'completed_at': datetime.now(timezone.utc).isoformat()
                    }).eq('id', reward_id).execute(),
                    op="update pending reward to completed"
                )
                processed += 1
                logger.info(f"Pending referral reward disbursed: {amount} G$ to {wallet[:8]}... TX: {result.get('tx_hash')}")

                referee_done = _safe(
                    lambda: supabase.table('referral_rewards_log').select('status').eq('referral_code', referral_code).eq('status', 'pending').execute(),
                    op="check remaining pending for referral"
                )
                remaining = referee_done.data if referee_done else []
                if not remaining:
                    self.update_referral_status_by_code(referral_code, 'completed')

            elif result.get('pending'):
                still_pending += 1
                logger.warning(f"Still insufficient balance for {amount} G$ to {wallet[:8]}...")
                break
            else:
                _safe(
                    lambda: supabase.table('referral_rewards_log').update({
                        'status': 'failed',
                        'completed_at': datetime.now(timezone.utc).isoformat()
                    }).eq('id', reward_id).execute(),
                    op="mark reward as failed"
                )
                failed += 1

        return {
            "success": True,
            "processed": processed,
            "failed": failed,
            "still_pending": still_pending
        }

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


referral_service = ReferralService()
