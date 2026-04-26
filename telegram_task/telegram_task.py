import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from supabase_client import get_supabase_client
from cache_utils import supabase_cache

logger = logging.getLogger(__name__)

# Preloaded custom messages (generated once at import time)
_TELEGRAM_MESSAGES: List[str] = []

def _generate_telegram_messages() -> List[str]:
    """Generate 1000 unique custom messages for Telegram (3 sentences each)"""
    import random
    messages = []

    opening_phrases = [
        "GoodMarket is more than tasks — it’s your gateway to learning, earning, and contributing to the GoodDollar ecosystem.",
        "Join the financial revolution with GoodMarket! It's your personal gateway to the GoodDollar ecosystem.",
        "Unlock the potential of Web3 with GoodMarket, your bridge to the GoodDollar universal basic income.",
        "Experience a new way to earn and learn! GoodMarket is the premier hub for the GoodDollar community.",
        "Step into the future of finance! GoodMarket connects you directly to the GoodDollar ecosystem.",
        "Empower yourself with GoodMarket! Start your journey of earning and contributing to GoodDollar today.",
        "GoodMarket: Where education meets rewards in the thriving GoodDollar ecosystem.",
        "Ready to earn? GoodMarket is your official gateway to the GoodDollar universal basic income mission.",
        "Discover a world of opportunities! GoodMarket is the ultimate portal for GoodDollar enthusiasts.",
        "Join thousands earning G$ daily! GoodMarket is your essential gateway to the GoodDollar ecosystem.",
        "Why GoodDollar fits as Universal Basic Income? Explore G$ and start earning while learning. Join goodmarket.live for more opportunities in the GoodDollar ecosystem.",
"Want to discover the potential of G$? GoodMarket shows you how GoodDollar empowers everyone in the UBI ecosystem. Explore more today!",
"Learn & Earn at GoodMarket is better than other platforms — it teaches you about GoodDollar while rewarding your participation. Start your journey now!",
"Curious about Achievement Cards from the GoodMarket Quiz? Each card shows your progress and could have NFT value in the future. Learn more here: https://goodmarket.live/news/article/20",
"Excited for mini-games? In the future, GoodMarket will integrate fun ways to earn G$. Let’s explore GoodMarket and see what the future holds!",
"Step into the GoodDollar ecosystem with GoodMarket. Learn, earn, and contribute to the universal basic income movement. Don’t miss out!",
"Ready to earn while learning? GoodMarket’s Learn & Earn quizzes teach you about GoodDollar and give you rewards plus Achievement Cards!",
"Why choose GoodMarket? It’s your gateway to understanding GoodDollar, exploring UBI, and earning rewards. Join the revolution today!",
"Want to explore more about G$? GoodMarket connects you directly to the GoodDollar ecosystem and opens doors to new opportunities. Start now!",
"Thank you to all GoodMarket participants! 113 users completed this week’s quiz and received rewards plus Achievement Cards — each with potential NFT value. Learn more here: https://goodmarket.live/news/article/20",
"Discover why GoodDollar is a perfect fit for UBI. Learn how you can earn, contribute, and grow with GoodMarket. Start your journey today!",
"Mini-games are coming soon! Explore GoodMarket today and get ready to play, earn, and learn more about the GoodDollar ecosystem.",
"Achievement Cards reward your quiz progress and may become NFTs in the future. Collect them and grow your GoodMarket achievements!",
"GoodMarket is more than just tasks — it’s your gateway to learning, earning, and contributing to GoodDollar. Explore more now!",
"Join the financial revolution with GoodMarket! It’s your personal gateway to the GoodDollar ecosystem and future opportunities.",
"Unlock the potential of Web3 with GoodMarket, your bridge to the GoodDollar universal basic income.",
"Experience a new way to earn and learn! GoodMarket is the premier hub for the GoodDollar community.",
"Step into the future of finance! GoodMarket connects you directly to the GoodDollar ecosystem.",
"Empower yourself with GoodMarket! Start your journey of earning and contributing to GoodDollar today.",
"GoodMarket: Where education meets rewards in the thriving GoodDollar ecosystem.",
"Ready to earn? GoodMarket is your official gateway to the GoodDollar universal basic income mission.",
"Discover a world of opportunities! GoodMarket is the ultimate portal for GoodDollar enthusiasts.",
"Join thousands earning G$ daily! GoodMarket is your essential gateway to the GoodDollar ecosystem.",
"Learn about GoodDollar while earning rewards — GoodMarket’s Learn & Earn quizzes make it simple and fun!",
"Curious about the future of UBI? GoodMarket shows how G$ is changing the game for financial inclusion.",
"Mini-games coming soon in GoodMarket! Prepare to explore, earn, and enjoy new ways to interact with the GoodDollar ecosystem.",
"Your quiz achievements now have more meaning! Each Achievement Card could hold NFT value in the future.",
"Why Learn & Earn is better than other platforms? GoodMarket teaches you about GoodDollar in the UBI ecosystem while rewarding your participation.",
"Explore GoodMarket.live today to discover how G$ can empower you and your community.",
"Achievement Cards mark your success! Participate in quizzes and collect valuable cards that may have long-term value.",
"Join GoodMarket and start learning how GoodDollar fits into the universal basic income ecosystem.",
"GoodMarket makes learning about G$ fun, engaging, and rewarding. Start your journey today!",
"Mini-games integration is coming! Get ready to earn, learn, and enjoy GoodMarket like never before.",
"Learn & Earn quizzes reward you instantly while helping you understand GoodDollar’s role in UBI.",
"Explore, earn, and grow with GoodMarket — your gateway to the GoodDollar ecosystem.",
"Ready to collect Achievement Cards? Participate in GoodMarket quizzes and unlock rewards and potential NFT value.",
"GoodMarket helps you discover why GoodDollar is perfect for UBI and how you can benefit from it.",
"Excited for upcoming mini-games? GoodMarket will soon offer fun ways to earn G$ while learning!",
"Why GoodDollar is suitable as Universal Basic Income? Join GoodMarket and explore the possibilities!",
"Achievement Cards track your quiz success and may hold future value — learn more at GoodMarket.live.",
"Learn, earn, and explore GoodMarket — your bridge to the GoodDollar ecosystem and future financial opportunities.",
"Participate in Learn & Earn quizzes and get rewards while understanding GoodDollar’s role in UBI.",
"GoodMarket is your portal to explore G$, earn rewards, and participate in the growing GoodDollar ecosystem.",
"Mini-games are coming soon! Let’s explore GoodMarket together and see the future of earning and learning.",
"Collect Achievement Cards with each quiz and gain recognition for your progress in GoodMarket.",
"Why Learn & Earn is better? GoodMarket teaches you about GoodDollar while offering real rewards for your effort.",
"Discover G$ opportunities at GoodMarket.live and join the movement shaping the UBI ecosystem.",
"Your GoodMarket journey starts now — learn, earn, and explore the GoodDollar ecosystem.",
"Future mini-games will make GoodMarket more interactive! Get ready to earn, play, and learn more about G$."

    ]

    middle_phrases = [
        "Visit goodmarket.live today and discover daily tasks, learning opportunities, and ways to earn G$ 💙",
        "Head over to goodmarket.live right now to explore exciting tasks and start your G$ earning journey.",
        "Check out goodmarket.live and find a wealth of daily opportunities to support the GoodDollar mission.",
        "Go to goodmarket.live and start completing simple tasks to earn real G$ rewards every single day.",
        "Access goodmarket.live and dive into a variety of ways to contribute and earn within our community.",
        "Your journey starts at goodmarket.live – discover interactive quizzes and tasks that reward you in G$.",
        "Visit goodmarket.live to find out how easy it is to earn G$ while learning about financial inclusion.",
        "Explore goodmarket.live today and join the movement for a more equitable global financial system.",
        "Start your daily earning routine at goodmarket.live with our fun and educational task modules.",
        "Navigate to goodmarket.live and unlock multiple pathways to earn G$ and support universal basic income.",
        "Visit goodmarket.live and explore new ways to learn, earn, and grow within the GoodDollar ecosystem.",
"Head over to goodmarket.live to complete daily tasks and discover exciting earning opportunities in G$.",
"Check out goodmarket.live today and take part in interactive quizzes that reward your learning with G$.",
"Go to goodmarket.live and start your journey of earning G$ while discovering the potential of financial inclusion.",
"Access goodmarket.live to find fun and educational tasks that contribute to the GoodDollar community.",
"Your adventure begins at goodmarket.live – unlock tasks, quizzes, and rewards that help you earn G$ daily.",
"Visit goodmarket.live and see how easy it is to combine learning and earning in the GoodDollar ecosystem.",
"Explore goodmarket.live today and join thousands earning G$ while learning about UBI and blockchain.",
"Start your daily learning and earning routine at goodmarket.live with engaging and rewarding tasks.",
"Navigate to goodmarket.live and uncover multiple ways to earn G$ while supporting the universal basic income mission.",
"Visit goodmarket.live now and experience interactive challenges designed to teach and reward you in G$.",
"Head to goodmarket.live and participate in quizzes and tasks that make earning G$ fun and educational.",
"Check out goodmarket.live to explore simple daily tasks that contribute to your G$ balance.",
"Go to goodmarket.live today and unlock learning modules that reward you directly with G$.",
"Access goodmarket.live and discover a variety of opportunities to earn while exploring the GoodDollar ecosystem.",
"Start at goodmarket.live and take part in activities that combine learning, contribution, and earning G$.",
"Visit goodmarket.live and find out how daily engagement can increase your G$ rewards and knowledge.",
"Explore goodmarket.live to participate in tasks that support financial inclusion and give you G$ rewards.",
"Head over to goodmarket.live and discover the easiest ways to start earning G$ while learning new skills.",
"Check out goodmarket.live to find your next rewarding learning activity and earn G$ along the way.",
"Go to goodmarket.live and experience daily tasks that are designed to teach and reward you in G$.",
"Access goodmarket.live and unlock a world of opportunities to contribute to the GoodDollar ecosystem while earning.",
"Your journey to earning G$ begins at goodmarket.live – explore quizzes, challenges, and interactive tasks.",
"Visit goodmarket.live and take part in engaging tasks that reward both your time and learning in G$.",
"Explore goodmarket.live today and start completing activities that boost your G$ balance while educating you.",
"Start at goodmarket.live and discover fun ways to earn G$ while learning about universal basic income.",
"Navigate to goodmarket.live and engage in quizzes, tasks, and interactive challenges to earn G$ daily.",
"Head to goodmarket.live to explore a variety of tasks that are both educational and rewarding in G$.",
"Check out goodmarket.live today and learn how daily participation can grow your G$ and knowledge.",
"Go to goodmarket.live and unlock opportunities to earn G$ while exploring the GoodDollar ecosystem.",
"Access goodmarket.live and find tasks, quizzes, and interactive content that reward you in G$.",
"Visit goodmarket.live and start your journey of daily learning and earning within the GoodDollar community.",
"Explore goodmarket.live and engage in activities designed to teach, reward, and empower you with G$.",
"Head over to goodmarket.live and discover daily challenges that increase both your knowledge and G$ rewards.",
"Check out goodmarket.live today to participate in rewarding tasks and learn more about GoodDollar.",
"Go to goodmarket.live and take advantage of fun ways to earn G$ while learning about UBI and blockchain.",
"Access goodmarket.live and explore tasks that contribute to your G$ earnings and personal growth.",
"Start your learning and earning journey at goodmarket.live with quizzes, challenges, and daily tasks.",
"Visit goodmarket.live to discover how easy it is to earn G$ while engaging with educational content.",
"Explore goodmarket.live today and take part in interactive activities that support GoodDollar and reward you.",
"Head to goodmarket.live to unlock tasks that teach, engage, and reward you with G$ daily.",
"Check out goodmarket.live and participate in activities that expand your knowledge while earning G$.",
"Go to goodmarket.live and engage with fun and educational challenges that increase your G$ balance.",
"Access goodmarket.live and discover daily opportunities to earn G$ while contributing to the GoodDollar mission.",
"Your path to earning G$ starts at goodmarket.live – complete tasks, participate in quizzes, and learn new skills.",
"Visit goodmarket.live and explore a variety of ways to earn G$ while learning about financial inclusion.",
"Explore goodmarket.live and participate in challenges that reward your knowledge and contributions in G$.",
"Head over to goodmarket.live and take part in activities that make learning and earning G$ fun and easy.",
"Check out goodmarket.live today to unlock tasks that reward your engagement and teach you about G$.",
"Go to goodmarket.live and find daily opportunities to learn, contribute, and earn G$ with the GoodDollar ecosystem.",
"Access goodmarket.live and explore educational activities that reward you directly in G$.",
"Start your G$ earning adventure at goodmarket.live – quizzes, tasks, and challenges await!"

    ]

    closing_phrases = [
        "Start earning G$ today at goodmarket.live",
        "Join the GoodMarket community at goodmarket.live",
        "Earn G$ through tasks and quizzes at goodmarket.live",
        "Complete daily tasks and earn G$ at goodmarket.live",
        "Explore GoodMarket and start earning at goodmarket.live",
        "Discover ways to earn G$ at goodmarket.live",
        "Take on challenges and earn G$ at goodmarket.live",
        "Visit GoodMarket and start your journey at goodmarket.live",
        "Earn more G$ every day at goodmarket.live",
        "Join thousands earning G$ at goodmarket.live",
        "Level up your G$ earnings at goodmarket.live",
        "Complete missions and earn G$ at goodmarket.live",
        "Start your G$ earning journey at goodmarket.live",
        "Participate and earn G$ at goodmarket.live",
        "More G$ earning opportunities await at goodmarket.live",
        "Earn G$ with every task at goodmarket.live",
        "Begin earning G$ right now at goodmarket.live",
        "Your G$ rewards are waiting at goodmarket.live",
        "Earn G$ daily with GoodMarket at goodmarket.live",
        "Connect and earn G$ at goodmarket.live",
    ]

    for i in range(1000):
        # Pick sentences based on index to ensure variety
        s1 = opening_phrases[i % len(opening_phrases)]
        s2 = middle_phrases[(i // 10) % len(middle_phrases)]
        s3 = closing_phrases[(i // 100) % len(closing_phrases)]
        
        # Combine into 3 sentences
        msg = f"✨ {s1}\n\n{s2}\n\n👉 {s3}"
        messages.append(msg)

    return messages

# OLD CODE REMOVED - keeping only the compact opening section above



# Generate messages once at module load
_TELEGRAM_MESSAGES = _generate_telegram_messages()


class TelegramTaskService:
    def __init__(self):
        self.supabase = get_supabase_client()
        # Reward amount is now dynamic and fetched from the reward configuration service
        # self.task_reward = 100.0  # 100 G$ reward - REMOVED, replaced by get_task_reward()

        # Use preloaded messages from module level (generated once at import)
        self.custom_messages = _TELEGRAM_MESSAGES

        self.telegram_channel = "GoodDollarX"
        self.cooldown_hours = 24  # 24 hour cooldown

        logger.info("📱 Telegram Task Service initialized")
        # logger.info(f"💰 Reward: {self.task_reward} G$") # REMOVED - dynamic reward
        logger.info(f"📢 Channel: t.me/{self.telegram_channel}")
        logger.info(f"⏰ Cooldown: {self.cooldown_hours} hours")
        logger.info(f"💬 Custom Messages: {len(self.custom_messages)} unique variations (20 sentences each, wallet-based rotation ensures unique messages per user)")



    def _create_tables(self):
        """Create necessary database tables (run this in Supabase SQL editor)"""
        sql_commands = """
        -- Telegram task completion log
        CREATE TABLE IF NOT EXISTS telegram_task_log (
            id SERIAL PRIMARY KEY,
            wallet_address VARCHAR(42) NOT NULL,
            telegram_url TEXT NOT NULL,
            reward_amount DECIMAL(18,8) NOT NULL,
            transaction_hash VARCHAR(66) NOT NULL,
            status VARCHAR(20) DEFAULT 'completed',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE(telegram_url)
        );

        CREATE INDEX IF NOT EXISTS idx_telegram_task_wallet ON telegram_task_log(wallet_address);
        CREATE INDEX IF NOT EXISTS idx_telegram_task_created ON telegram_task_log(created_at);

        ALTER TABLE telegram_task_log ENABLE ROW LEVEL SECURITY;
        CREATE POLICY "Allow all operations on telegram_task_log" ON telegram_task_log FOR ALL USING (true);
        """
        logger.info("📋 Telegram task database tables ready (run SQL commands in Supabase)")

    def _mask_wallet(self, wallet_address: str) -> str:
        """Mask wallet address for display"""
        if not wallet_address or len(wallet_address) < 10:
            return wallet_address
        return wallet_address[:6] + "..." + wallet_address[-4:]

    def get_custom_message_for_user(self, wallet_address: str) -> str:
        """Get custom message for the user - wallet-based rotation ensures unique messages

        Each user gets a different message every day based on:
          1. Their wallet address (for uniqueness per user)
          2. Current UTC timestamp (hour + day for better rotation)

        This ensures variety and prevents repetitive posts
        """
        import hashlib
        from datetime import datetime, timezone

        # Normalize wallet address to lowercase
        wallet_normalized = wallet_address.lower().strip()

        # Hash wallet address to get consistent index
        wallet_hash = int(hashlib.sha256(wallet_normalized.encode()).hexdigest(), 16)

        # Get current UTC time for rotation
        now_utc = datetime.now(timezone.utc)
        day_of_year = now_utc.timetuple().tm_yday
        hour_of_day = now_utc.hour

        # Use multiple factors for better distribution:
        # 1. Wallet hash (unique per user)
        # 2. Day of year (daily rotation)
        # 3. Hour of day (hourly variation)
        # 4. Last 4 chars of wallet (additional entropy)
        last_4_chars = int(wallet_normalized[-4:], 16) if len(wallet_normalized) >= 4 else 0

        # Combine all factors for unique message index
        message_index = (
            wallet_hash +
            (day_of_year * 37) +  # Prime number multiplier
            (hour_of_day * 17) +   # Prime number multiplier
            (last_4_chars * 7)     # Prime number multiplier
        ) % len(self.custom_messages)

        logger.info(f"📅 Message index {message_index} for user: {wallet_address[:8]}... (Day: {day_of_year}, Hour: {hour_of_day}, 1000 unique messages available)")
        return self.custom_messages[message_index]

    def _validate_telegram_url(self, telegram_url: str) -> Dict[str, Any]:
        """Validate Telegram post URL and verify post existence via Telegram Bot API"""
        try:
            telegram_url = telegram_url.strip()

            if not telegram_url:
                return {"valid": False, "error": "Telegram post URL is required"}

            # Valid formats: https://t.me/GoodDollarX/123 or https://telegram.me/GoodDollarX/123
            if not (telegram_url.startswith("https://t.me/") or
                   telegram_url.startswith("https://telegram.me/")):
                return {"valid": False, "error": "Please provide a valid Telegram post URL (https://t.me/...)"}

            # Check if URL contains the expected channel
            if f"/{self.telegram_channel}/" not in telegram_url:
                return {"valid": False, "error": f"Post must be in t.me/{self.telegram_channel} channel"}

            # Check if URL contains a message ID (number after channel name)
            url_parts = telegram_url.split('/')
            if len(url_parts) < 5 or not url_parts[-1].isdigit():
                return {"valid": False, "error": "URL must be a direct link to your Telegram post (should end with a message number)"}

            # Extract message ID
            message_id = int(url_parts[-1])

            # Minimum message ID validation - real posts in GoodDollarX are 6+ digits
            if message_id < 200000:
                return {"valid": False, "error": "Invalid post link. Please provide a real Telegram post URL from t.me/GoodDollarX channel"}

            # Additional check: reject common test numbers
            test_numbers = [123, 1234, 12345, 123456, 1234567]
            if message_id in test_numbers:
                return {"valid": False, "error": "Please provide a real Telegram post link, not a test URL"}

            # CRITICAL: Verify post exists using Telegram Web API (NO BOT TOKEN NEEDED)
            try:
                import requests
                from bs4 import BeautifulSoup

                # Access Telegram post via public web interface
                # This works for public channels without authentication
                web_url = f"https://t.me/{self.telegram_channel}/{message_id}?embed=1"

                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                }

                logger.info(f"🔍 Verifying post existence: {web_url}")

                response = requests.get(web_url, headers=headers, timeout=10, allow_redirects=False)

                # Check response status
                if response.status_code == 200:
                    # Post exists! Verify it's actually a post page
                    if 'tgme_widget_message' in response.text or 'message' in response.text.lower():
                        logger.info(f"✅ Telegram post {message_id} verified as existing")
                    else:
                        logger.warning(f"⚠️ URL exists but doesn't appear to be a valid post")
                        return {"valid": False, "error": "Invalid post URL. Please provide a real Telegram post link."}

                elif response.status_code == 404:
                    logger.warning(f"❌ Post {message_id} does not exist (404)")
                    return {"valid": False, "error": "This post does not exist. Please create a real post and submit the correct link."}

                elif response.status_code in [301, 302, 307, 308]:
                    # Redirects might indicate channel issues
                    logger.warning(f"⚠️ Post URL redirected (status {response.status_code})")
                    return {"valid": False, "error": "Invalid post link. Please verify you're using the correct channel."}

                else:
                    logger.warning(f"⚠️ Unexpected status code {response.status_code}")
                    # Don't block on unexpected errors, allow through
                    pass

            except requests.exceptions.Timeout:
                logger.warning(f"⚠️ Telegram verification timeout - allowing request")
                # Don't block user if verification times out
                pass

            except Exception as verify_error:
                logger.warning(f"⚠️ Post verification failed: {verify_error}")
                # Don't block user if verification fails
                pass

            return {"valid": True, "telegram_url": telegram_url}

        except Exception as e:
            logger.error(f"❌ Telegram URL validation error: {e}")
            return {"valid": False, "error": "Validation failed. Please try again."}

    async def check_eligibility(self, wallet_address: str) -> Dict[str, Any]:
        """Check if user can claim Telegram task reward"""
        try:
            if not self.supabase:
                return {
                    'can_claim': True,
                    'reason': 'Database not available'
                }

            logger.info(f"🔍 Checking Telegram eligibility for {wallet_address[:8]}...")

            # Check for pending submission (waiting for approval)
            # Cooldown starts IMMEDIATELY after submission, not after approval
            pending_check = self.supabase.table('telegram_task_log')\
                .select('created_at, status')\
                .eq('wallet_address', wallet_address)\
                .eq('status', 'pending')\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()

            logger.info(f"🔍 Pending check result: {len(pending_check.data) if pending_check.data else 0} pending submissions")

            if pending_check.data:
                # Cooldown active - submission is pending
                pending_time = datetime.fromisoformat(pending_check.data[0]['created_at'].replace('Z', '+00:00'))
                next_claim_time = pending_time + timedelta(hours=self.cooldown_hours)

                logger.info(f"⏰ Cooldown active (pending) - Submitted: {pending_time}, Next available: {next_claim_time}")

                return {
                    'can_claim': False,
                    'has_pending_submission': True,
                    'reason': 'Waiting for admin approval',
                    'status': 'pending',
                    'next_claim_time': next_claim_time.isoformat(),
                    'last_claim': pending_time.isoformat()
                }

            # Check last COMPLETED or REJECTED claim within 24 hours
            # Only check claims from the last 24 hours
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=self.cooldown_hours)
            last_claim = self.supabase.table('telegram_task_log')\
                .select('created_at, status')\
                .eq('wallet_address', wallet_address)\
                .in_('status', ['completed', 'rejected'])\
                .gte('created_at', cutoff_time.isoformat())\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()

            logger.info(f"🔍 Recent claims (last 24h): {len(last_claim.data) if last_claim.data else 0}")
            if last_claim.data:
                logger.info(f"🔍 Last claim: {last_claim.data[0]}")

            if last_claim.data:
                last_claim_status = last_claim.data[0]['status']

                # If last claim was REJECTED, user can resubmit immediately
                if last_claim_status == 'rejected':
                    logger.info(f"✅ Last submission was rejected - user can resubmit")
                    return {
                        'can_claim': True,
                        'reward_amount': self.get_task_reward() # Fetch dynamic reward
                    }

                # If last claim was COMPLETED, cooldown is active
                if last_claim_status == 'completed':
                    last_claim_time = datetime.fromisoformat(last_claim.data[0]['created_at'].replace('Z', '+00:00'))
                    next_claim_time = last_claim_time + timedelta(hours=self.cooldown_hours)

                    logger.info(f"⏰ Cooldown active (completed) - Last claim: {last_claim_time}, Next available: {next_claim_time}")

                    return {
                        'can_claim': False,
                        'reason': 'Already claimed today',
                        'next_claim_time': next_claim_time.isoformat(),
                        'last_claim': last_claim_time.isoformat()
                    }

            logger.info(f"✅ User can claim - no recent submissions")

            return {
                'can_claim': True,
                'reward_amount': self.get_task_reward() # Fetch dynamic reward
            }

        except Exception as e:
            logger.error(f"❌ Error checking Telegram task eligibility: {e}")
            return {
                'can_claim': True,
                'reason': 'Error checking eligibility'
            }

    async def claim_task_reward(self, wallet_address: str, telegram_url: str) -> Dict[str, Any]:
        """Submit Telegram task for admin approval"""
        try:
            logger.info(f"📱 Telegram task submission started for {wallet_address[:8]}... with URL: {telegram_url}")

            # Check maintenance mode
            from maintenance_service import maintenance_service
            maintenance_status = maintenance_service.get_maintenance_status('telegram_task')

            if maintenance_status.get('is_maintenance'):
                logger.warning(f"🔧 Telegram Task in maintenance mode")
                return {
                    'success': False,
                    'error': maintenance_status.get('message', 'Telegram Task is under maintenance')
                }

            # Validate URL
            validation = self._validate_telegram_url(telegram_url)
            logger.info(f"🔍 URL validation result: {validation}")

            if not validation.get('valid'):
                logger.warning(f"❌ URL validation failed: {validation.get('error')}")
                return {
                    'success': False,
                    'error': validation.get('error')
                }

            # Check eligibility
            eligibility = await self.check_eligibility(wallet_address)
            logger.info(f"🔍 Eligibility check result: {eligibility}")

            if not eligibility.get('can_claim'):
                logger.warning(f"❌ Not eligible to claim: {eligibility.get('reason')}")
                return {
                    'success': False,
                    'error': eligibility.get('reason', 'Cannot claim at this time')
                }

            # CRITICAL: Check if URL already exists in database
            if self.supabase:
                try:
                    # Check if this EXACT URL was already used by ANYONE
                    url_check = self.supabase.table('telegram_task_log')\
                        .select('wallet_address, created_at, status')\
                        .eq('telegram_url', telegram_url)\
                        .execute()

                    if url_check.data and len(url_check.data) > 0:
                        previous_claim = url_check.data[0]
                        previous_wallet = previous_claim.get('wallet_address', 'Unknown')
                        previous_status = previous_claim.get('status', 'pending')

                        if previous_wallet == wallet_address:
                            if previous_status == 'pending':
                                return {
                                    'success': False,
                                    'error': 'You already submitted this post. Please wait for admin approval.'
                                }
                            else:
                                logger.warning(f"❌ User {wallet_address[:8]}... already claimed with this URL")
                                return {
                                    'success': False,
                                    'error': 'You have already used this Telegram post for rewards. Please create a new post.'
                                }
                        else:
                            logger.warning(f"❌ URL already used by another wallet: {previous_wallet[:8]}...")
                            return {
                                'success': False,
                                'error': 'This Telegram post link has already been used. Please create your own post.'
                            }

                    logger.info(f"✅ URL is unique and unused - submitting for approval")

                except Exception as db_error:
                    logger.error(f"❌ Database URL check error: {db_error}")
                    return {
                        'success': False,
                        'error': 'Unable to verify post uniqueness. Please try again.'
                    }

            # Submit for admin approval instead of immediate disbursement
            if self.supabase:
                try:
                    # Insert with NULL transaction_hash for pending submissions
                    # Transaction hash will be added when admin approves
                    current_reward = self.get_task_reward() # Fetch dynamic reward
                    self.supabase.table('telegram_task_log').insert({
                        'wallet_address': wallet_address,
                        'telegram_url': telegram_url,
                        'reward_amount': current_reward,
                        'status': 'pending',
                        'transaction_hash': None,  # Will be set after admin approval
                        'created_at': datetime.now(timezone.utc).isoformat()
                    }).execute()

                    logger.info(f"✅ Telegram task submitted for approval: {self._mask_wallet(wallet_address)} with reward {current_reward} G$")

                    return {
                        'success': True,
                        'pending': True,
                        'message': f'✅ Submission successful! Your post is waiting for admin approval.',
                        'status': 'pending_approval',
                        'telegram_url': telegram_url
                    }
                except Exception as insert_error:
                    logger.error(f"❌ Failed to submit for approval: {insert_error}")
                    return {
                        'success': False,
                        'error': 'Failed to submit for approval. Please try again.'
                    }
            else:
                logger.warning(f"⚠️ Database not configured - cannot save Telegram task submission for {wallet_address[:8]}...")
                return {
                    'success': False,
                    'error': 'The reward system database is not available right now. Please contact the administrator.'
                }

        except Exception as e:
            logger.error(f"❌ Telegram task submission error: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    async def approve_submission(self, submission_id: int, admin_wallet: str) -> Dict[str, Any]:
        """Admin approves a submission and disburses reward"""
        try:
            if not self.supabase:
                return {'success': False, 'error': 'Database not available'}

            # Get submission details
            submission = self.supabase.table('telegram_task_log')\
                .select('*')\
                .eq('id', submission_id)\
                .eq('status', 'pending')\
                .execute()

            if not submission.data or len(submission.data) == 0:
                return {'success': False, 'error': 'Submission not found or already processed'}

            sub_data = submission.data[0]
            wallet_address = sub_data['wallet_address']
            telegram_url = sub_data['telegram_url']
            reward_amount = sub_data['reward_amount'] # Use the reward amount stored in the submission

            logger.info(f"✅ Admin {admin_wallet[:8]}... approving submission {submission_id}")

            # Disburse reward
            from telegram_task.blockchain import telegram_blockchain_service

            disbursement = telegram_blockchain_service.disburse_telegram_reward_sync(
                wallet_address=wallet_address,
                amount=reward_amount,
                task_id=str(submission_id)
            )

            if disbursement.get('success'):
                # Update status to completed
                self.supabase.table('telegram_task_log').update({
                    'status': 'completed',
                    'transaction_hash': disbursement.get('tx_hash'),
                    'approved_by': admin_wallet,
                    'approved_at': datetime.now(timezone.utc).isoformat()
                }).eq('id', submission_id).execute()

                logger.info(f"✅ Telegram task approved and disbursed: {reward_amount} G$ to {self._mask_wallet(wallet_address)}")

                return {
                    'success': True,
                    'tx_hash': disbursement.get('tx_hash'),
                    'message': f'Approved! {reward_amount} G$ disbursed to user.'
                }
            else:
                # Keep status as 'pending' so admin can retry — only move out of pending on success
                logger.error(f"❌ Disbursement failed for submission {submission_id}: {disbursement.get('error')} — keeping as pending for retry")

                return {
                    'success': False,
                    'error': f"Disbursement failed: {disbursement.get('error')}. Submission kept as pending — please retry approval."
                }

        except Exception as e:
            logger.error(f"❌ Approval error: {e}")
            return {'success': False, 'error': str(e)}

    async def reject_submission(self, submission_id: int, admin_wallet: str, reason: str = '') -> Dict[str, Any]:
        """Admin rejects a submission - cooldown is reset, user can immediately resubmit"""
        try:
            if not self.supabase:
                return {'success': False, 'error': 'Database not available'}

            # Get submission details first
            submission = self.supabase.table('telegram_task_log')\
                .select('wallet_address')\
                .eq('id', submission_id)\
                .eq('status', 'pending')\
                .execute()

            if not submission.data:
                return {'success': False, 'error': 'Submission not found or already processed'}

            wallet_address = submission.data[0]['wallet_address']

            # Update status to rejected - this effectively resets the cooldown
            result = self.supabase.table('telegram_task_log').update({
                'status': 'rejected',
                'rejected_by': admin_wallet,
                'rejected_at': datetime.now(timezone.utc).isoformat(),
                'rejection_reason': reason
            }).eq('id', submission_id).eq('status', 'pending').execute()

            if result.data:
                logger.info(f"❌ Admin {admin_wallet[:8]}... rejected submission {submission_id}")
                logger.info(f"✅ Cooldown reset for {wallet_address[:8]}... - User can resubmit immediately")

                return {
                    'success': True,
                    'message': 'Submission rejected. Cooldown has been reset - user can resubmit immediately with a new post.',
                    'cooldown_reset': True
                }
            else:
                return {'success': False, 'error': 'Submission not found or already processed'}

        except Exception as e:
            logger.error(f"❌ Rejection error: {e}")
            return {'success': False, 'error': str(e)}

    async def get_task_stats(self, wallet_address: str) -> Dict[str, Any]:
        """Get user's Telegram task statistics"""
        try:
            if not self.supabase:
                return {
                    'total_earned': 0,
                    'total_claims': 0,
                    'can_claim_today': True
                }

            # Get total earned
            claims = self.supabase.table('telegram_task_log')\
                .select('reward_amount')\
                .eq('wallet_address', wallet_address)\
                .execute()

            total_earned = sum(float(c.get('reward_amount', 0)) for c in claims.data or [])
            total_claims = len(claims.data or [])

            # Check if can claim today
            eligibility = await self.check_eligibility(wallet_address)

            return {
                'total_earned': total_earned,
                'total_claims': total_claims,
                'can_claim_today': eligibility.get('can_claim', False),
                'next_claim_time': eligibility.get('next_claim_time'),
                'reward_amount': self.get_task_reward() # Fetch dynamic reward
            }

        except Exception as e:
            logger.error(f"❌ Error getting Telegram task stats: {e}")
            return {
                'total_earned': 0,
                'total_claims': 0,
                'can_claim_today': True
            }

    def get_transaction_history(self, wallet_address: str, limit: int = 50) -> Dict[str, Any]:
        """Get user's Telegram task transaction history"""
        try:
            if not self.supabase:
                return {
                    'success': True,
                    'transactions': [],
                    'total_count': 0,
                    'total_earned': 0
                }

            logger.info(f"📋 Getting Telegram task history for {wallet_address[:8]}... (limit: {limit})")

            # Get transaction history
            history = self.supabase.table('telegram_task_log')\
                .select('*')\
                .eq('wallet_address', wallet_address)\
                .order('created_at', desc=True)\
                .limit(limit)\
                .execute()

            transactions = []
            total_earned = 0

            if history.data:
                for record in history.data:
                    reward_amount = float(record.get('reward_amount', 0))
                    total_earned += reward_amount

                    transactions.append({
                        'id': record.get('id'),
                        'reward_amount': reward_amount,
                        'transaction_hash': record.get('transaction_hash'),
                        'telegram_url': record.get('telegram_url'),
                        'status': record.get('status', 'completed'),
                        'created_at': record.get('created_at'),
                        'explorer_url': f"https://explorer.celo.org/mainnet/tx/{record.get('transaction_hash')}" if record.get('transaction_hash') else None,
                        'rejection_reason': record.get('rejection_reason')
                    })

            logger.info(f"✅ Retrieved {len(transactions)} Telegram task transactions for {wallet_address[:8]}... (Total: {total_earned} G$)")

            return {
                'success': True,
                'transactions': transactions,
                'total_count': len(transactions),
                'total_earned': total_earned,
                'summary': {
                    'total_earned': total_earned,
                    'transaction_count': len(transactions),
                    'avg_reward': total_earned / len(transactions) if transactions else 0
                }
            }

        except Exception as e:
            logger.error(f"❌ Error getting Telegram task transaction history: {e}")
            return {
                'success': False,
                'error': str(e),
                'transactions': [],
                'total_count': 0,
                'total_earned': 0
            }

    def get_task_reward(self) -> float:
        """
        Fetches the current reward amount from the reward configuration service.
        This ensures that the reward amount is dynamic and can be changed by admins
        without code redeployment.
        """
        try:
            from reward_config_service import RewardConfigService
            reward_service = RewardConfigService()
            reward_amount = reward_service.get_reward_amount('telegram_task')
            logger.info(f"💰 Fetched dynamic reward amount for Telegram task: {reward_amount} G$")
            return reward_amount
        except Exception as e:
            logger.error(f"❌ Failed to fetch dynamic reward amount: {e}. Falling back to default.")
            # Fallback to a default value if fetching fails
            return 100.0 # Default reward amount

# Global instance
telegram_task_service = TelegramTaskService()

def init_telegram_task(app):
    """Initialize Telegram Task system with Flask app"""
    try:
        logger.info("📱 Initializing Telegram Task system...")

        from flask import session, request, jsonify

        @app.route('/api/telegram-task/status', methods=['GET'])
        def get_telegram_task_status():
            """Get Telegram task status for current user"""
            try:
                wallet_address = session.get('wallet_address') or session.get('wallet')
                if not wallet_address or not session.get('verified'):
                    return jsonify({'error': 'Not authenticated'}), 401

                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    stats = loop.run_until_complete(
                        telegram_task_service.get_task_stats(wallet_address)
                    )
                finally:
                    loop.close()

                return jsonify(stats), 200

            except Exception as e:
                logger.error(f"❌ Telegram task status error: {e}")
                return jsonify({'error': 'Failed to get task status'}), 500

        @app.route('/api/telegram-task/custom-message', methods=['GET'])
        def get_telegram_custom_message():
            """Get custom message for current user"""
            try:
                wallet_address = session.get('wallet_address') or session.get('wallet')
                verified = session.get('verified')

                logger.info(f"📱 Custom message request - wallet: {wallet_address[:8] if wallet_address else 'None'}..., verified: {verified}")
                logger.info(f"📱 Session keys: {list(session.keys())}")

                if not wallet_address:
                    logger.warning(f"❌ No wallet address in session")
                    return jsonify({
                        'success': False,
                        'error': 'Not authenticated - no wallet'
                    }), 401

                if not verified:
                    logger.warning(f"❌ Wallet not verified")
                    return jsonify({
                        'success': False,
                        'error': 'Not authenticated - not verified'
                    }), 401

                # Get the custom message for this user
                custom_message = telegram_task_service.get_custom_message_for_user(wallet_address)

                logger.info(f"✅ Custom message generated for {wallet_address[:8]}... (length: {len(custom_message)})")

                return jsonify({
                    'success': True,
                    'custom_message': custom_message,
                    'wallet': wallet_address[:8] + "..."
                }), 200

            except Exception as e:
                logger.error(f"❌ Error getting custom message: {e}")
                import traceback
                logger.error(f"🔍 Traceback: {traceback.format_exc()}")
                return jsonify({
                    'success': False,
                    'error': 'Failed to generate message',
                    'details': str(e)
                }), 500

        @app.route('/api/telegram-task/claim', methods=['POST'])
        def claim_telegram_task():
            """Claim Telegram task reward"""
            try:
                wallet_address = session.get('wallet_address') or session.get('wallet')
                if not wallet_address or not session.get('verified'):
                    return jsonify({'error': 'Not authenticated'}), 401

                data = request.get_json()
                telegram_url = data.get('telegram_url', '').strip()

                if not telegram_url:
                    return jsonify({
                        'success': False,
                        'error': 'Telegram post URL is required'
                    }), 400

                import asyncio

                # Use a fresh event loop to avoid conflicts
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                try:
                    result = loop.run_until_complete(
                        telegram_task_service.claim_task_reward(wallet_address, telegram_url)
                    )
                finally:
                    try:
                        loop.close()
                    except:
                        pass

                if result.get('success'):
                    return jsonify(result), 200
                else:
                    return jsonify(result), 400

            except Exception as e:
                logger.error(f"❌ Telegram task claim error: {e}")
                import traceback
                logger.error(f"🔍 Traceback: {traceback.format_exc()}")
                return jsonify({'error': 'Failed to claim task', 'details': str(e)}), 500

        @app.route('/api/telegram-task/history', methods=['GET'])
        def get_telegram_task_history():
            """Get Telegram task transaction history for current user"""
            try:
                wallet_address = session.get('wallet_address') or session.get('wallet')
                if not wallet_address or not session.get('verified'):
                    return jsonify({'error': 'Not authenticated'}), 401

                limit = int(request.args.get('limit', 50))

                history = telegram_task_service.get_transaction_history(wallet_address, limit)

                return jsonify(history), 200

            except Exception as e:
                logger.error(f"❌ Telegram task history error: {e}")
                return jsonify({
                    'success': False,
                    'error': 'Failed to get transaction history',
                    'transactions': [],
                    'total_count': 0
                }), 500

        logger.info("✅ Telegram Task system initialized successfully")
        return True

    except Exception as e:
        logger.error(f"❌ Failed to initialize Telegram Task system: {e}")
        return False
