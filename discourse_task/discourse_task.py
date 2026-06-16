
import logging
from datetime import datetime
from typing import Dict, Any
from supabase_client import get_supabase_client, safe_supabase_operation
from .blockchain import discourse_blockchain_service

logger = logging.getLogger(__name__)

DEFAULT_DISCOURSE_LINK = "https://discourse.gooddollar.org/"
DEFAULT_REWARD_AMOUNT = 500.0

class DiscourseTaskService:
    """Service for managing Discourse Task submissions and rewards"""

    def __init__(self):
        self.supabase = get_supabase_client()
        logger.info("💬 Discourse Task Service initialized")

    # ------------------------------------------------------------------ #
    #  Settings
    # ------------------------------------------------------------------ #

    def get_settings(self) -> Dict[str, Any]:
        """Get current discourse task settings (link + reward)"""
        try:
            if not self.supabase:
                return {"success": True, "link": DEFAULT_DISCOURSE_LINK, "reward_amount": DEFAULT_REWARD_AMOUNT}

            result = safe_supabase_operation(
                lambda: self.supabase.table('discourse_task_settings')
                    .select('*')
                    .order('id', desc=True)
                    .limit(1)
                    .execute(),
                fallback_result=None,
                operation_name="get discourse task settings"
            )

            if result and result.data:
                row = result.data[0]
                return {
                    "success": True,
                    "link": row.get('discourse_link', DEFAULT_DISCOURSE_LINK),
                    "reward_amount": float(row.get('reward_amount', DEFAULT_REWARD_AMOUNT)),
                    "updated_by": row.get('updated_by'),
                    "updated_at": row.get('updated_at')
                }
            else:
                return {"success": True, "link": DEFAULT_DISCOURSE_LINK, "reward_amount": DEFAULT_REWARD_AMOUNT}

        except Exception as e:
            logger.error(f"❌ Error getting discourse settings: {e}")
            return {"success": True, "link": DEFAULT_DISCOURSE_LINK, "reward_amount": DEFAULT_REWARD_AMOUNT}

    def update_settings(self, discourse_link: str, reward_amount: float, admin_wallet: str) -> Dict[str, Any]:
        """Update discourse task settings"""
        try:
            if not discourse_link or not discourse_link.startswith('http'):
                return {"success": False, "error": "Invalid discourse link"}

            if reward_amount < 10 or reward_amount > 100000:
                return {"success": False, "error": "Reward amount must be between 10 and 100,000 G$"}

            if not self.supabase:
                return {"success": False, "error": "Database not available"}

            now = datetime.utcnow().isoformat()

            existing = safe_supabase_operation(
                lambda: self.supabase.table('discourse_task_settings')
                    .select('id')
                    .order('id', desc=True)
                    .limit(1)
                    .execute(),
                fallback_result=None,
                operation_name="check existing discourse settings"
            )

            if existing and existing.data:
                row_id = existing.data[0]['id']
                safe_supabase_operation(
                    lambda: self.supabase.table('discourse_task_settings')
                        .update({
                            'discourse_link': discourse_link,
                            'reward_amount': reward_amount,
                            'updated_by': admin_wallet,
                            'updated_at': now
                        })
                        .eq('id', row_id)
                        .execute(),
                    fallback_result=None,
                    operation_name="update discourse settings"
                )
            else:
                safe_supabase_operation(
                    lambda: self.supabase.table('discourse_task_settings')
                        .insert({
                            'discourse_link': discourse_link,
                            'reward_amount': reward_amount,
                            'updated_by': admin_wallet,
                            'updated_at': now
                        })
                        .execute(),
                    fallback_result=None,
                    operation_name="insert discourse settings"
                )

            logger.info(f"✅ Discourse settings updated by {admin_wallet[:8]}...")
            return {
                "success": True,
                "message": "Settings updated successfully",
                "link": discourse_link,
                "reward_amount": reward_amount
            }

        except Exception as e:
            logger.error(f"❌ Error updating discourse settings: {e}")
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------ #
    #  User Submission
    # ------------------------------------------------------------------ #

    def get_user_status(self, wallet_address: str, current_link: str = None) -> Dict[str, Any]:
        """
        Get the user's submission status for the CURRENT discourse link only.
        If the admin updates the link, this returns status=None so the task is available again.
        """
        try:
            if not self.supabase:
                return {"success": True, "status": None}

            if not current_link:
                settings = self.get_settings()
                current_link = settings.get('link', DEFAULT_DISCOURSE_LINK)

            query = self.supabase.table('discourse_task_log')\
                .select('*')\
                .eq('wallet_address', wallet_address.lower())

            # Filter by current link if the column exists
            try:
                result = safe_supabase_operation(
                    lambda: query
                        .eq('discourse_link', current_link)
                        .order('submitted_at', desc=True)
                        .limit(1)
                        .execute(),
                    fallback_result=None,
                    operation_name="get discourse user status by link"
                )
            except Exception:
                # Fallback: table may not have discourse_link column yet
                result = safe_supabase_operation(
                    lambda: self.supabase.table('discourse_task_log')
                        .select('*')
                        .eq('wallet_address', wallet_address.lower())
                        .order('submitted_at', desc=True)
                        .limit(1)
                        .execute(),
                    fallback_result=None,
                    operation_name="get discourse user status fallback"
                )

            if result and result.data:
                row = result.data[0]
                return {
                    "success": True,
                    "status": row.get('status'),
                    "discourse_username": row.get('discourse_username'),
                    "submitted_at": row.get('submitted_at'),
                    "reward_amount": row.get('reward_amount'),
                    "tx_hash": row.get('tx_hash'),
                    "submission_link": row.get('discourse_link')
                }
            else:
                return {"success": True, "status": None}

        except Exception as e:
            logger.error(f"❌ Error getting discourse user status: {e}")
            return {"success": True, "status": None}

    def submit_username(self, wallet_address: str, discourse_username: str, discourse_link: str = None) -> Dict[str, Any]:
        """Submit a discourse username for approval (linked to the current discourse post)"""
        try:
            if not discourse_username or len(discourse_username.strip()) < 2:
                return {"success": False, "error": "Invalid discourse username"}

            discourse_username = discourse_username.strip()

            settings = self.get_settings()
            reward_amount = settings.get('reward_amount', DEFAULT_REWARD_AMOUNT)
            current_link = discourse_link or settings.get('link', DEFAULT_DISCOURSE_LINK)

            # Check if user already has a pending or approved submission FOR THIS SPECIFIC LINK
            existing = self.get_user_status(wallet_address, current_link)
            if existing.get('status') in ('pending', 'approved'):
                return {
                    "success": False,
                    "error": f"You already have a {existing['status']} submission for this task"
                }

            if not self.supabase:
                return {"success": False, "error": "Database not available"}

            now = datetime.utcnow().isoformat()

            safe_supabase_operation(
                lambda: self.supabase.table('discourse_task_log')
                    .insert({
                        'wallet_address': wallet_address.lower(),
                        'discourse_username': discourse_username,
                        'discourse_link': current_link,
                        'status': 'pending',
                        'submitted_at': now,
                        'reward_amount': reward_amount
                    })
                    .execute(),
                fallback_result=None,
                operation_name="submit discourse username"
            )

            logger.info(f"✅ Discourse submission from {wallet_address[:8]}... username: {discourse_username}")
            return {
                "success": True,
                "message": "Submission received. Waiting for admin approval.",
                "status": "pending"
            }

        except Exception as e:
            logger.error(f"❌ Error submitting discourse username: {e}")
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------ #
    #  Admin Actions
    # ------------------------------------------------------------------ #

    def get_pending_submissions(self) -> Dict[str, Any]:
        """Get all pending discourse task submissions"""
        try:
            if not self.supabase:
                return {"success": True, "submissions": []}

            result = safe_supabase_operation(
                lambda: self.supabase.table('discourse_task_log')
                    .select('*')
                    .eq('status', 'pending')
                    .order('submitted_at', desc=False)
                    .execute(),
                fallback_result=None,
                operation_name="get pending discourse submissions"
            )

            submissions = []
            if result and result.data:
                for row in result.data:
                    submissions.append({
                        'id': row.get('id'),
                        'wallet_address': row.get('wallet_address'),
                        'discourse_username': row.get('discourse_username'),
                        'reward_amount': row.get('reward_amount'),
                        'submitted_at': row.get('submitted_at'),
                        'status': row.get('status')
                    })

            return {"success": True, "submissions": submissions, "total": len(submissions)}

        except Exception as e:
            logger.error(f"❌ Error getting pending discourse submissions: {e}")
            return {"success": False, "submissions": [], "error": str(e)}

    async def approve_submission(self, submission_id: int, admin_wallet: str) -> Dict[str, Any]:
        """Approve a discourse submission and disburse reward"""
        try:
            if not self.supabase:
                return {"success": False, "error": "Database not available"}

            result = safe_supabase_operation(
                lambda: self.supabase.table('discourse_task_log')
                    .select('*')
                    .eq('id', submission_id)
                    .single()
                    .execute(),
                fallback_result=None,
                operation_name="get discourse submission"
            )

            if not result or not result.data:
                return {"success": False, "error": "Submission not found"}

            submission = result.data
            if submission.get('status') != 'pending':
                return {"success": False, "error": f"Submission is already {submission.get('status')}"}

            wallet_address = submission.get('wallet_address')
            reward_amount = float(submission.get('reward_amount', DEFAULT_REWARD_AMOUNT))

            blockchain_result = await discourse_blockchain_service.disburse_discourse_reward(
                wallet_address, reward_amount
            )

            if not blockchain_result.get('success'):
                return {
                    "success": False,
                    "error": f"Blockchain disbursement failed: {blockchain_result.get('error')}"
                }

            tx_hash = blockchain_result.get('tx_hash')
            now = datetime.utcnow().isoformat()

            safe_supabase_operation(
                lambda: self.supabase.table('discourse_task_log')
                    .update({
                        'status': 'approved',
                        'reviewed_at': now,
                        'reviewed_by': admin_wallet,
                        'tx_hash': tx_hash
                    })
                    .eq('id', submission_id)
                    .execute(),
                fallback_result=None,
                operation_name="approve discourse submission"
            )

            logger.info(f"✅ Discourse submission {submission_id} approved. TX: {tx_hash}")
            return {
                "success": True,
                "message": f"Approved and disbursed {reward_amount} G$ to {wallet_address[:8]}...",
                "tx_hash": tx_hash,
                "explorer_url": blockchain_result.get('explorer_url')
            }

        except Exception as e:
            logger.error(f"❌ Error approving discourse submission: {e}")
            return {"success": False, "error": str(e)}

    async def reject_submission(self, submission_id: int, admin_wallet: str, reason: str = '') -> Dict[str, Any]:
        """Reject a discourse submission"""
        try:
            if not self.supabase:
                return {"success": False, "error": "Database not available"}

            result = safe_supabase_operation(
                lambda: self.supabase.table('discourse_task_log')
                    .select('*')
                    .eq('id', submission_id)
                    .single()
                    .execute(),
                fallback_result=None,
                operation_name="get discourse submission for rejection"
            )

            if not result or not result.data:
                return {"success": False, "error": "Submission not found"}

            submission = result.data
            if submission.get('status') != 'pending':
                return {"success": False, "error": f"Submission is already {submission.get('status')}"}

            now = datetime.utcnow().isoformat()

            safe_supabase_operation(
                lambda: self.supabase.table('discourse_task_log')
                    .update({
                        'status': 'rejected',
                        'reviewed_at': now,
                        'reviewed_by': admin_wallet,
                        'rejection_reason': reason
                    })
                    .eq('id', submission_id)
                    .execute(),
                fallback_result=None,
                operation_name="reject discourse submission"
            )

            logger.info(f"❌ Discourse submission {submission_id} rejected by admin")
            return {
                "success": True,
                "message": "Submission rejected successfully"
            }

        except Exception as e:
            logger.error(f"❌ Error rejecting discourse submission: {e}")
            return {"success": False, "error": str(e)}


def init_discourse_task(app):
    """Initialize discourse task module"""
    try:
        logger.info("💬 Initializing Discourse Task module...")
        global discourse_task_service
        discourse_task_service = DiscourseTaskService()
        logger.info("✅ Discourse Task module initialized")
        return True
    except Exception as e:
        logger.error(f"❌ Discourse Task initialization failed: {e}")
        return False

# Global instance
discourse_task_service = DiscourseTaskService()
