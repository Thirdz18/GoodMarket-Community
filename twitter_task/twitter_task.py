import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional
from supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

class TwitterTaskService:
    def __init__(self):
        self.supabase = get_supabase_client()

        # Custom messages for Twitter posts (keeps @GoodDollarTeam @gooddollarorg mentions)
        self.custom_messages = self._generate_custom_messages()

        self.cooldown_hours = 72  # 72 hour cooldown

        logger.info("🐦 Twitter Task Service initialized")
        logger.info(f"⏰ Cooldown: {self.cooldown_hours} hours")
        logger.info(f"💬 Custom Messages: {len(self.custom_messages)} unique variations (covers daily tasks, CELO check-in rewards, daily G$ claims — wallet-based rotation ensures unique messages per user)")
        logger.info(f"💰 Rewards: Dynamic (loaded from admin configuration)")
    
    def get_task_reward(self) -> float:
        """Get current reward amount from configuration"""
        from reward_config_service import reward_config_service
        return reward_config_service.get_reward_amount('twitter_task')

    def _generate_custom_messages(self):
        """Generate 1000 unique custom messages for Twitter (respecting character limits)"""
        import random
        
        opening_phrases = [
            "GoodMarket is more than tasks in the GoodDollar ecosystem.",
            "Join the financial revolution with GoodMarket.",
            "Unlock Web3 potential with GoodMarket.",
            "Earn & learn.",
            "Step into the future of finance with GoodMarket.",
            "Start your journey in GoodDollar on GoodMarket today.",
            "Where education meets rewards in the thriving GoodDollar ecosystem.!",
            "Ready to earn G$?.",
            "Discover new opportunities.",
            "Join thousands earning G$ daily.",
            # Claim daily G$
            "Claim your daily G$ and keep your streak alive on GoodMarket.",
            "Your daily G$ is waiting — claim it now on GoodMarket.",
            "Don't miss your daily G$ claim on GoodMarket.",
            "Every day you claim G$ on GoodMarket you help grow the GoodDollar ecosystem.",
            "Claim daily G$ on GoodMarket and build real crypto savings over time.",
        ]

        middle_phrases = [
            "Visit goodmarket.live today to discover daily tasks & ways to earn G$ 💙",
            "Go to goodmarket.live now to explore exciting tasks and start earning G$.",
            "Check out goodmarket.live for daily opportunities to support the GoodDollar mission.",
            "Go to goodmarket.live & complete simple tasks to earn real G$ rewards daily.",
            "Access goodmarket.live to find ways to contribute & earn within our community.",
            "Your journey starts at goodmarket.live – discover quizzes that reward you in G$.",
            "Visit goodmarket.live & find out how easy it is to earn G$ while learning.",
            "Explore goodmarket.live today and join the movement for a more equitable future.",
            "Start your earning routine at goodmarket.live with fun educational tasks.",
            "Navigate to goodmarket.live & unlock multiple pathways to earn G$ rewards.",
            # Claim daily G$
            "Claim your free daily G$ at goodmarket.live — a small action that compounds into real crypto income.",
            "Your daily G$ claim at goodmarket.live takes under a minute and grows your GoodDollar balance.",
            "Visit goodmarket.live to claim your daily G$ and stay active in the GoodDollar community.",
            "Claim daily G$ at goodmarket.live and combine it with tasks, quizzes, and community rewards.",
            "Go to goodmarket.live now, claim your daily G$, and see how fast your balance grows.",
        ]

        closing_phrases = [
            "New to GoodDollar? Start today: https://goodmarket.live",
            "Join the movement! Get started: https://goodmarket.live",
            "Begin your crypto journey! Info: https://goodmarket.live",
            "Don't wait to earn! Visit: https://goodmarket.live",
            "Take the first step! Site: https://goodmarket.live",
            "Your crypto future starts here: https://goodmarket.live",
            "Start receiving UBI now: https://goodmarket.live",
            "Empower your future! Visit: https://goodmarket.live",
            "Joining is fast & simple: https://goodmarket.live",
            "Be part of our community: https://goodmarket.live",
            # Daily check-in & CELO rewards
            "Check in daily & earn CELO: https://goodmarket.live",
            "Your CELO check-in reward awaits: https://goodmarket.live",
            "Earn CELO every day — start here: https://goodmarket.live",
            "Daily CELO rewards on GoodMarket: https://goodmarket.live",
            "Check in now, earn CELO today: https://goodmarket.live",
            # Claim daily G$
            "Claim your daily G$ here: https://goodmarket.live",
            "Your daily G$ is waiting for you: https://goodmarket.live",
            "Claim G$ every day — visit: https://goodmarket.live",
            "Don't miss today's G$ claim: https://goodmarket.live",
            "Free daily G$ — claim yours now: https://goodmarket.live",
        ]

        templates = []
        for i in range(1000):
            s1 = opening_phrases[i % len(opening_phrases)]
            s2 = middle_phrases[(i // 10) % len(middle_phrases)]
            s3 = closing_phrases[(i // 100) % len(closing_phrases)]
            
            # Twitter messages are shorter to fit limits
            message = f"🐦 {s1} {s2}\n\n{s3} @gooddollarorg @GoodDollarTeam"
            templates.append(message)
        
        return templates

    def _mask_wallet(self, wallet_address: str) -> str:
        """Mask wallet address for display"""
        if not wallet_address or len(wallet_address) < 10:
            return wallet_address
        return wallet_address[:6] + "..." + wallet_address[-4:]

    def get_custom_message_for_user(self, wallet_address: str) -> str:
        """Get custom message for the user - wallet-based rotation ensures unique messages with personal referral link"""
        import hashlib
        from datetime import datetime, timezone
        from referral_program.referral_service import ReferralService
        
        # Normalize wallet address to lowercase
        wallet_normalized = wallet_address.lower().strip()
        
        # Hash wallet address to get consistent index
        wallet_hash = int(hashlib.sha256(wallet_normalized.encode()).hexdigest(), 16)
        
        # Get current UTC time for rotation
        now_utc = datetime.now(timezone.utc)
        day_of_year = now_utc.timetuple().tm_yday
        hour_of_day = now_utc.hour
        
        # Use multiple factors for better distribution
        last_4_chars = int(wallet_normalized[-4:], 16) if len(wallet_normalized) >= 4 else 0
        
        # Combine all factors for unique message index
        message_index = (
            wallet_hash + 
            (day_of_year * 37) +  # Prime number multiplier
            (hour_of_day * 17) +   # Prime number multiplier
            (last_4_chars * 7)     # Prime number multiplier
        ) % len(self.custom_messages)
        
        # Get the base message template
        message = self.custom_messages[message_index]
        
        # Replace the static goodmarket.live URL with the user's personal referral link
        try:
            referral_service = ReferralService()
            referral_code = referral_service.generate_code_for_wallet(wallet_address)
            referral_link = f"https://goodmarket.live/?ref={referral_code}"
            message = message.replace("https://goodmarket.live", referral_link)
            logger.info(f"🔗 Referral link injected for {wallet_address[:8]}...: {referral_link}")
        except Exception as ref_err:
            logger.warning(f"⚠️ Could not generate referral link for {wallet_address[:8]}...: {ref_err}")
        
        logger.info(f"📅 Message index {message_index} for user: {wallet_address[:8]}... (Day: {day_of_year}, Hour: {hour_of_day}, 1000 unique messages available)")
        return message

    def _validate_twitter_url(self, twitter_url: str) -> Dict[str, Any]:
        """Validate Twitter post URL"""
        try:
            twitter_url = twitter_url.strip()

            if not twitter_url:
                return {"valid": False, "error": "Twitter post URL is required"}

            # Valid formats: https://twitter.com/user/status/123 or https://x.com/user/status/123
            if not (twitter_url.startswith("https://twitter.com/") or
                   twitter_url.startswith("https://x.com/")):
                return {"valid": False, "error": "Please provide a valid Twitter post URL (https://twitter.com/... or https://x.com/...)"}

            # Check if URL contains /status/ (tweet format)
            if "/status/" not in twitter_url:
                return {"valid": False, "error": "URL must be a direct link to your tweet (should contain /status/)"}

            # Extract tweet ID
            url_parts = twitter_url.split('/')
            status_index = url_parts.index('status')

            if len(url_parts) <= status_index + 1:
                return {"valid": False, "error": "Invalid tweet link format"}

            tweet_id = url_parts[status_index + 1].split('?')[0]  # Remove query params

            if not tweet_id.isdigit():
                return {"valid": False, "error": "Invalid tweet ID in URL"}

            # Minimum tweet ID validation (Twitter IDs are very long numbers)
            if len(tweet_id) < 10:
                return {"valid": False, "error": "Invalid tweet link. Please provide a real Twitter post URL"}

            # CRITICAL: Verify post exists using Twitter/X Web API (NO API TOKEN NEEDED)
            try:
                import requests

                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                }

                logger.info(f"🔍 Verifying Twitter post existence: {twitter_url}")

                response = requests.get(twitter_url, headers=headers, timeout=5, allow_redirects=True)

                # Check response status
                if response.status_code == 200:
                    # Post exists! Basic verification passed
                    logger.info(f"✅ Twitter post verified as existing")
                elif response.status_code == 404:
                    logger.warning(f"❌ Post does not exist (404)")
                    return {"valid": False, "error": "This post does not exist. Please create a real post and submit the correct link."}
                else:
                    logger.warning(f"⚠️ Unexpected status code {response.status_code} - allowing for admin review")
                    # Don't block on unexpected errors, allow through
                    pass

            except requests.exceptions.Timeout:
                logger.warning(f"⚠️ Twitter verification timeout - allowing request for admin review")
                # Don't block user if verification times out
                pass

            except requests.exceptions.ConnectionError as conn_err:
                logger.warning(f"⚠️ Connection error during verification - allowing request: {conn_err}")
                # Don't block user on network issues
                pass

            except Exception as verify_error:
                logger.warning(f"⚠️ Post verification failed - allowing for admin review: {verify_error}")
                # Don't block user if verification fails
                pass

            return {"valid": True, "twitter_url": twitter_url}

        except Exception as e:
            logger.error(f"❌ Twitter URL validation error: {e}")
            return {"valid": False, "error": "Validation failed. Please try again."}

    async def check_eligibility(self, wallet_address: str) -> Dict[str, Any]:
        """Check if user can claim Twitter task reward - CACHED"""
        # Use 60-second cache for eligibility
        cache_key = f'twitter_elig_{wallet_address}'
        if hasattr(self, '_cache'):
            if cache_key in self._cache:
                cached_data, cached_time = self._cache[cache_key]
                import time
                if time.time() - cached_time < 60:  # 60 seconds
                    logger.info(f"📦 Using cached Twitter eligibility for {wallet_address[:8]}...")
                    return cached_data
        else:
            self._cache = {}
        
        try:
            if not self.supabase:
                return {
                    'can_claim': True,
                    'reason': 'Database not available'
                }

            logger.info(f"🔍 Checking Twitter eligibility for {wallet_address[:8]}...")

            # Check for pending submission (waiting for approval)
            pending_check = self.supabase.table('twitter_task_log')\
                .select('created_at, status')\
                .eq('wallet_address', wallet_address)\
                .eq('status', 'pending')\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()

            logger.info(f"🔍 Pending check result: {len(pending_check.data) if pending_check.data else 0} pending submissions")

            if pending_check.data:
                result = {
                    'can_claim': False,
                    'has_pending_submission': True,
                    'reason': 'Waiting for admin approval',
                    'status': 'pending'
                }
                
                # Cache pending status
                import time
                self._cache[cache_key] = (result, time.time())
                
                return result

            # Check last COMPLETED claim within the cooldown window (only approved submissions trigger cooldown)
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=self.cooldown_hours)
            last_claim = self.supabase.table('twitter_task_log')\
                .select('created_at, status')\
                .eq('wallet_address', wallet_address)\
                .eq('status', 'completed')\
                .gte('created_at', cutoff_time.isoformat())\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()

            logger.info(f"🔍 Completed claims (last {self.cooldown_hours}h): {len(last_claim.data) if last_claim.data else 0}")
            if last_claim.data:
                logger.info(f"🔍 Last completed claim: {last_claim.data[0]}")

            if last_claim.data:
                # User already has approved claim within cooldown window - cooldown active
                last_claim_time = datetime.fromisoformat(last_claim.data[0]['created_at'].replace('Z', '+00:00'))
                next_claim_time = last_claim_time + timedelta(hours=self.cooldown_hours)

                logger.info(f"⏰ Cooldown active - Last claim: {last_claim_time}, Next available: {next_claim_time}")

                result = {
                    'can_claim': False,
                    'reason': f'Already claimed within the last {self.cooldown_hours} hours',
                    'next_claim_time': next_claim_time.isoformat(),
                    'last_claim': last_claim_time.isoformat()
                }
                
                # Cache blocked result
                import time
                self._cache[cache_key] = (result, time.time())
                
                return result

            logger.info(f"✅ User can claim - no completed claims within cooldown window")

            result = {
                'can_claim': True,
                'reward_amount': self.get_task_reward()
            }
            
            # Cache the result
            import time
            self._cache[cache_key] = (result, time.time())
            
            return result

        except Exception as e:
            logger.error(f"❌ Error checking Twitter task eligibility: {e}")
            error_result = {
                'can_claim': True,
                'reason': 'Error checking eligibility'
            }
            
            # Cache error result too (prevent repeated failures)
            import time
            self._cache[cache_key] = (error_result, time.time())
            
            return error_result

    async def claim_task_reward(self, wallet_address: str, twitter_url: str) -> Dict[str, Any]:
        """Submit Twitter task for admin approval"""
        try:
            logger.info(f"🐦 Twitter task submission started for {wallet_address[:8]}... with URL: {twitter_url}")

            if not twitter_url or not twitter_url.strip():
                logger.warning(f"❌ Empty URL provided")
                return {
                    'success': False,
                    'error': 'Twitter post URL is required'
                }

            # Validate URL
            validation = self._validate_twitter_url(twitter_url)
            logger.info(f"🔍 URL validation result: {validation}")

            if not validation.get('valid'):
                logger.warning(f"❌ URL validation failed: {validation.get('error')}")
                return {
                    'success': False,
                    'error': validation.get('error', 'Invalid Twitter URL')
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

            # Check if URL already exists (STRICT DUPLICATE PREVENTION)
            if self.supabase:
                try:
                    # Extract tweet ID from URL for consistent checking
                    url_parts = twitter_url.split('/')
                    status_index = url_parts.index('status')
                    tweet_id = url_parts[status_index + 1].split('?')[0]

                    logger.info(f"🔍 Checking if tweet ID {tweet_id} has been used before...")

                    # Get all existing twitter URLs from database
                    from supabase_client import safe_supabase_operation

                    url_check = safe_supabase_operation(
                        lambda: self.supabase.table('twitter_task_log')\
                            .select('wallet_address, created_at, twitter_url, status')\
                            .execute(),
                        fallback_result=type('obj', (object,), {'data': []})(),
                        operation_name="check twitter URL uniqueness"
                    )

                    if url_check and url_check.data:
                        # Check each URL to see if it contains the same tweet ID
                        for record in url_check.data:
                            existing_url = record.get('twitter_url', '')
                            # Check if tweet ID exists in the URL
                            if f'/status/{tweet_id}' in existing_url or tweet_id in existing_url:
                                previous_wallet = record.get('wallet_address', 'Unknown')
                                previous_status = record.get('status', 'pending')

                                if previous_wallet == wallet_address:
                                    if previous_status == 'pending':
                                        return {
                                            'success': False,
                                            'error': 'You already submitted this post. Please wait for admin approval.'
                                        }
                                    else:
                                        logger.warning(f"❌ User {wallet_address[:8]}... already used tweet ID {tweet_id}")
                                        return {
                                            'success': False,
                                            'error': f'⚠️ This Twitter post link (ID: {tweet_id}) has already been used by you. Please create a NEW Twitter post and submit its link.'
                                        }
                                else:
                                    logger.warning(f"❌ Tweet ID {tweet_id} already used by another wallet: {previous_wallet[:8]}...")
                                    return {
                                        'success': False,
                                        'error': f'⚠️ This Twitter post link (ID: {tweet_id}) has already been claimed by another user. Please create your OWN Twitter post about GoodDollar and submit its link.'
                                    }

                    logger.info(f"✅ Tweet ID {tweet_id} is unique and unused - submitting for approval")

                except ValueError:
                    logger.error(f"❌ Could not extract tweet ID from URL")
                    return {
                        'success': False,
                        'error': 'Invalid Twitter URL format. Please provide a valid tweet link.'
                    }
                except Exception as db_error:
                    logger.error(f"❌ Database URL check error: {db_error}")
                    import traceback
                    logger.error(f"🔍 Database error traceback: {traceback.format_exc()}")
                    return {
                        'success': False,
                        'error': 'Unable to verify post uniqueness. Please try again.'
                    }

            # Submit for admin approval with retry logic
            if self.supabase:
                max_retries = 5  # Increased from 3 to 5
                for attempt in range(max_retries):
                    try:
                        if attempt > 0:
                            # Exponential backoff: 2s, 4s, 8s, 16s
                            import time
                            wait_time = 2 ** attempt
                            logger.info(f"⏳ Waiting {wait_time}s before retry {attempt + 1}...")
                            time.sleep(wait_time)
                            
                            # Reinitialize Supabase connection
                            from supabase_client import get_supabase_client
                            self.supabase = get_supabase_client()
                            if not self.supabase:
                                logger.error(f"❌ Failed to reconnect to database on attempt {attempt + 1}")
                                if attempt < max_retries - 1:
                                    continue
                                return {
                                    'success': False,
                                    'error': 'Database connection failed. Please try again in a moment.'
                                }
                        
                        logger.info(f"📝 Submitting Twitter task (attempt {attempt + 1}/{max_retries})...")
                        logger.info(f"   URL: {twitter_url}")
                        
                        # Get dynamic reward for this specific submission
                        current_reward = self.get_task_reward()
                        logger.info(f"   Reward: {current_reward} G$")
                        
                        # Insert with NULL transaction_hash for pending submissions
                        result = self.supabase.table('twitter_task_log').insert({
                            'wallet_address': wallet_address,
                            'twitter_url': twitter_url,
                            'reward_amount': current_reward,
                            'status': 'pending',
                            'transaction_hash': None,
                            'created_at': datetime.now(timezone.utc).isoformat()
                        }).execute()

                        logger.info(f"🔍 Database insert result: {result}")
                        
                        if result and hasattr(result, 'data') and result.data:
                            logger.info(f"✅ Twitter task submitted for approval: {self._mask_wallet(wallet_address)}")

                            return {
                                'success': True,
                                'pending': True,
                                'message': f'✅ Submission successful! Your post is waiting for admin approval.',
                                'status': 'pending_approval',
                                'twitter_url': twitter_url
                            }
                        else:
                            logger.error(f"❌ Database insert failed - no data returned")
                            if attempt < max_retries - 1:
                                continue
                            return {
                                'success': False,
                                'error': 'Failed to save submission. Please try again.'
                            }
                            
                    except Exception as insert_error:
                        error_msg = str(insert_error).lower()
                        
                        logger.error(f"❌ Submission attempt {attempt + 1} failed: {insert_error}")
                        
                        # Check if it's a network/connection error
                        is_network_error = any(keyword in error_msg for keyword in [
                            'connection', 'timeout', 'network', 'disconnect', 
                            'refused', 'unreachable', 'temporary failure'
                        ])
                        
                        if is_network_error and attempt < max_retries - 1:
                            logger.warning(f"⚠️ Network error detected, will retry...")
                            continue
                        
                        if attempt >= max_retries - 1:
                            logger.error(f"❌ Failed to submit after {max_retries} attempts: {insert_error}")
                        
                        if 'unique' in error_msg or 'duplicate' in error_msg:
                            return {
                                'success': False,
                                'error': 'This post has already been submitted.'
                            }
                        elif is_network_error:
                            return {
                                'success': False,
                                'error': 'Database connection issue. Please wait a moment and try again.'
                            }
                        else:
                            return {
                                'success': False,
                                'error': 'Failed to save submission. Please try again.'
                            }
            else:
                logger.error(f"❌ Supabase client not available")
                return {
                    'success': False,
                    'error': 'Database not available. Please try again later.'
                }

        except Exception as e:
            logger.error(f"❌ Twitter task submission error: {e}")
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
            submission = self.supabase.table('twitter_task_log')\
                .select('*')\
                .eq('id', submission_id)\
                .eq('status', 'pending')\
                .execute()

            if not submission.data or len(submission.data) == 0:
                return {'success': False, 'error': 'Submission not found or already processed'}

            sub_data = submission.data[0]
            wallet_address = sub_data['wallet_address']
            twitter_url = sub_data['twitter_url']

            logger.info(f"✅ Admin {admin_wallet[:8]}... approving submission {submission_id}")

            # Disburse reward (use the amount stored in the submission)
            from twitter_task.blockchain import twitter_blockchain_service

            current_reward = float(sub_data['reward_amount'])
            disbursement = twitter_blockchain_service.disburse_twitter_reward_sync(
                wallet_address=wallet_address,
                amount=current_reward,
                task_id=str(submission_id)
            )

            if disbursement.get('success'):
                # Update status to completed
                self.supabase.table('twitter_task_log').update({
                    'status': 'completed',
                    'transaction_hash': disbursement.get('tx_hash'),
                    'approved_by': admin_wallet,
                    'approved_at': datetime.now(timezone.utc).isoformat()
                }).eq('id', submission_id).execute()

                logger.info(f"✅ Twitter task approved and disbursed: {current_reward} G$ to {self._mask_wallet(wallet_address)}")

                return {
                    'success': True,
                    'tx_hash': disbursement.get('tx_hash'),
                    'message': f'Approved! {current_reward} G$ disbursed to user.'
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
            submission = self.supabase.table('twitter_task_log')\
                .select('wallet_address')\
                .eq('id', submission_id)\
                .eq('status', 'pending')\
                .execute()

            if not submission.data:
                return {'success': False, 'error': 'Submission not found or already processed'}

            wallet_address = submission.data[0]['wallet_address']

            # Update status to rejected - this effectively resets the cooldown
            self.supabase.table('twitter_task_log').update({
                'status': 'rejected',
                'rejected_by': admin_wallet,
                'rejected_at': datetime.now(timezone.utc).isoformat(),
                'rejection_reason': reason
            }).eq('id', submission_id).eq('status', 'pending').execute()

            logger.info(f"❌ Admin {admin_wallet[:8]}... rejected submission {submission_id}")
            logger.info(f"✅ Cooldown reset for {wallet_address[:8]}... - User can resubmit immediately")

            return {
                'success': True,
                'message': 'Submission rejected. Cooldown has been reset - user can submit a new post immediately.',
                'cooldown_reset': True
            }

        except Exception as e:
            logger.error(f"❌ Rejection error: {e}")
            return {'success': False, 'error': str(e)}

    async def get_task_stats(self, wallet_address: str) -> Dict[str, Any]:
        """Get user's Twitter task statistics"""
        try:
            if not self.supabase:
                return {
                    'total_earned': 0,
                    'total_claims': 0,
                    'can_claim_today': True
                }

            claims = self.supabase.table('twitter_task_log')\
                .select('reward_amount')\
                .eq('wallet_address', wallet_address)\
                .execute()

            total_earned = sum(float(c.get('reward_amount', 0)) for c in claims.data or [])
            total_claims = len(claims.data or [])

            eligibility = await self.check_eligibility(wallet_address)

            return {
                'total_earned': total_earned,
                'total_claims': total_claims,
                'can_claim_today': eligibility.get('can_claim', False),
                'next_claim_time': eligibility.get('next_claim_time'),
                'reward_amount': self.get_task_reward()
            }

        except Exception as e:
            logger.error(f"❌ Error getting Twitter task stats: {e}")
            return {
                'total_earned': 0,
                'total_claims': 0,
                'can_claim_today': True
            }

    def get_transaction_history(self, wallet_address: str, limit: int = 50) -> Dict[str, Any]:
        """Get user's Twitter task transaction history"""
        try:
            if not self.supabase:
                return {
                    'success': True,
                    'transactions': [],
                    'total_count': 0,
                    'total_earned': 0
                }

            logger.info(f"📋 Getting Twitter task history for {wallet_address[:8]}... (limit: {limit})")

            history = self.supabase.table('twitter_task_log')\
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
                        'twitter_url': record.get('twitter_url'),
                        'status': record.get('status', 'completed'),
                        'created_at': record.get('created_at'),
                        'explorer_url': f"https://explorer.celo.org/mainnet/tx/{record.get('transaction_hash')}" if record.get('transaction_hash') else None,
                        'rejection_reason': record.get('rejection_reason')
                    })

            logger.info(f"✅ Retrieved {len(transactions)} Twitter task transactions for {wallet_address[:8]}... (Total: {total_earned} G$)")

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
            logger.error(f"❌ Error getting Twitter task transaction history: {e}")
            return {
                'success': False,
                'error': str(e),
                'transactions': [],
                'total_count': 0,
                'total_earned': 0
            }

# Global instance
twitter_task_service = TwitterTaskService()

def init_twitter_task(app):
    """Initialize Twitter Task system with Flask app"""
    try:
        logger.info("🐦 Initializing Twitter Task system...")

        from flask import session, request, jsonify

        @app.route('/api/twitter-task/status', methods=['GET'])
        def get_twitter_task_status():
            """Get Twitter task status for current user"""
            try:
                wallet_address = session.get('wallet_address') or session.get('wallet')
                if not wallet_address or not session.get('verified'):
                    return jsonify({'error': 'Not authenticated'}), 401

                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    stats = loop.run_until_complete(
                        twitter_task_service.get_task_stats(wallet_address)
                    )
                finally:
                    loop.close()

                return jsonify(stats), 200

            except Exception as e:
                logger.error(f"❌ Twitter task status error: {e}")
                return jsonify({'error': 'Failed to get task status'}), 500

        @app.route('/api/twitter-task/custom-message', methods=['GET'])
        def get_twitter_custom_message():
            """Get custom message for current user"""
            try:
                wallet_address = session.get('wallet_address') or session.get('wallet')
                if not wallet_address or not session.get('verified'):
                    return jsonify({'error': 'Not authenticated'}), 401

                custom_message = twitter_task_service.get_custom_message_for_user(wallet_address)

                return jsonify({
                    'success': True,
                    'custom_message': custom_message
                })

            except Exception as e:
                logger.error(f"❌ Error getting custom message: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @app.route('/api/twitter-task/claim', methods=['POST'])
        def claim_twitter_task():
            """Claim Twitter task reward"""
            try:
                wallet_address = session.get('wallet_address') or session.get('wallet')
                if not wallet_address or not session.get('verified'):
                    return jsonify({'error': 'Not authenticated'}), 401

                data = request.get_json()
                twitter_url = data.get('twitter_url', '').strip()

                if not twitter_url:
                    return jsonify({
                        'success': False,
                        'error': 'Twitter post URL is required'
                    }), 400

                import asyncio

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                try:
                    result = loop.run_until_complete(
                        twitter_task_service.claim_task_reward(wallet_address, twitter_url)
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
                logger.error(f"❌ Twitter task claim error: {e}")
                import traceback
                logger.error(f"🔍 Traceback: {traceback.format_exc()}")
                return jsonify({'error': 'Failed to claim task', 'details': str(e)}), 500

        @app.route('/api/twitter-task/history', methods=['GET'])
        def get_twitter_task_history():
            """Get Twitter task transaction history for current user"""
            try:
                wallet_address = session.get('wallet_address') or session.get('wallet')
                if not wallet_address or not session.get('verified'):
                    return jsonify({'error': 'Not authenticated'}), 401

                limit = int(request.args.get('limit', 50))

                history = twitter_task_service.get_transaction_history(wallet_address, limit)

                return jsonify(history), 200

            except Exception as e:
                logger.error(f"❌ Twitter task history error: {e}")
                return jsonify({
                    'success': False,
                    'error': 'Failed to get transaction history',
                    'transactions': [],
                    'total_count': 0
                }), 500

        logger.info("✅ Twitter Task system initialized successfully")
        return True

    except Exception as e:
        logger.error(f"❌ Failed to initialize Twitter Task system: {e}")
        return False
