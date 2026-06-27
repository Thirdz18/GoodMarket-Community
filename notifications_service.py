import os
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

class NotificationService:
    """
    Unified Notification Service for GoodDollar Analytics Platform

    Handles all notifications from different modules:
    - Hour Bonus notifications
    - Forum reward notifications  
    - Learn & Earn notifications
    - Withdrawal notifications
    """

    def __init__(self):
        self.client = get_supabase_client()
        self.enabled = self.client is not None

        logger.info("🔔 Unified Notification Service initialized")

    def get_all_notifications(self, wallet_address: str, limit: int = 50) -> Dict:
        """Get all notifications for user from all modules"""
        if not self.enabled:
            return {
                'notifications': [],
                'unread_count': 0,
                'total_count': 0
            }

        # Cache notifications for 30 seconds
        cache_key = f'notif_{wallet_address}'
        if hasattr(self, '_cache'):
            if cache_key in self._cache:
                cached_data, cached_time = self._cache[cache_key]
                import time
                if time.time() - cached_time < 30:  # 30 seconds
                    logger.info(f"📦 Using cached notifications for {wallet_address[:8]}...")
                    return cached_data
        else:
            self._cache = {}

        try:
            all_notifications = []

            # 1. Learn & Earn Notifications
            learn_earn_notifications = self._get_learn_earn_notifications(wallet_address, limit)
            all_notifications.extend(learn_earn_notifications)

            # 2. Daily Task Notifications (Twitter & Telegram)
            daily_task_notifications = self._get_daily_task_notifications(wallet_address, limit)
            all_notifications.extend(daily_task_notifications)

            # 4. Minigames Notifications
            minigames_notifications = self._get_minigames_notifications(wallet_address, limit)
            all_notifications.extend(minigames_notifications)

            # 5. Community Stories Notifications
            community_stories_notifications = self._get_community_stories_notifications(wallet_address, limit)
            all_notifications.extend(community_stories_notifications)

            # 6. Admin Broadcast Messages
            admin_broadcasts = self._get_admin_broadcast_notifications(wallet_address, limit)
            all_notifications.extend(admin_broadcasts)

            # Sort by timestamp (newest first)
            all_notifications.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

            # Limit to requested number
            all_notifications = all_notifications[:limit]

            # Get notification IDs that this user has read
            read_notification_ids = self._get_read_notification_ids(wallet_address)

            # Mark notifications as read in the response if user has seen them
            for notif in all_notifications:
                notif['read'] = notif['id'] in read_notification_ids

            # Calculate unread count based on user-specific read status
            unread_count = sum(1 for notif in all_notifications if not notif.get('read', False))

            logger.info(f"🔔 Retrieved {len(all_notifications)} notifications for {wallet_address[:8]}... (Unread: {unread_count})")

            result = {
                'notifications': all_notifications,
                'unread_count': unread_count,
                'total_count': len(all_notifications),
                'success': True
            }

            # Cache the result
            import time
            self._cache[cache_key] = (result, time.time())

            return result

        except Exception as e:
            logger.error(f"❌ Error getting notifications: {e}")
            return {
                'notifications': [],
                'unread_count': 0,
                'total_count': 0,
                'error': str(e),
                'success': False
            }



    def _get_learn_earn_notifications(self, wallet_address: str, limit: int) -> List[Dict]:
        """Get Learn & Earn notifications"""
        try:
            # Get recent quiz completions - use full wallet address
            learn_earn = self.client.table('learnearn_log')\
                .select('*')\
                .eq('wallet_address', wallet_address)\
                .order('timestamp', desc=True)\
                .limit(limit)\
                .execute()

            notifications = []
            for quiz in learn_earn.data or []:
                score = quiz.get('score', 0)
                total = quiz.get('total_questions', 0)
                percentage = (score / total * 100) if total > 0 else 0

                notifications.append({
                    'id': f"learn_earn_{quiz.get('quiz_id', '')}",
                    'type': 'learn_earn',
                    'title': '📚 Quiz Completed',
                    'message': f"Quiz completed! Score: {score}/{total} ({percentage:.0f}%) - Earned {quiz.get('amount_g$', 0)} G$",
                    'amount': quiz.get('amount_g$', 0),
                    'timestamp': quiz.get('timestamp'),
                    'transaction_hash': quiz.get('transaction_hash'),
                    'quiz_id': quiz.get('quiz_id'),
                    'score': score,
                    'total_questions': total,
                    'icon': '📚',
                    'color': '#f59e0b',
                    'module': 'Learn & Earn',
                    'read': False
                })

            return notifications

        except Exception as e:
            logger.error(f"❌ Error getting Learn & Earn notifications: {e}")
            return []

    def _get_daily_task_notifications(self, wallet_address: str, limit: int) -> List[Dict]:
        """Get Daily Task (Twitter & Telegram) notifications"""
        try:
            notifications = []

            # Get Twitter task notifications
            twitter_tasks = self.client.table('twitter_task_log')\
                .select('*')\
                .eq('wallet_address', wallet_address)\
                .order('created_at', desc=True)\
                .limit(limit)\
                .execute()

            for task in twitter_tasks.data or []:
                notifications.append({
                    'id': f"twitter_task_{task.get('id', '')}",
                    'type': 'twitter_task',
                    'title': '🐦 Twitter Task Completed',
                    'message': f"Daily Twitter task completed! Earned {task.get('reward_amount', 0)} G$",
                    'amount': task.get('reward_amount', 0),
                    'timestamp': task.get('created_at'),
                    'transaction_hash': task.get('transaction_hash'),
                    'icon': '🐦',
                    'color': '#1da1f2',
                    'module': 'Daily Task',
                    'read': False
                })

            # Get Telegram task notifications
            telegram_tasks = self.client.table('telegram_task_log')\
                .select('*')\
                .eq('wallet_address', wallet_address)\
                .order('created_at', desc=True)\
                .limit(limit)\
                .execute()

            for task in telegram_tasks.data or []:
                notifications.append({
                    'id': f"telegram_task_{task.get('id', '')}",
                    'type': 'telegram_task',
                    'title': '📱 Telegram Task Completed',
                    'message': f"Daily Telegram task completed! Earned {task.get('reward_amount', 0)} G$",
                    'amount': task.get('reward_amount', 0),
                    'timestamp': task.get('created_at'),
                    'transaction_hash': task.get('transaction_hash'),
                    'icon': '📱',
                    'color': '#0088cc',
                    'module': 'Daily Task',
                    'read': False
                })

            return notifications

        except Exception as e:
            logger.error(f"❌ Error getting Daily Task notifications: {e}")
            return []

    def _get_minigames_notifications(self, wallet_address: str, limit: int) -> List[Dict]:
        """Get Minigames notifications"""
        try:
            # Get recent minigame rewards
            minigame_rewards = self.client.table('minigames_rewards_log')\
                .select('*')\
                .eq('wallet_address', wallet_address)\
                .order('created_at', desc=True)\
                .limit(limit)\
                .execute()

            notifications = []
            for reward in minigame_rewards.data or []:
                notifications.append({
                    'id': f"minigame_{reward.get('id', '')}",
                    'type': 'minigame',
                    'title': '🎮 Minigame Reward',
                    'message': f"Completed {reward.get('game_type', 'game')}! Earned {reward.get('reward_amount', 0)} G$",
                    'amount': reward.get('reward_amount', 0),
                    'timestamp': reward.get('created_at'),
                    'transaction_hash': reward.get('transaction_hash'),
                    'icon': '🎮',
                    'color': '#a855f7',
                    'module': 'Minigames',
                    'read': False
                })

            return notifications

        except Exception as e:
            logger.error(f"❌ Error getting Minigames notifications: {e}")
            return []

    def _get_community_stories_notifications(self, wallet_address: str, limit: int) -> List[Dict]:
        """Get Community Stories notifications"""
        try:
            # Get recent community stories submissions
            stories = self.client.table('community_stories_submissions')\
                .select('*')\
                .eq('wallet_address', wallet_address)\
                .in_('status', ['approved_high', 'approved_low'])\
                .order('reviewed_at', desc=True)\
                .limit(limit)\
                .execute()

            notifications = []
            for story in stories.data or []:
                notifications.append({
                    'id': f"community_story_{story.get('submission_id', '')}",
                    'type': 'community_story',
                    'title': '🌟 Community Story Approved',
                    'message': f"Your community story was approved! Earned {story.get('reward_amount', 0)} G$",
                    'amount': story.get('reward_amount', 0),
                    'timestamp': story.get('reviewed_at'),
                    'transaction_hash': story.get('transaction_hash'),
                    'icon': '🌟',
                    'color': '#fbbf24',
                    'module': 'Community Stories',
                    'read': False
                })

            return notifications

        except Exception as e:
            logger.error(f"❌ Error getting Community Stories notifications: {e}")
            return []

    def _get_admin_broadcast_notifications(self, wallet_address: str, limit: int) -> List[Dict]:
        """Get admin broadcast messages"""
        try:
            # Get recent admin broadcast messages
            broadcasts = self.client.table('admin_broadcast_messages')\
                .select('*')\
                .eq('is_active', True)\
                .order('created_at', desc=True)\
                .limit(limit)\
                .execute()

            notifications = []
            for broadcast in broadcasts.data or []:
                notifications.append({
                    'id': f"admin_broadcast_{broadcast.get('id', '')}",
                    'type': 'admin_broadcast',
                    'title': f"📢 {broadcast.get('title', 'Admin Message')}",
                    'message': broadcast.get('message', ''),
                    'amount': 0,
                    'timestamp': broadcast.get('created_at'),
                    'transaction_hash': None,
                    'icon': '📢',
                    'color': '#ef4444',
                    'module': 'Admin Announcement',
                    'read': False,
                    'broadcast_id': broadcast.get('id')
                })

            return notifications

        except Exception as e:
            logger.error(f"❌ Error getting admin broadcast notifications: {e}")
            return []



    def get_notification_counts(self, wallet_address: str) -> Dict:
        """Get notification counts by type"""
        try:
            all_notifications = self.get_all_notifications(wallet_address, 100)

            counts = {
                'total': len(all_notifications['notifications']),
                'unread': all_notifications['unread_count'],
                'by_type': {}
            }

            # Count by type
            for notification in all_notifications['notifications']:
                notif_type = notification.get('type', 'unknown')
                if notif_type not in counts['by_type']:
                    counts['by_type'][notif_type] = 0
                counts['by_type'][notif_type] += 1

            return counts

        except Exception as e:
            logger.error(f"❌ Error getting notification counts: {e}")
            return {'total': 0, 'unread': 0, 'by_type': {}}

    def check_learn_earn_availability(self, wallet_address: str) -> Dict:
        """Check if Learn & Earn quiz is available for notification"""
        try:
            from learn_and_earn.learn_and_earn import quiz_manager
            from learn_and_earn.blockchain import learn_blockchain_service
            import asyncio

            # Check user eligibility
            eligible = quiz_manager.check_user_eligibility(wallet_address)

            if not eligible:
                return {'available': False, 'reason': 'cooldown_active'}

            # Check Learn wallet balance
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                learn_balance = loop.run_until_complete(learn_blockchain_service.get_learn_wallet_balance())
            finally:
                loop.close()

            min_required_balance = 2000  # 10 questions * 200 G$ per correct answer

            if learn_balance >= min_required_balance:
                return {
                    'available': True,
                    'type': 'learn_earn_available',
                    'title': '📚 Learn & Earn Quiz Ready!',
                    'message': f'Quiz is available! Earn up to {min_required_balance} G$ by answering questions correctly.',
                    'amount': min_required_balance,
                    'action': 'take_quiz',
                    'learn_balance': learn_balance
                }
            else:
                return {
                    'available': False, 
                    'reason': 'insufficient_balance',
                    'learn_balance': learn_balance,
                    'required_balance': min_required_balance
                }

        except Exception as e:
            logger.error(f"❌ Error checking Learn & Earn availability: {e}")
            return {'available': False, 'error': str(e)}

    def get_real_time_notifications(self, wallet_address: str) -> Dict:
        """Get real-time notifications for available features"""
        try:
            available_features = []

            # Check Learn & Earn availability
            learn_earn_status = self.check_learn_earn_availability(wallet_address)
            if learn_earn_status.get('available'):
                available_features.append(learn_earn_status)

            return {
                'success': True,
                'available_features': available_features,
                'total_available': len(available_features),
                'timestamp': datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"❌ Error getting real-time notifications: {e}")
            return {
                'success': False,
                'available_features': [],
                'total_available': 0,
                'error': str(e)
            }

    def mark_notifications_read(self, wallet_address: str, notification_ids: List[str] = None) -> Dict:
        """Mark notifications as read for specific user"""
        if not self.enabled:
            return {'success': False, 'error': 'Notification service disabled'}

        try:
            # Store read status per user in database
            for notif_id in (notification_ids or []):
                try:
                    # Insert or update read status for this user and notification
                    self.client.table('notification_read_status').upsert({
                        'wallet_address': wallet_address,
                        'notification_id': notif_id,
                        'read_at': datetime.now().isoformat(),
                        'is_read': True
                    }).execute()
                except Exception as e:
                    logger.warning(f"⚠️ Failed to mark notification {notif_id} as read: {e}")

            logger.info(f"🔔 Marked {len(notification_ids or [])} notifications as read for {wallet_address[:8]}...")

            # Clear cache for this user
            cache_key = f'notif_{wallet_address}'
            if hasattr(self, '_cache') and cache_key in self._cache:
                del self._cache[cache_key]

            return {
                'success': True,
                'message': 'Notifications marked as read',
                'marked_count': len(notification_ids) if notification_ids else 0
            }

        except Exception as e:
            logger.error(f"❌ Error marking notifications read: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _get_read_notification_ids(self, wallet_address: str) -> set:
        """Get notification IDs that user has already read"""
        try:
            read_status = self.client.table('notification_read_status')\
                .select('notification_id')\
                .eq('wallet_address', wallet_address)\
                .eq('is_read', True)\
                .execute()

            return {item['notification_id'] for item in (read_status.data or [])}
        except Exception as e:
            logger.error(f"❌ Error getting read notifications: {e}")
            return set()

    def create_achievement_sale_notification(self, wallet_address: str, score: int, total_questions: int, sell_price: float, transaction_hash: str):
        """Create a notification for achievement card sale"""
        if not self.enabled:
            return None

        try:
            from datetime import datetime
            
            notification_data = {
                'wallet_address': wallet_address,
                'notification_type': 'achievement_card_sale',
                'title': '💰 Achievement Card Sold',
                'message': f'Your achievement card (Score: {score}/{total_questions}) was sold for {sell_price} G$!',
                'amount': sell_price,
                'transaction_hash': transaction_hash,
                'metadata': {
                    'score': score,
                    'total_questions': total_questions,
                    'sell_price': sell_price,
                    'explorer_url': f"https://explorer.celo.org/mainnet/tx/{transaction_hash}"
                },
                'created_at': datetime.now().isoformat(),
                'is_read': False
            }

            result = self.client.table('achievement_card_notifications').insert(notification_data).execute()
            logger.info(f"✅ Created achievement sale notification for {wallet_address[:8]}...")
            
            # Clear notification cache for this user
            cache_key = f'notif_{wallet_address}'
            if hasattr(self, '_cache') and cache_key in self._cache:
                del self._cache[cache_key]
            
            return result
        except Exception as e:
            logger.error(f"❌ Error creating achievement sale notification: {e}")
            return None

# Global service instance
notification_service = NotificationService()
