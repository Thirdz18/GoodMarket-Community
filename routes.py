from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, make_response
from blockchain import has_recent_ubi_claim, GOODDOLLAR_CONTRACTS
from analytics_service import analytics
from supabase_client import get_supabase_client, safe_supabase_operation, supabase_logger, log_admin_action
from notifications_service import NotificationService
from web3 import Web3
import json
import logging
import os
import threading
import time
import urllib.request
import urllib.error

# Initialize notification service
notification_service = NotificationService()

# Logger for this module
logger = logging.getLogger(__name__)

# Simple TTL caches for high-frequency public endpoints
_price_visibility_cache: dict = {"data": None, "expires": 0}
_feature_visibility_cache: dict = {"data": None, "expires": 0}
_PUBLIC_ENDPOINT_CACHE_TTL = 60  # seconds

# Create Blueprint FIRST - BEFORE any route decorators
routes = Blueprint("routes", __name__)

def auth_required(f):
    """Decorator for endpoints requiring authentication with auto-logout on expiry"""
    def wrapper(*args, **kwargs):
        wallet = session.get("wallet")
        verified = session.get("verified")

        if not verified or not wallet:
            return jsonify({"success": False, "error": "Authentication required"}), 401

        # UBI check temporarily disabled — all verified sessions allowed
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

def admin_required(f):
    """Decorator for endpoints requiring admin authentication"""
    def wrapper(*args, **kwargs):
        wallet = session.get("wallet")
        if not session.get("verified") or not wallet:
            return jsonify({"success": False, "error": "Authentication required"}), 401

        from supabase_client import is_admin
        if not is_admin(wallet):
            return jsonify({"success": False, "error": "Admin access required"}), 403

        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

@routes.route('/api/daily-task/claim', methods=['POST'])
@auth_required
def claim_daily_task():
    """Claim unified daily task (Twitter or Telegram)"""
    try:
        wallet = session.get('wallet')
        data = request.get_json()

        platform = data.get('platform')  # 'twitter' or 'telegram'
        post_url = data.get('post_url')

        if platform not in ['twitter', 'telegram']:
            return jsonify({
                'success': False,
                'error': 'Invalid platform. Choose twitter or telegram.'
            }), 400

        if not post_url:
            return jsonify({
                'success': False,
                'error': f'{platform.capitalize()} post URL is required'
            }), 400

        # Import appropriate service
        if platform == 'twitter':
            from twitter_task.twitter_task import twitter_task_service
            service = twitter_task_service
        else:  # telegram
            from telegram_task.telegram_task import telegram_task_service
            service = telegram_task_service

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            result = loop.run_until_complete(
                service.claim_task_reward(wallet, post_url)
            )

            if result is None:
                logger.error(f"❌ claim_task_reward returned None for platform={platform}")
                return jsonify({'success': False, 'error': 'Unexpected error processing your submission. Please try again.'}), 500

            if result.get('success'):
                return jsonify(result), 200
            else:
                return jsonify(result), 400

        finally:
            loop.close()

    except Exception as e:
        logger.error(f"❌ Daily task claim error: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': 'Failed to claim reward'}), 500

@routes.route('/api/daily-task/status', methods=['GET'])
@auth_required
def get_daily_task_status():
    """Get unified daily task status (checks both Twitter and Telegram)"""
    try:
        wallet = session.get('wallet')

        # Import both services
        from twitter_task.twitter_task import twitter_task_service
        from telegram_task.telegram_task import telegram_task_service
        from datetime import datetime, timezone

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            # Check both tasks
            twitter_status = loop.run_until_complete(twitter_task_service.check_eligibility(wallet))
            telegram_status = loop.run_until_complete(telegram_task_service.check_eligibility(wallet))

            # CRITICAL FIX: Check platforms for pending AND check database for actual pending submissions
            # This ensures real-time accuracy even with caching issues

            # First, check direct database for ANY pending submissions
            supabase = get_supabase_client()
            actual_pending = False
            actual_pending_platform = None

            if supabase:
                # Check Twitter pending
                twitter_pending_check = safe_supabase_operation(
                    lambda: supabase.table('twitter_task_log')\
                        .select('id')\
                        .eq('wallet_address', wallet)\
                        .eq('status', 'pending')\
                        .limit(1)\
                        .execute(),
                    fallback_result=type('obj', (object,), {'data': []})(),
                    operation_name="check twitter pending"
                )

                if twitter_pending_check.data and len(twitter_pending_check.data) > 0:
                    actual_pending = True
                    actual_pending_platform = 'Twitter'

                # Check Telegram pending only if Twitter not pending
                if not actual_pending:
                    telegram_pending_check = safe_supabase_operation(
                        lambda: supabase.table('telegram_task_log')\
                            .select('id')\
                            .eq('wallet_address', wallet)\
                            .eq('status', 'pending')\
                            .limit(1)\
                            .execute(),
                        fallback_result=type('obj', (object,), {'data': []})(),
                        operation_name="check telegram pending"
                    )

                    if telegram_pending_check.data and len(telegram_pending_check.data) > 0:
                        actual_pending = True
                        actual_pending_platform = 'Telegram'

            # Determine pending platform based on actual database check
            if actual_pending:
                pending_platform = actual_pending_platform
            else:
                pending_platform = None

            # Determine next claim time based on eligible platform cooldowns
            next_claim_time = None
            if actual_pending:
                # If there's a pending submission, next_claim_time is not relevant for claiming
                pass
            else:
                # Check for cooldown (completed claims) - if ANY platform has cooldown, ALL are blocked
                if not twitter_status.get('can_claim') or not telegram_status.get('can_claim'):
                    # If any platform has cooldown active (from completed claims), all are blocked
                    twitter_next = twitter_status.get('next_claim_time')
                    telegram_next = telegram_status.get('next_claim_time')

                    # Find the earliest next claim time among all platforms
                    possible_next_claims = [t for t in [twitter_next, telegram_next] if t]
                    if possible_next_claims:
                        next_claim_time = min(possible_next_claims)

            # Calculate time remaining if next_claim_time exists
            time_remaining_seconds = 0
            if next_claim_time:
                next_claim_dt = datetime.fromisoformat(next_claim_time.replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)
                time_remaining_seconds = max(0, int((next_claim_dt - now).total_seconds()))

            # User can claim if ALL platforms are available (shared cooldown) and no pending submissions
            can_claim = twitter_status.get('can_claim', False) and \
                        telegram_status.get('can_claim', False) and \
                        not actual_pending

            return jsonify({
                'can_claim': can_claim,
                'has_pending_submission': actual_pending,
                'pending_platform': pending_platform,
                'next_claim_time': next_claim_time,
                'time_remaining_seconds': time_remaining_seconds
            }), 200
        finally:
            loop.close()

    except Exception as e:
        logger.error(f"❌ Daily task status error: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({'error': 'Failed to get task status'}), 500


@routes.route('/api/daily-task/history', methods=['GET'])
@auth_required
def get_daily_task_history():
    """Get combined Twitter and Telegram task history"""
    try:
        wallet = session.get('wallet')
        limit = int(request.args.get('limit', 50))

        from twitter_task.twitter_task import twitter_task_service
        from telegram_task.telegram_task import telegram_task_service

        # Get all histories
        twitter_history = twitter_task_service.get_transaction_history(wallet, limit)
        telegram_history = telegram_task_service.get_transaction_history(wallet, limit)

        # Combine transactions
        all_transactions = []

        if twitter_history.get('success') and twitter_history.get('transactions'):
            for tx in twitter_history['transactions']:
                tx['platform'] = 'twitter'
                # Ensure rejection_reason is included
                if 'rejection_reason' not in tx:
                    tx['rejection_reason'] = None
                all_transactions.append(tx)

        if telegram_history.get('success') and telegram_history.get('transactions'):
            for tx in telegram_history['transactions']:
                tx['platform'] = 'telegram'
                # Ensure rejection_reason is included
                if 'rejection_reason' not in tx:
                    tx['rejection_reason'] = None
                all_transactions.append(tx)

        # Sort by date (newest first)
        all_transactions.sort(key=lambda x: x.get('created_at', ''), reverse=True)

        # Limit results
        all_transactions = all_transactions[:limit]

        # Calculate totals
        total_earned = sum(float(tx.get('reward_amount', 0)) for tx in all_transactions)

        return jsonify({
            'success': True,
            'transactions': all_transactions,
            'total_count': len(all_transactions),
            'total_earned': total_earned
        })

    except Exception as e:
        logger.error(f"❌ Daily task history error: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({
            'success': False,
            'error': 'Failed to get history',
            'transactions': [],
            'total_count': 0,
            'total_earned': 0
        }), 500


@routes.route("/api/recent-daily-tasks", methods=["GET"])
def get_recent_daily_tasks():
    """Get recent daily task submissions from last 24 hours"""
    try:
        from datetime import datetime, timedelta
        from supabase_client import get_supabase_client
        from flask import Response
        from cache_utils import api_cache, cached

        # Check cache first (2 minute TTL)
        cache_key = "recent_daily_tasks"
        cached_result = api_cache.get(cache_key)
        if cached_result:
            response = jsonify(cached_result)
            response.headers['Content-Type'] = 'application/json'
            response.headers['Cache-Control'] = 'public, max-age=120'
            return response, 200

        supabase = get_supabase_client()
        if not supabase:
            response = jsonify({"success": False, "submissions": []})
            response.headers['Content-Type'] = 'application/json'
            return response, 200

        # Calculate 24 hours ago
        twenty_four_hours_ago = (datetime.utcnow() - timedelta(hours=24)).isoformat()

        # Get Twitter task submissions from last 24 hours
        twitter_submissions = safe_supabase_operation(
            lambda: supabase.table('twitter_task_log')\
                .select('wallet_address, reward_amount, created_at, twitter_url')\
                .gte('created_at', twenty_four_hours_ago)\
                .order('created_at', desc=True)\
                .limit(50)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get recent twitter tasks"
        )

        # Get Telegram task submissions from last 24 hours
        telegram_submissions = safe_supabase_operation(
            lambda: supabase.table('telegram_task_log')\
                .select('wallet_address, reward_amount, created_at, telegram_url')\
                .gte('created_at', twenty_four_hours_ago)\
                .order('created_at', desc=True)\
                .limit(50)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get recent telegram tasks"
        )

        # Combine and format submissions WITH MESSAGES/LINKS
        all_submissions = []

        # Add Twitter submissions WITH LINKS - USE CACHED USERNAMES
        if twitter_submissions and twitter_submissions.data:
            for sub in twitter_submissions.data:
                wallet = sub.get('wallet_address', '')

                all_submissions.append({
                    'wallet_address': wallet,
                    'display_name': f"{wallet[:6]}...{wallet[-4:]}",
                    'reward_amount': float(sub.get('reward_amount', 0)),
                    'created_at': sub.get('created_at'),
                    'platform': 'Twitter',
                    'submission_url': sub.get('twitter_url', ''),
                    'submission_type': 'twitter_post',
                    'status': sub.get('status', 'completed'),
                    'rejection_reason': sub.get('rejection_reason')
                })

        # Add Telegram submissions WITH LINKS
        if telegram_submissions and telegram_submissions.data:
            for sub in telegram_submissions.data:
                wallet = sub.get('wallet_address', '')

                all_submissions.append({
                    'wallet_address': wallet,
                    'display_name': f"{wallet[:6]}...{wallet[-4:]}",
                    'reward_amount': float(sub.get('reward_amount', 0)),
                    'created_at': sub.get('created_at'),
                    'platform': 'Telegram',
                    'submission_url': sub.get('telegram_url', ''),
                    'submission_type': 'telegram_post',
                    'status': sub.get('status', 'completed'),
                    'rejection_reason': sub.get('rejection_reason')
                })

        # Sort by created_at (newest first)
        all_submissions.sort(key=lambda x: x['created_at'], reverse=True)

        # Limit to 20 most recent
        all_submissions = all_submissions[:20]

        logger.info(f"✅ Returning {len(all_submissions)} recent daily task submissions")

        result = {
            "success": True,
            "submissions": all_submissions,
            "total_count": len(all_submissions)
        }

        # Cache for 2 minutes for better performance
        api_cache.set(cache_key, result, ttl=120)

        response = jsonify(result)
        response.headers['Content-Type'] = 'application/json'
        response.headers['Cache-Control'] = 'public, max-age=120'
        return response, 200

    except Exception as e:
        logger.error(f"❌ Error getting recent daily tasks: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        error_response = jsonify({"success": False, "submissions": [], "error": str(e)})
        error_response.headers['Content-Type'] = 'application/json'
        return error_response, 500

@routes.route("/api/learn-earn-participants", methods=["GET"])
def get_learn_earn_participants():
    """Get Learn & Earn participants for a specific date or date range"""
    try:
        from datetime import datetime
        from supabase_client import get_supabase_client

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "participants": []})

        # Get date parameter (format: YYYY-MM-DD)
        target_date = request.args.get('date')

        if target_date:
            # Query for specific date with proper UTC timezone format
            start_datetime = f"{target_date}T00:00:00Z"
            end_datetime = f"{target_date}T23:59:59Z"
        else:
            # Default to today with proper UTC timezone format
            today = datetime.utcnow().strftime('%Y-%m-%d')
            start_datetime = f"{today}T00:00:00Z"
            end_datetime = f"{today}T23:59:59Z"

        logger.info(f"📊 Fetching Learn & Earn participants for {target_date or 'today'}")
        logger.info(f"🕐 Date range: {start_datetime} to {end_datetime}")

        # Get all Learn & Earn participants for the date
        participants = safe_supabase_operation(
            lambda: supabase.table('learnearn_log')\
                .select('wallet_address, amount_g$, timestamp, transaction_hash, quiz_id')\
                .gte('timestamp', start_datetime)\
                .lte('timestamp', end_datetime)\
                .eq('status', True)\
                .order('timestamp', desc=False)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get learn earn participants"
        )

        formatted_participants = []
        total_g_disbursed = 0
        total_achievement_cards = 0

        if participants and participants.data:
            logger.info(f"✅ Found {len(participants.data)} Learn & Earn participants")
            for p in participants.data:
                wallet = p.get('wallet_address', '')
                amount = float(p.get('amount_g$', 0))
                total_g_disbursed += amount
                total_achievement_cards += 1

                formatted_participants.append({
                    'wallet_address': wallet,
                    'display_name': f"{wallet[:6]}...{wallet[-4:]}",
                    'amount_g$': amount,
                    'amount_formatted': f"{amount:,.1f} G$",
                    'achievement_card_label': 'Convertible into NFT',
                    'timestamp': p.get('timestamp'),
                    'transaction_hash': p.get('transaction_hash', 'N/A'),
                    'quiz_id': p.get('quiz_id', 'N/A')
                })
        else:
            logger.info(f"ℹ️ No Learn & Earn participants found for {target_date or 'today'}")

        return jsonify({
            "success": True,
            "participants": formatted_participants,
            "total_count": len(formatted_participants),
            "total_g_disbursed": total_g_disbursed,
            "total_g_disbursed_formatted": f"{total_g_disbursed:,.2f} G$",
            "total_achievement_cards": total_achievement_cards,
            "total_achievement_cards_formatted": f"{total_achievement_cards:,} card{'s' if total_achievement_cards != 1 else ''}",
            "date": target_date if target_date else datetime.utcnow().strftime('%Y-%m-%d')
        })

    except Exception as e:
        logger.error(f"❌ Error getting Learn & Earn participants: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({
            "success": False,
            "participants": [],
            "total_count": 0,
            "total_g_disbursed": 0,
            "total_achievement_cards": 0,
            "total_achievement_cards_formatted": "0 cards",
            "error": str(e)
        })

@routes.route("/api/achievement-card-sales", methods=["GET"])
def get_all_achievement_card_sales():
    """Get ALL platform-wide achievement card sales for the overview analytics page"""
    try:
        from supabase_client import get_supabase_client

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "sales": [], "error": "Database not available"}), 500

        limit = int(request.args.get('limit', 100))

        result = supabase.table('achievement_card_sales')\
            .select('wallet_address, quiz_id, score, total_questions, sell_price, transaction_hash, created_at')\
            .order('created_at', desc=True)\
            .limit(limit)\
            .execute()

        sales = result.data if result.data else []
        total_earned = sum(float(s.get('sell_price', 0)) for s in sales)
        unique_sellers = len(set(s.get('wallet_address', '') for s in sales))

        formatted = []
        for s in sales:
            wallet = s.get('wallet_address', '')
            formatted.append({
                'wallet_address': wallet,
                'display_name': f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 10 else wallet,
                'score': s.get('score'),
                'total_questions': s.get('total_questions'),
                'sell_price': float(s.get('sell_price', 0)),
                'transaction_hash': s.get('transaction_hash'),
                'created_at': s.get('created_at'),
            })

        logger.info(f"📜 Platform achievement card sales: {len(sales)} records, {unique_sellers} unique sellers, total {total_earned} G$")

        return jsonify({
            "success": True,
            "sales": formatted,
            "sale_count": len(sales),
            "unique_sellers": unique_sellers,
            "total_earned": total_earned,
            "total_earned_formatted": f"{total_earned:,.2f} G$"
        })

    except Exception as e:
        logger.error(f"❌ Error fetching all achievement card sales: {e}")
        return jsonify({"success": False, "sales": [], "error": str(e)}), 500


@routes.route("/api/screenshot/<path:filename>", methods=["GET"])
def serve_screenshot(filename):
    """Serve screenshot from Object Storage"""
    try:
        from object_storage_client import download_screenshot
        from flask import send_file
        import io

        # Download from Object Storage
        file_data = download_screenshot(filename)

        if not file_data:
            return jsonify({"success": False, "error": "Screenshot not found"}), 404

        # Return as image
        return send_file(
            io.BytesIO(file_data),
            mimetype='image/png',
            as_attachment=False
        )

    except Exception as e:
        logger.error(f"❌ Error serving screenshot: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/community-screenshots", methods=["GET"])
def get_community_screenshots():
    """Get community screenshots for homepage"""
    try:
        from community_stories.community_stories_service import community_stories_service
        from supabase_client import get_supabase_client
        from cache_utils import api_cache

        # Check cache first (2 minute TTL)
        cache_key = "community_screenshots"
        cached_result = api_cache.get(cache_key)
        if cached_result:
            return jsonify(cached_result)

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "screenshots": []})

        limit = int(request.args.get('limit', 12))

        result = community_stories_service.get_screenshots_for_homepage(limit)

        if result.get('success') and result.get('screenshots'):
            # Display names are now just wallet truncations (no username lookup)
            for screenshot in result['screenshots']:
                wallet = screenshot.get('wallet_address', '')
                screenshot['display_name'] = f"{wallet[:6]}...{wallet[-4:]}"

        # Cache for 2 minutes for better performance
        api_cache.set(cache_key, result, ttl=120)

        return jsonify(result)

    except Exception as e:
        logger.error(f"❌ Error getting community screenshots: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/recent-community-stories", methods=["GET"])
def get_recent_community_stories():
    """Get recent approved community stories"""
    try:
        from supabase_client import get_supabase_client

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "stories": []})

        limit = int(request.args.get('limit', 50))

        # Get approved community stories (both high and low rewards)
        stories = safe_supabase_operation(
            lambda: supabase.table('community_stories_submissions')\
                .select('*')\
                .in_('status', ['approved_high', 'approved_low'])\
                .order('reviewed_at', desc=True)\
                .limit(limit)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get recent community stories"
        )

        # Format stories without username
        formatted_stories = []
        if stories and stories.data:
            for story in stories.data:
                wallet = story.get('wallet_address', '')

                formatted_stories.append({
                    'wallet_address': wallet,
                    'display_name': f"{wallet[:6]}...{wallet[-4:]}",
                    'reward_amount': float(story.get('reward_amount', 0)),
                    'reviewed_at': story.get('reviewed_at'),
                    'status': story.get('status'),
                    'tweet_url': story.get('tweet_url', ''),
                    'submission_id': story.get('submission_id')
                })

        return jsonify({
            "success": True,
            "stories": formatted_stories,
            "total_count": len(formatted_stories)
        })

    except Exception as e:
        logger.error(f"❌ Error getting recent community stories: {e}")
        return jsonify({"success": False, "stories": []})

@routes.route("/api/admin/maintenance-status", methods=["GET"])
@admin_required
def get_maintenance_status_api():
    feature = request.args.get('feature', 'wallet_connection')
    from maintenance_service import maintenance_service
    result = maintenance_service.get_maintenance_status(feature)
    return jsonify(result)

@routes.route("/api/admin/maintenance-status", methods=["POST"])
@admin_required
def set_maintenance_status_api():
    data = request.get_json()
    feature_name = data.get('feature_name')
    is_maintenance = data.get('is_maintenance')
    message = data.get('message')
    admin_wallet = session.get('wallet')

    from maintenance_service import maintenance_service
    result = maintenance_service.set_maintenance_status(feature_name, is_maintenance, message, admin_wallet)
    return jsonify(result)

@routes.route("/api/maintenance-status", methods=["GET"])
def public_maintenance_status():
    feature = request.args.get('feature', 'wallet_connection')
    wallet_address = request.args.get('wallet') # Get wallet from query param for exemption check

    from maintenance_service import maintenance_service
    result = maintenance_service.get_maintenance_status(feature)

    # Check if the specific wallet provided is an admin
    check_wallet = wallet_address or session.get('wallet')

    if check_wallet:
        from supabase_client import is_admin
        if is_admin(check_wallet):
            logger.info(f"🛡️ Admin {check_wallet[:8]}... detected, bypassing maintenance for {feature}")
            result['is_maintenance'] = False
            result['message'] = ""

    return jsonify(result)

@routes.route("/api/price-visibility", methods=["GET"])
def public_price_visibility():
    """Public endpoint to check if live price display is enabled"""
    try:
        now = time.time()
        if _price_visibility_cache["data"] is not None and now < _price_visibility_cache["expires"]:
            return jsonify(_price_visibility_cache["data"])

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": True, "show_price": True})
        result = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')
                .select('is_maintenance')
                .eq('feature_name', 'live_price_display')
                .execute(),
            operation_name="get price visibility"
        )
        if result and result.data:
            is_hidden = result.data[0].get('is_maintenance', False)
            data = {"success": True, "show_price": not is_hidden}
        else:
            data = {"success": True, "show_price": True}

        _price_visibility_cache["data"] = data
        _price_visibility_cache["expires"] = now + _PUBLIC_ENDPOINT_CACHE_TTL
        return jsonify(data)
    except Exception as e:
        logger.error(f"Price visibility fetch error: {e}")
        return jsonify({"success": True, "show_price": True})

@routes.route("/api/admin/price-visibility", methods=["GET"])
@admin_required
def get_price_visibility():
    """Get current price visibility setting"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500
        result = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')
                .select('is_maintenance')
                .eq('feature_name', 'live_price_display')
                .execute(),
            operation_name="get price visibility admin"
        )
        if result and result.data:
            is_hidden = result.data[0].get('is_maintenance', False)
            return jsonify({"success": True, "show_price": not is_hidden})
        return jsonify({"success": True, "show_price": True})
    except Exception as e:
        logger.error(f"Admin price visibility fetch error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/price-visibility", methods=["POST"])
@admin_required
def set_price_visibility():
    """Toggle live price display on/off"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500
        data = request.get_json()
        show_price = data.get('show_price', True)
        is_hidden = not show_price
        admin_wallet = session.get('wallet')

        existing = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')
                .select('feature_name')
                .eq('feature_name', 'live_price_display')
                .execute(),
            operation_name="check price visibility row"
        )

        if existing and existing.data:
            safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')
                    .update({'is_maintenance': is_hidden})
                    .eq('feature_name', 'live_price_display')
                    .execute(),
                operation_name="update price visibility"
            )
        else:
            safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')
                    .insert({'feature_name': 'live_price_display', 'is_maintenance': is_hidden, 'maintenance_message': ''})
                    .execute(),
                operation_name="insert price visibility"
            )

        log_admin_action(
            admin_wallet=admin_wallet,
            action_type="update_price_visibility",
            action_details={"show_price": show_price}
        )

        _price_visibility_cache["data"] = None
        _price_visibility_cache["expires"] = 0
        return jsonify({"success": True, "show_price": show_price})
    except Exception as e:
        logger.error(f"Set price visibility error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/feature-visibility", methods=["GET"])
def public_feature_visibility():
    """Public endpoint to check if swap/wallet features are visible to users"""
    try:
        now = time.time()
        if _feature_visibility_cache["data"] is not None and now < _feature_visibility_cache["expires"]:
            return jsonify(_feature_visibility_cache["data"])

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": True, "swap_visible": True, "wallet_visible": True,
                            "savings_visible": True, "topup_visible": True, "giftcard_visible": True, "utility_visible": True})
        result = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')
                .select('feature_name,is_maintenance')
                .in_('feature_name', ['swap_feature', 'wallet_feature', 'savings_feature', 'store_topup', 'store_giftcard', 'store_utility'])
                .execute(),
            operation_name="get feature visibility"
        )
        swap_visible = True
        wallet_visible = True
        savings_visible = True
        topup_visible = True
        giftcard_visible = True
        utility_visible = True
        if result and result.data:
            for row in result.data:
                fn = row['feature_name']
                val = not row.get('is_maintenance', False)
                if fn == 'swap_feature':
                    swap_visible = val
                elif fn == 'wallet_feature':
                    wallet_visible = val
                elif fn == 'savings_feature':
                    savings_visible = val
                elif fn == 'store_topup':
                    topup_visible = val
                elif fn == 'store_giftcard':
                    giftcard_visible = val
                elif fn == 'store_utility':
                    utility_visible = val
        data = {"success": True, "swap_visible": swap_visible, "wallet_visible": wallet_visible,
                "savings_visible": savings_visible,
                "topup_visible": topup_visible, "giftcard_visible": giftcard_visible, "utility_visible": utility_visible}
        _feature_visibility_cache["data"] = data
        _feature_visibility_cache["expires"] = now + _PUBLIC_ENDPOINT_CACHE_TTL
        return jsonify(data)
    except Exception as e:
        logger.error(f"Feature visibility fetch error: {e}")
        return jsonify({"success": True, "swap_visible": True, "wallet_visible": True,
                        "savings_visible": True, "topup_visible": True, "giftcard_visible": True, "utility_visible": True})


@routes.route("/api/admin/feature-visibility", methods=["GET"])
@admin_required
def get_feature_visibility():
    """Admin: get current visibility settings for swap and wallet features"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500
        result = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')
                .select('feature_name,is_maintenance')
                .in_('feature_name', ['swap_feature', 'wallet_feature', 'savings_feature', 'store_topup', 'store_giftcard', 'store_utility'])
                .execute(),
            operation_name="get feature visibility admin"
        )
        swap_visible = True
        wallet_visible = True
        savings_visible = True
        topup_visible = True
        giftcard_visible = True
        utility_visible = True
        if result and result.data:
            for row in result.data:
                fn = row['feature_name']
                val = not row.get('is_maintenance', False)
                if fn == 'swap_feature':
                    swap_visible = val
                elif fn == 'wallet_feature':
                    wallet_visible = val
                elif fn == 'savings_feature':
                    savings_visible = val
                elif fn == 'store_topup':
                    topup_visible = val
                elif fn == 'store_giftcard':
                    giftcard_visible = val
                elif fn == 'store_utility':
                    utility_visible = val
        return jsonify({"success": True, "swap_visible": swap_visible, "wallet_visible": wallet_visible,
                        "savings_visible": savings_visible,
                        "topup_visible": topup_visible, "giftcard_visible": giftcard_visible, "utility_visible": utility_visible})
    except Exception as e:
        logger.error(f"Admin feature visibility fetch error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/feature-visibility", methods=["POST"])
@admin_required
def set_feature_visibility():
    """Admin: toggle visibility of swap or wallet feature"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500
        data = request.get_json()
        feature = data.get('feature')
        visible = data.get('visible', True)
        is_hidden = not visible
        admin_wallet = session.get('wallet')

        if feature not in ['swap_feature', 'wallet_feature', 'savings_feature', 'store_topup', 'store_giftcard', 'store_utility']:
            return jsonify({"success": False, "error": "Invalid feature name"}), 400

        existing = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')
                .select('feature_name')
                .eq('feature_name', feature)
                .execute(),
            operation_name="check feature visibility row"
        )

        if existing and existing.data:
            safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')
                    .update({'is_maintenance': is_hidden})
                    .eq('feature_name', feature)
                    .execute(),
                operation_name="update feature visibility"
            )
        else:
            safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')
                    .insert({'feature_name': feature, 'is_maintenance': is_hidden, 'maintenance_message': ''})
                    .execute(),
                operation_name="insert feature visibility"
            )

        log_admin_action(
            admin_wallet=admin_wallet,
            action_type="update_feature_visibility",
            action_details={"feature": feature, "visible": visible}
        )

        _feature_visibility_cache["data"] = None
        _feature_visibility_cache["expires"] = 0
        return jsonify({"success": True, "feature": feature, "visible": visible})
    except Exception as e:
        logger.error(f"Set feature visibility error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/")
def index():
    """Main homepage with Connect Wallet style"""
    # If already logged in, redirect to wallet
    if session.get("verified") and session.get("wallet"):
        return redirect("/wallet")
    wc_project_id = os.environ.get("WALLETCONNECT_PROJECT_ID", "")
    return render_template("homepage.html", walletconnect_project_id=wc_project_id)

@routes.route("/login")
def login_page():
    """Old login page - redirect to new homepage"""
    if session.get('verified') and session.get('wallet'):
        return redirect(url_for("routes.dashboard"))
    return redirect(url_for("routes.index"))

@routes.route("/login", methods=["POST"])
def login():
    """Legacy login endpoint - redirects to main page"""
    # This legacy login should ideally be updated or removed
    # For now, assuming it sets session['wallet'] and session['verified'] if needed
    # For the purpose of this edit, we assume session['wallet'] is set by other means if this is bypassed
    # If session['wallet'] is not set, the subsequent checks will handle redirection.
    return redirect(url_for("routes.index"))

@routes.route("/verify-ubi-page")
def verify_ubi_page():
    """Legacy verify page - redirects to main page"""
    return redirect(url_for("routes.index"))

def _disburse_referral_rewards(referral_blockchain_service, referral_service,
                               referrer_wallet, referee_wallet, referral_code):
    """
    Disburse referral rewards: 1000 G$ to referrer, 500 G$ to referee.
    If REFERRAL_KEY has insufficient funds, logs rewards as pending and marks
    referral as pending_disbursed for automatic retry when funds are added.
    """
    REFERRER_AMOUNT = 1000.0
    REFEREE_AMOUNT = 500.0

    referrer_result = referral_blockchain_service.disburse_referral_reward_sync(
        wallet_address=referrer_wallet,
        amount=REFERRER_AMOUNT,
        reward_type='referrer'
    )

    referee_result = referral_blockchain_service.disburse_referral_reward_sync(
        wallet_address=referee_wallet,
        amount=REFEREE_AMOUNT,
        reward_type='referee'
    )

    referrer_pending = referrer_result.get('pending', False) and not referrer_result.get('success')
    referee_pending = referee_result.get('pending', False) and not referee_result.get('success')
    any_pending = referrer_pending or referee_pending
    both_success = referrer_result.get('success') and referee_result.get('success')

    referral_service.log_reward(
        wallet_address=referrer_wallet,
        amount=REFERRER_AMOUNT,
        reward_type='referrer',
        referral_code=referral_code,
        tx_hash=referrer_result.get('tx_hash'),
        status='completed' if referrer_result.get('success') else 'pending'
    )

    referral_service.log_reward(
        wallet_address=referee_wallet,
        amount=REFEREE_AMOUNT,
        reward_type='referee',
        referral_code=referral_code,
        tx_hash=referee_result.get('tx_hash'),
        status='completed' if referee_result.get('success') else 'pending'
    )

    if both_success:
        referral_service.update_referral_status(referee_wallet, 'completed')
        referral_service.increment_referrer_stats(referrer_wallet, REFERRER_AMOUNT)
        logger.info(
            f"Referral rewards fully disbursed: {REFERRER_AMOUNT} G$ to {referrer_wallet[:8]}... "
            f"and {REFEREE_AMOUNT} G$ to {referee_wallet[:8]}..."
        )
    elif any_pending:
        referral_service.update_referral_status(referee_wallet, 'pending_disbursed',
                                                 error_message='Insufficient REFERRAL_KEY balance')
        logger.warning(
            f"Referral rewards pending (insufficient balance): "
            f"referrer={referrer_wallet[:8]}... referee={referee_wallet[:8]}..."
        )
    else:
        referral_service.update_referral_status(referee_wallet, 'failed',
                                                 error_message='Disbursement failed')
        logger.error(f"Referral reward disbursement failed for referee {referee_wallet[:8]}...")


@routes.route("/verify-ubi", methods=["POST"])
def verify_ubi():
    try:
        data = request.get_json()
        wallet_address = data.get("wallet", "").strip()
        referral_code = data.get("referral_code", None) # Get referral code from request
        track_analytics = data.get("track_analytics", False)

        if not wallet_address:
            return jsonify({"status": "error", "message": "⚠️ Wallet address required"}), 400

        # Normalize to EIP-55 checksum format so MetaMask, WalletConnect,
        # and manual paste all resolve to the SAME record in Supabase
        if Web3.is_address(wallet_address):
            try:
                wallet_address = Web3.to_checksum_address(wallet_address)
            except Exception:
                pass

        # UBI check temporarily disabled — allow all wallets in
        if True:
            # Track successful verification (for GoodMarket access)
            analytics.track_verification_attempt(wallet_address, True)
            analytics.track_user_session(wallet_address)

            # Store in session
            session["wallet"] = wallet_address
            session["verified"] = True

            # Check actual GoodDollar face verification status.
            # Even though we allow all wallets in, we still want to track
            # users who are NOT yet face-verified so we can count how many
            # eventually verify after discovering GoodMarket.
            fv_result = {'verified': False}
            try:
                from blockchain import is_identity_verified
                from supabase_client import supabase_logger
                fv_result = is_identity_verified(wallet_address)
                if not fv_result.get('verified', False) and supabase_logger:
                    supabase_logger.record_unverified_visit(wallet_address)
                    logger.info(f"📝 New unverified visitor recorded: {wallet_address[:8]}...")
                elif fv_result.get('verified', False):
                    logger.info(f"✅ User is already face-verified: {wallet_address[:8]}...")
                    # Mark face_verified + ubi_verified in user_data
                    if supabase_logger:
                        supabase_logger.log_verification_attempt(
                            wallet_address, success=True, face_verified=True
                        )
            except Exception as fv_check_err:
                logger.warning(f"⚠️ Could not check face verification status for tracking: {fv_check_err}")

            # Placeholder values since UBI check is skipped
            block_number = "N/A"
            claim_amount = "N/A"

            # Referral Program Processing
            # Rewards are ONLY disbursed when the referee (invited user) is face-verified.
            # Inviter (referrer) gets 1000 G$, Invited user (referee) gets 500 G$.
            # If REFERRAL_KEY runs out, rewards are marked pending_disbursed and auto-retried.
            try:
                from referral_program.referral_service import referral_service
                from referral_program.blockchain import referral_blockchain_service

                is_face_verified = fv_result.get('verified', False)

                # --- Case 1: New referral code provided ---
                if referral_code and referral_code.strip():
                    logger.info(f"Referral code provided: {referral_code} for {wallet_address[:8]}... face_verified={is_face_verified}")

                    validation = referral_service.validate_referral_code(referral_code.strip().upper())
                    if not validation.get('valid'):
                        logger.warning(f"Invalid referral code {referral_code}: {validation.get('error')}")
                    else:
                        referrer_wallet = validation['referrer_wallet']
                        record_result = referral_service.record_referral(
                            referral_code=referral_code.strip().upper(),
                            referee_wallet=wallet_address
                        )
                        if record_result.get('already_verified'):
                            logger.info(
                                f"Referral rejected (already verified externally): {wallet_address[:8]}... "
                                f"code={referral_code}"
                            )
                        elif record_result.get('success'):
                            logger.info(f"Referral recorded: {referral_code} | referrer={referrer_wallet[:8]}...")
                            if is_face_verified:
                                _disburse_referral_rewards(
                                    referral_blockchain_service, referral_service,
                                    referrer_wallet, wallet_address, referral_code.strip().upper()
                                )
                            else:
                                logger.info(
                                    f"Referee {wallet_address[:8]}... not yet face-verified. "
                                    f"Referral pending face verification."
                                )
                        elif record_result.get('already_exists'):
                            logger.info(f"Referral already recorded for {wallet_address[:8]}... checking if pending disbursement needed.")
                            if is_face_verified:
                                claimed = referral_service.claim_pending_referral_for_disbursement(wallet_address)
                                if claimed.get('claimed'):
                                    pending_row = claimed['referral']
                                    logger.info(
                                        f"Pending referral claimed for {wallet_address[:8]}... "
                                        f"disbursing now (code={pending_row['referral_code']})."
                                    )
                                    _disburse_referral_rewards(
                                        referral_blockchain_service, referral_service,
                                        pending_row['referrer_wallet'], wallet_address,
                                        pending_row['referral_code']
                                    )
                                else:
                                    logger.info(f"No pending referral to claim for {wallet_address[:8]}... (already completed, failed, or being processed).")
                        else:
                            logger.warning(f"Could not record referral: {record_result.get('error')}")

                # --- Case 2: No new code, but wallet has a pending referral and is now face-verified ---
                elif is_face_verified:
                    claimed = referral_service.claim_pending_referral_for_disbursement(wallet_address)
                    if claimed.get('claimed'):
                        ref_row = claimed['referral']
                        referrer_wallet = ref_row['referrer_wallet']
                        ref_code = ref_row['referral_code']
                        logger.info(
                            f"Referee {wallet_address[:8]}... is now face-verified. "
                            f"Disbursing pending referral rewards (code={ref_code})."
                        )
                        _disburse_referral_rewards(
                            referral_blockchain_service, referral_service,
                            referrer_wallet, wallet_address, ref_code
                        )

            except Exception as ref_error:
                logger.error(f"Referral processing error: {ref_error}")
                logger.exception("Referral error traceback:")

            # Set permanent session
            session.permanent = True

            return jsonify({
                'success': True,
                'status': 'success',
                'message': 'Identity verification successful!',
                'wallet': wallet_address,
                'ubi_verified': True,
                'redirect_to': '/wallet'
            })
        else:
            # Track failed verification
            analytics.track_verification_attempt(wallet_address, False)

            # Use the detailed message from blockchain.py
            error_message = result.get("message", "You need to claim G$ once every 24 hours to access GoodMarket.\n\nClaim G$ using:\n• MiniPay app (built into Opera Mini)\n• goodwallet.xyz\n• gooddapp.org")

            return jsonify({
                "status": "error",
                "message": error_message,
                "reason": "no_recent_claim",
                "help_links": {
                    "minipay": "https://www.opera.com/products/minipay",
                    "goodwallet": "https://goodwallet.xyz",
                    "gooddapp": "https://gooddapp.org"
                }
            }), 400

    except Exception as e:
        logger.exception("Verification error occurred")
        # Return custom message instead of generic error
        error_message = "You need to claim G$ once every 24 hours to access GoodMarket.\n\nClaim G$ using:\n• MiniPay app (built into Opera Mini)\n• goodwallet.xyz\n• gooddapp.org"
        return jsonify({
            "status": "error",
            "message": error_message,
            "reason": "verification_error"
        }), 500

@routes.route("/overview")
def overview():
    wallet = session.get('wallet') or session.get('wallet_address')
    verified = session.get('verified') or session.get('ubi_verified')
    username = None

    # Check if user has valid session (UBI check disabled)
    if wallet and verified:
        # Track overview page visit asynchronously (don't block render)
        import threading
        threading.Thread(target=analytics.track_page_view, args=(wallet, "overview"), daemon=True).start()

    # Get analytics - pass None for guest users, wallet for authenticated users
    stats = analytics.get_dashboard_stats(wallet if wallet and verified else None)

    # Debug logging
    logger.debug(f"🔍 Overview page - Wallet: {wallet[:8] if wallet else 'Guest'}...")
    logger.debug(f"🔍 Overview page - stats keys: {list(stats.keys())}")
    logger.debug(f"🔍 Overview page - disbursement_analytics present: {'disbursement_analytics' in stats}")
    if 'disbursement_analytics' in stats:
        logger.debug(f"🔍 Overview page - disbursement_analytics keys: {list(stats['disbursement_analytics'].keys())}")
        logger.debug(f"🔍 Overview page - breakdown_formatted present: {'breakdown_formatted' in stats['disbursement_analytics']}")

    import os as _os
    return render_template("overview.html",
                         wallet=wallet if wallet and verified else None,
                         data=stats,
                         login_method=session.get("login_method", "walletconnect"))

@routes.route("/dashboard")
def dashboard():
    """Dashboard page"""
    wallet = session.get('wallet') or session.get('wallet_address')
    verified = session.get('verified') or session.get('ubi_verified')

    if not wallet or not verified:
        return redirect(url_for("routes.index"))

    # Check on-chain Face Verification (GoodDollar Identity contract)
    try:
        from blockchain import is_identity_verified
        fv_result = is_identity_verified(wallet)
        if not fv_result.get("verified", False):
            return redirect(url_for("routes.wallet_page") + "?fv_required=1")
    except Exception as e:
        logger.warning(f"⚠️ Could not check FV status for dashboard access: {e}")

    # Track dashboard visit asynchronously (don't block render)
    import threading
    threading.Thread(target=analytics.track_page_view, args=(wallet, "dashboard"), daemon=True).start()

    import os as _os
    return render_template("dashboard.html", wallet=wallet)

@routes.route("/track-analytics", methods=["POST"])
def track_analytics_endpoint(): # Renamed to avoid conflict with analytics_service
    try:
        data = request.get_json()
        if not data:
            logger.error("❌ track-analytics: No JSON data received")
            return jsonify({"status": "error", "message": "No data provided"}), 400

        event = data.get("event")
        wallet = data.get("wallet")
        # Add username to track if available in request data
        username = data.get("username")

        logger.info(f"🔍 track-analytics: event='{event}', wallet='{wallet}', username='{username}'")

        if event and wallet:
            # Track page view (analytics.track_page_view only takes wallet and page)
            analytics.track_page_view(wallet, event)
            return jsonify({"status": "success"})

        missing = []
        if not event:
            missing.append("event")
        if not wallet:
            missing.append("wallet")

        error_msg = f"Missing required fields: {', '.join(missing)}"
        logger.error(f"❌ track-analytics: {error_msg}")
        return jsonify({"status": "error", "message": error_msg}), 400

    except Exception as e:
        logger.exception("❌ track-analytics error") # Use logger.exception for full traceback
        return jsonify({"status": "error", "message": str(e)}), 500

@routes.route("/ubi-tracker")
def ubi_tracker_page():
    if not session.get("verified") or not session.get("wallet"):
        return redirect(url_for("routes.index"))

    wallet = session.get("wallet")

    analytics.track_page_view(wallet, "ubi_tracker")

    return render_template("ubi_tracker.html",
                         wallet=wallet,
                         contract_count=len(GOODDOLLAR_CONTRACTS))

@routes.route("/logout")
def logout():
    wallet = session.get("wallet")
    if wallet:
        # Log logout to Supabase
        supabase_logger.log_logout(wallet)

    # Completely clear the session
    session.clear()

    # Create response with redirect
    response = redirect(url_for("routes.index"))

    # Clear all session cookies
    response.set_cookie('session', '', expires=0, path='/')
    response.set_cookie('wallet', '', expires=0, path='/')
    response.set_cookie('verified', '', expires=0, path='/')
    response.set_cookie('username', '', expires=0, path='/')

    # Add cache control headers to prevent caching
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'

    return response


@routes.route("/news")
def news_feed_page():
    wallet = session.get("wallet")

    # Track news page visit only for logged-in users
    if wallet and session.get("verified"):
        analytics.track_page_view(wallet, "news_feed")

    # Get news feed data for initial page load
    from news_feed import news_feed_service

    featured_news = news_feed_service.get_featured_news(limit=3)
    recent_news = news_feed_service.get_news_feed(limit=10)
    news_stats = news_feed_service.get_news_stats()

    return render_template("news_feed.html",
                         wallet=wallet,
                         featured_news=featured_news,
                         recent_news=recent_news,
                         news_stats=news_stats,
                         categories=news_feed_service.categories)

@routes.route('/news/article/<article_id>')
def news_article_page(article_id: str):
    """Individual news article page"""
    from news_feed import news_feed_service # Import moved here to avoid circular import issues if news_feed is used elsewhere before this route is called

    article = news_feed_service.get_news_article(article_id)

    if not article:
        # return render_template("404.html"), 404 # Assuming a 404 template exists
        return "Article not found", 404

    # Get the full article URL for sharing
    article_url = request.url

    # Prepare meta tags for social media sharing - this is now handled by passing article_url to the template
    # meta_tags = {
    #     "title": article.get('title', 'GoodDollar News'),
    #     "description": article.get('content', '')[:200], # Truncate description
    #     "image": article.get('image_url', ''),
    #     "url": article_url # Use the correctly constructed article URL
    # }

    # Add any additional session/wallet checks if this page requires authentication
    wallet = session.get("wallet")
    verified = session.get("verified")
    username = None
    if wallet and verified:
        # username = supabase_logger.get_username(wallet) # Username fetching moved to template rendering if needed
        analytics.track_page_view(wallet, f"news_article_{article_id}")

    return render_template("news_article.html",
                         article=article,
                         article_url=article_url, # Pass article_url to template
                         wallet=wallet if wallet and verified else None,
                         username=username if username else "Guest")


@routes.route("/api/admin/check", methods=["GET"])
@auth_required
def check_admin_status():
    """Check if current user is admin"""
    try:
        wallet = session.get("wallet")
        from supabase_client import is_admin

        is_admin_user = is_admin(wallet)

        return jsonify({
            "success": True,
            "is_admin": is_admin_user,
            "wallet": wallet[:8] + "..." if wallet else None
        })
    except Exception as e:
        logger.error(f"❌ Admin check error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/users", methods=["GET"])
@admin_required
def get_all_users():
    """Get all users (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        limit = int(request.args.get('limit', 100))
        offset = int(request.args.get('offset', 0))

        # Get users with pagination
        users = safe_supabase_operation(
            lambda: supabase.table('user_data')\
                .select('wallet_address, username, ubi_verified, total_logins, last_login, created_at')\
                .order('created_at', desc=True)\
                .range(offset, offset + limit - 1)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get all users"
        )

        return jsonify({
            "success": True,
            "users": users.data if users.data else [],
            "count": len(users.data) if users.data else 0
        })
    except Exception as e:
        logger.error(f"❌ Get users error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/stats", methods=["GET"])
@admin_required
def get_admin_stats():
    """Get platform statistics (admin only)"""
    try:
        from analytics_service import analytics

        # Get comprehensive platform stats using the correct method
        platform_stats = analytics.get_global_analytics()

        # Extract relevant stats for admin dashboard
        metrics = platform_stats.get("metrics", {})
        stats = {
            "total_users": metrics.get("total_users", 0),
            "verified_users": metrics.get("successful_verifications", 0),
            "total_page_views": platform_stats.get("user_activity", {}).get("total_page_views", 0),
            "verification_rate": platform_stats.get("verification_stats", {}).get("success_rate", "0%"),
            "goodmarket_verified_users": metrics.get("goodmarket_verified_users", 0),
            "pending_verification_users": metrics.get("pending_verification_users", 0),
            "goodmarket_conversion_rate": metrics.get("goodmarket_conversion_rate", "0%")
        }

        return jsonify({
            "success": True,
            "stats": stats
        })
    except Exception as e:
        logger.error(f"❌ Get admin stats error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/referral-stats", methods=["GET"])
@admin_required
def get_admin_referral_stats():
    """Get platform-wide referral statistics (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 500

        referrals_result = supabase.table('referrals').select('*').order('created_at', desc=True).execute()
        referrals = referrals_result.data if referrals_result else []

        rewards_result = supabase.table('referral_rewards_log').select('*').eq('status', 'completed').execute()
        rewards = rewards_result.data if rewards_result else []

        total = len(referrals)
        pending_fv = sum(1 for r in referrals if r.get('status') == 'pending_face_verification')
        pending_disbursed = sum(1 for r in referrals if r.get('status') == 'pending_disbursed')
        completed = sum(1 for r in referrals if r.get('status') == 'completed')
        failed = sum(1 for r in referrals if r.get('status') == 'failed')
        total_g_distributed = sum(float(r.get('reward_amount', 0)) for r in rewards)

        codes_result = supabase.table('referral_codes').select('referral_code, wallet_address, total_earned, created_at').order('total_earned', desc=True).limit(20).execute()
        top_referrers = codes_result.data if codes_result else []

        return jsonify({
            "success": True,
            "summary": {
                "total_referrals": total,
                "pending_face_verification": pending_fv,
                "pending_disbursed": pending_disbursed,
                "completed": completed,
                "failed": failed,
                "total_g_distributed": total_g_distributed
            },
            "recent_referrals": referrals[:50],
            "top_referrers": top_referrers
        })
    except Exception as e:
        logger.error(f"❌ Get admin referral stats error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/set-admin", methods=["POST"])
@admin_required
def set_user_admin_status():
    """Set admin status for a user (admin only)"""
    try:
        from supabase_client import set_admin_status, log_admin_action

        data = request.json
        target_wallet = data.get("wallet_address")
        is_admin_status = data.get("is_admin", False)

        if not target_wallet:
            return jsonify({"success": False, "error": "Wallet address required"}), 400

        admin_wallet = session.get("wallet")

        # Set admin status
        result = set_admin_status(target_wallet, is_admin_status)

        if result.get("success"):
            # Log admin action
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="set_admin_status",
                target_wallet=target_wallet,
                action_details={"is_admin": is_admin_status}
            )

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Set admin status error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/actions-log", methods=["GET"])
@admin_required
def get_admin_actions_log():
    """Get admin actions log (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))

        # Get admin actions with pagination
        actions = safe_supabase_operation(
            lambda: supabase.table('admin_actions_log')\
                .select('*')\
                .order('created_at', desc=True)\
                .range(offset, offset + limit - 1)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get admin actions log"
        )

        return jsonify({
            "success": True,
            "actions": actions.data if actions.data else [],
            "count": len(actions.data) if actions.data else 0
        })
    except Exception as e:
        logger.error(f"❌ Get admin actions log error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/reward-config", methods=["GET"])
@admin_required
def get_reward_config():
    """Get all reward configurations (admin only)"""
    try:
        from reward_config_service import reward_config_service

        result = reward_config_service.get_all_rewards()
        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Get reward config error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/reward-config", methods=["POST"])
@admin_required
def update_reward_config():
    """Update reward configuration (admin only)"""
    try:
        from reward_config_service import reward_config_service

        data = request.json
        task_type = data.get('task_type')
        new_amount = float(data.get('reward_amount', 0))
        admin_wallet = session.get('wallet')

        if not task_type or task_type not in ['telegram_task', 'twitter_task']:
            return jsonify({"success": False, "error": "Invalid task type"}), 400

        result = reward_config_service.update_reward_amount(task_type, new_amount, admin_wallet)

        if result.get('success'):
            # Log admin action
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="update_reward_config",
                action_details={
                    "task_type": task_type,
                    "new_amount": new_amount
                }
            )

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Update reward config error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500



@routes.route("/api/admin/quiz-questions", methods=["GET"])
@admin_required
def get_quiz_questions():
    """Get all quiz questions (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        logger.info("📚 Fetching quiz questions from Supabase 'quiz_questions' table...")

        # Get all quiz questions
        questions = safe_supabase_operation(
            lambda: supabase.table('quiz_questions')\
                .select('*')\
                .order('created_at', desc=True)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get quiz questions"
        )

        logger.info(f"✅ Retrieved {len(questions.data) if questions.data else 0} questions from Supabase")
        if questions.data and len(questions.data) > 0:
            logger.info(f"📝 Sample question: ID={questions.data[0].get('question_id')}, Question={questions.data[0].get('question')[:50]}...")

        return jsonify({
            "success": True,
            "questions": questions.data if questions.data else [],
            "count": len(questions.data) if questions.data else 0,
            "data_source": "supabase_quiz_questions_table"
        })
    except Exception as e:
        logger.error(f"❌ Get quiz questions error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/quiz-questions", methods=["POST"])
@admin_required
def add_quiz_question():
    """Add new quiz question (admin only)"""
    try:
        data = request.json

        # Validate required fields
        required_fields = ['question_id', 'question', 'answer_a', 'answer_b', 'answer_c', 'answer_d', 'correct']
        for field in required_fields:
            if not data.get(field):
                return jsonify({"success": False, "error": f"Missing required field: {field}"}), 400

        # Validate correct answer is A, B, C, or D
        if data['correct'].upper() not in ['A', 'B', 'C', 'D']:
            return jsonify({"success": False, "error": "Correct answer must be A, B, C, or D"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Check if question_id already exists
        existing = safe_supabase_operation(
            lambda: supabase.table('quiz_questions')\
                .select('question_id')\
                .eq('question_id', data['question_id'])\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="check question_id"
        )

        if existing.data and len(existing.data) > 0:
            return jsonify({"success": False, "error": "Question ID already exists"}), 400

        # Add new question
        from datetime import datetime
        question_data = {
            'question_id': data['question_id'],
            'question': data['question'],
            'answer_a': data['answer_a'],
            'answer_b': data['answer_b'],
            'answer_c': data['answer_c'],
            'answer_d': data['answer_d'],
            'correct': data['correct'].upper(),
            'created_at': datetime.utcnow().isoformat() + 'Z'
        }

        result = safe_supabase_operation(
            lambda: supabase.table('quiz_questions').insert(question_data).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="add quiz question"
        )

        if result.data:
            # Log admin action
            admin_wallet = session.get("wallet")
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="add_quiz_question",
                action_details={"question_id": data['question_id']}
            )

            logger.info(f"✅ Quiz question added: {data['question_id']}")
            return jsonify({"success": True, "question": result.data[0]})
        else:
            return jsonify({"success": False, "error": "Failed to add question"}), 500

    except Exception as e:
        logger.error(f"❌ Add quiz question error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/quiz-questions/<question_id>", methods=["PUT"])
@admin_required
def update_quiz_question(question_id):
    """Update quiz question (admin only)"""
    try:
        data = request.json

        # Validate correct answer if provided
        if 'correct' in data and data['correct'].upper() not in ['A', 'B', 'C', 'D']:
            return jsonify({"success": False, "error": "Correct answer must be A, B, C, or D"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Build update data
        update_data = {}
        allowed_fields = ['question', 'answer_a', 'answer_b', 'answer_c', 'answer_d', 'correct']
        for field in allowed_fields:
            if field in data:
                update_data[field] = data[field].upper() if field == 'correct' else data[field]

        if not update_data:
            return jsonify({"success": False, "error": "No valid fields to update"}), 400

        # Update question
        result = safe_supabase_operation(
            lambda: supabase.table('quiz_questions')\
                .update(update_data)\
                .eq('question_id', question_id)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="update quiz question"
        )

        if result.data:
            # Log admin action
            admin_wallet = session.get("wallet")
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="update_quiz_question",
                action_details={"question_id": question_id, "updated_fields": list(update_data.keys())}
            )

            logger.info(f"✅ Quiz question updated: {question_id}")
            return jsonify({"success": True, "question": result.data[0]})
        else:
            return jsonify({"success": False, "error": "Question not found"}), 404

    except Exception as e:
        logger.error(f"❌ Update quiz question error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/quiz-questions/<question_id>", methods=["DELETE"])
@admin_required
def delete_quiz_question(question_id):
    """Delete quiz question (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Delete question
        result = safe_supabase_operation(
            lambda: supabase.table('quiz_questions')\
                .delete()\
                .eq('question_id', question_id)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="delete quiz question"
        )

        if result.data:
            # Log admin action
            admin_wallet = session.get("wallet")
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="delete_quiz_question",
                action_details={"question_id": question_id}
            )

            logger.info(f"✅ Quiz question deleted: {question_id}")
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Question not found"}), 404

    except Exception as e:
        logger.error(f"❌ Delete quiz question error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/quiz-questions/delete-all", methods=["DELETE"])
@admin_required
def delete_all_quiz_questions():
    """Delete all quiz questions (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Get count of questions before deletion
        count_result = safe_supabase_operation(
            lambda: supabase.table('quiz_questions').select('quiz_id').execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="count quiz questions"
        )

        question_count = len(count_result.data) if count_result.data else 0

        if question_count == 0:
            return jsonify({"success": False, "error": "No questions to delete"}), 400

        # Delete all questions
        result = safe_supabase_operation(
            lambda: supabase.table('quiz_questions').delete().neq('quiz_id', 0).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="delete all quiz questions"
        )

        # Log admin action
        admin_wallet = session.get("wallet")
        log_admin_action(
            admin_wallet=admin_wallet,
            action_type="delete_all_quiz_questions",
            action_details={"deleted_count": question_count}
        )

        logger.info(f"✅ All quiz questions deleted: {question_count} questions")
        return jsonify({
            "success": True,
            "deleted_count": question_count
        })

    except Exception as e:
        logger.error(f"❌ Delete all quiz questions error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/broadcast-message", methods=["POST"])
@admin_required
def send_broadcast_message():
    """Send broadcast message to all users (admin only)"""
    try:
        data = request.json
        title = data.get('title', '').strip()
        message = data.get('message', '').strip()

        if not title or not message:
            return jsonify({"success": False, "error": "Title and message are required"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        admin_wallet = session.get("wallet")

        from datetime import datetime
        broadcast_data = {
            'title': title,
            'message': message,
            'sender_wallet': admin_wallet,
            'is_active': True,
            'created_at': datetime.utcnow().isoformat()
        }

        result = safe_supabase_operation(
            lambda: supabase.table('admin_broadcast_messages').insert(broadcast_data).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="send broadcast message"
        )

        if result.data:
            # Log admin action
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="send_broadcast_message",
                action_details={"title": title, "message_length": len(message)}
            )

            logger.info(f"✅ Broadcast message sent by admin {admin_wallet[:8]}...")
            return jsonify({
                "success": True,
                "message": "Broadcast message sent successfully!",
                "broadcast_id": result.data[0].get('id')
            })
        else:
            return jsonify({"success": False, "error": "Failed to send broadcast message"}), 500

    except Exception as e:
        logger.error(f"❌ Send broadcast message error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/broadcast-messages", methods=["GET"])
@admin_required
def get_broadcast_messages():
    """Get all broadcast messages (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        limit = int(request.args.get('limit', 50))

        messages = safe_supabase_operation(
            lambda: supabase.table('admin_broadcast_messages')\
                .select('*')\
                .order('created_at', desc=True)\
                .limit(limit)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get broadcast messages"
        )

        return jsonify({
            "success": True,
            "messages": messages.data if messages.data else [],
            "count": len(messages.data) if messages.data else 0
        })

    except Exception as e:
        logger.error(f"❌ Get broadcast messages error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/broadcast-message/<int:broadcast_id>", methods=["DELETE"])
@admin_required
def delete_broadcast_message(broadcast_id):
    """Delete/deactivate broadcast message (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Deactivate instead of delete
        result = safe_supabase_operation(
            lambda: supabase.table('admin_broadcast_messages')\
                .update({'is_active': False})\
                .eq('id', broadcast_id)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="deactivate broadcast message"
        )

        if result.data:
            admin_wallet = session.get("wallet")
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="delete_broadcast_message",
                action_details={"broadcast_id": broadcast_id}
            )

            logger.info(f"✅ Broadcast message {broadcast_id} deactivated")
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Message not found"}), 404

    except Exception as e:
        logger.error(f"❌ Delete broadcast message error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/news-history", methods=["GET"])
@admin_required
def get_news_history():
    """Get all news articles (admin only)"""
    try:
        from news_feed import news_feed_service

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Get all news articles
        news = safe_supabase_operation(
            lambda: supabase.table('news_articles')\
                .select('*')\
                .order('created_at', desc=True)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get all news articles"
        )

        return jsonify({
            "success": True,
            "news": news.data if news.data else [],
            "count": len(news.data) if news.data else 0
        })

    except Exception as e:
        logger.error(f"❌ Error getting news history: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ─── Featured Tweets (Community Stories Showcase) ───────────────────────────
import time as _time_mod
_featured_tweets_cache = {"data": None, "expires": 0}
FEATURED_TWEETS_CACHE_TTL = 30  # 30 seconds — fast reflect after admin adds tweet

@routes.route("/api/featured-tweets", methods=["GET"])
def get_featured_tweets():
    """Public — returns active featured tweets with in-memory cache."""
    global _featured_tweets_cache
    now = _time_mod.time()
    if _featured_tweets_cache["data"] is not None and now < _featured_tweets_cache["expires"]:
        return jsonify({"success": True, "tweets": _featured_tweets_cache["data"], "cached": True})
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": True, "tweets": [], "cached": False})
        result = safe_supabase_operation(
            lambda: supabase.table("community_tweet_showcases")
                .select("id, tweet_url, tweet_id, label, display_order")
                .eq("is_active", True)
                .order("display_order", desc=False)
                .execute(),
            fallback_result=type("r", (), {"data": []})(),
            operation_name="get featured tweets"
        )
        tweets = result.data or []
        _featured_tweets_cache = {"data": tweets, "expires": now + FEATURED_TWEETS_CACHE_TTL}
        return jsonify({"success": True, "tweets": tweets, "cached": False})
    except Exception as e:
        logger.error(f"❌ get_featured_tweets: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/featured-tweets", methods=["GET"])
@admin_required
def admin_get_featured_tweets():
    """Admin — list all featured tweets."""
    try:
        supabase = get_supabase_client()
        result = safe_supabase_operation(
            lambda: supabase.table("community_tweet_showcases")
                .select("*")
                .order("display_order", desc=False)
                .execute(),
            fallback_result=type("r", (), {"data": []})(),
            operation_name="admin get featured tweets"
        )
        return jsonify({"success": True, "tweets": result.data or []})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/featured-tweets", methods=["POST"])
@admin_required
def admin_add_featured_tweet():
    """Admin — add a new tweet link."""
    global _featured_tweets_cache
    try:
        import re as _re
        data = request.get_json() or {}
        tweet_url = (data.get("tweet_url") or "").strip()
        label = (data.get("label") or "").strip()
        display_order = int(data.get("display_order", 0))
        if not tweet_url:
            return jsonify({"success": False, "error": "tweet_url is required"}), 400
        match = _re.search(r"/status/(\d+)", tweet_url)
        tweet_id = match.group(1) if match else None
        supabase = get_supabase_client()
        wallet = session.get("wallet")
        result = safe_supabase_operation(
            lambda: supabase.table("community_tweet_showcases").insert({
                "tweet_url": tweet_url,
                "tweet_id": tweet_id,
                "label": label or None,
                "display_order": display_order,
                "is_active": True,
                "added_by": wallet
            }).execute(),
            fallback_result=None,
            operation_name="add featured tweet"
        )
        _featured_tweets_cache["data"] = None
        return jsonify({"success": True, "tweet": result.data[0] if result and result.data else {}})
    except Exception as e:
        logger.error(f"❌ admin_add_featured_tweet: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/featured-tweets/<int:ft_id>", methods=["DELETE"])
@admin_required
def admin_delete_featured_tweet(ft_id):
    """Admin — delete a tweet entry."""
    global _featured_tweets_cache
    try:
        supabase = get_supabase_client()
        safe_supabase_operation(
            lambda: supabase.table("community_tweet_showcases").delete().eq("id", ft_id).execute(),
            fallback_result=None,
            operation_name="delete featured tweet"
        )
        _featured_tweets_cache["data"] = None
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/featured-tweets/<int:ft_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_featured_tweet(ft_id):
    """Admin — toggle active status."""
    global _featured_tweets_cache
    try:
        data = request.get_json() or {}
        is_active = bool(data.get("is_active", True))
        supabase = get_supabase_client()
        safe_supabase_operation(
            lambda: supabase.table("community_tweet_showcases")
                .update({"is_active": is_active}).eq("id", ft_id).execute(),
            fallback_result=None,
            operation_name="toggle featured tweet"
        )
        _featured_tweets_cache["data"] = None
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
# ────────────────────────────────────────────────────────────────────────────

@routes.route("/api/admin/news/<int:news_id>", methods=["DELETE"])
@admin_required
def delete_news_article(news_id):
    """Delete news article (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Delete news article
        result = safe_supabase_operation(
            lambda: supabase.table('news_articles')\
                .delete()\
                .eq('id', news_id)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="delete news article"
        )

        if result.data:
            # Log admin action
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="delete_news_article",
                action_details={"news_id": news_id}
            )

            logger.info(f"✅ News article {news_id} deleted by admin {admin_wallet[:8]}...")
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "News article not found"}), 404

    except Exception as e:
        logger.error(f"❌ Error deleting news article: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/publish-news", methods=["POST"])
@admin_required
def publish_news_article():
    """Publish a news article (admin only)"""
    try:
        from news_feed import news_feed_service

        # Get form data
        title = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        category = request.form.get('category', 'announcement')
        priority = request.form.get('priority', 'medium')
        featured = request.form.get('featured') == 'true'
        url = request.form.get('url', '').strip()

        # Validate required fields
        if not title or not content:
            return jsonify({"success": False, "error": "Title and content are required"}), 400

        # Handle image upload if present
        image_url = None
        if 'image' in request.files:
            image_file = request.files['image']
            if image_file and image_file.filename:
                # Upload to ImgBB
                try:
                    import requests
                    import base64

                    imgbb_api_key = os.getenv('IMGBB_API_KEY')
                    if not imgbb_api_key:
                        logger.warning("⚠️ IMGBB_API_KEY not configured - skipping image upload")
                    else:
                        # Reset file pointer to beginning and read image
                        image_file.seek(0)
                        image_data = image_file.read()

                        # Validate image data
                        if not image_data or len(image_data) == 0:
                            logger.error("❌ Image file is empty")
                            return jsonify({"success": False, "error": "Image file is empty"}), 400

                        # Encode to base64
                        encoded_image = base64.b64encode(image_data).decode('utf-8')

                        logger.info(f"📤 Uploading image to ImgBB: {image_file.filename} ({len(image_data)} bytes)")

                        # Upload to ImgBB
                        imgbb_response = requests.post(
                            'https://api.imgbb.com/1/upload',
                            data={
                                'key': imgbb_api_key,
                                'image': encoded_image,
                                'name': f"news_{title[:30]}"
                            },
                            timeout=30
                        )

                        logger.info(f"📥 ImgBB Response: {imgbb_response.status_code}")

                        if imgbb_response.status_code == 200:
                            imgbb_data = imgbb_response.json()
                            if imgbb_data.get('success'):
                                image_url = imgbb_data['data']['url']
                                logger.info(f"✅ Image uploaded to ImgBB: {image_url}")
                            else:
                                error_msg = imgbb_data.get('error', {}).get('message', 'Unknown error')
                                logger.error(f"❌ ImgBB API error: {error_msg}")
                                return jsonify({"success": False, "error": f"Image upload failed: {error_msg}"}), 500
                        else:
                            logger.error(f"❌ ImgBB upload failed: {imgbb_response.status_code} - {imgbb_response.text[:500]}")
                            return jsonify({"success": False, "error": f"Image upload failed with status {imgbb_response.status_code}"}), 500

                except Exception as img_error:
                    logger.error(f"❌ Image upload error: {img_error}")
                    import traceback
                    logger.error(f"Traceback: {traceback.format_exc()}")
                    return jsonify({"success": False, "error": f"Image upload error: {str(img_error)}"}), 500

        # Get admin wallet
        admin_wallet = session.get("wallet")

        # Add news article
        result = news_feed_service.add_news_article(
            title=title,
            content=content,
            category=category,
            priority=priority,
            author=f"Admin ({admin_wallet[:8]}...)",
            featured=featured,
            image_url=image_url,
            url=url if url else None
        )

        if result.get('success'):
            # Log admin action
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="publish_news_article",
                action_details={
                    "title": title,
                    "category": category,
                    "featured": featured,
                    "has_image": bool(image_url)
                }
            )

            logger.info(f"✅ News article published: {title}")
            return jsonify({
                "success": True,
                "message": "News article published successfully!",
                "article": result.get('article')
            })
        else:
            return jsonify({
                "success": False,
                "error": result.get('error', 'Failed to publish article')
            }), 500

    except Exception as e:
        logger.error(f"❌ Publish news article error: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/learn-earn-sell-date", methods=["GET"])
@admin_required
def get_learn_earn_sell_date():
    """Get the current achievement card sell start date"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        result = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')\
                .select('custom_message')\
                .eq('feature_name', 'learn_earn_sell_date')\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get learn earn sell date"
        )

        sell_date = None
        if result.data and len(result.data) > 0:
            sell_date = result.data[0].get('custom_message')

        return jsonify({
            "success": True,
            "sell_date": sell_date or "2026-05-10"
        })
    except Exception as e:
        logger.error(f"❌ Error getting sell date: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/learn-earn-sell-date", methods=["POST"])
@admin_required
def set_learn_earn_sell_date():
    """Update the achievement card sell start date"""
    try:
        data = request.json
        sell_date = data.get('sell_date', '').strip()

        if not sell_date:
            return jsonify({"success": False, "error": "sell_date is required"}), 400

        from datetime import datetime
        try:
            datetime.strptime(sell_date, '%Y-%m-%d')
        except ValueError:
            return jsonify({"success": False, "error": "Invalid date format. Use YYYY-MM-DD"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        existing = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')\
                .select('id')\
                .eq('feature_name', 'learn_earn_sell_date')\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="check existing sell date"
        )

        if existing.data and len(existing.data) > 0:
            safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')\
                    .update({'custom_message': sell_date})\
                    .eq('feature_name', 'learn_earn_sell_date')\
                    .execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="update sell date"
            )
        else:
            safe_supabase_operation(
                lambda: supabase.table('maintenance_settings').insert({
                    'feature_name': 'learn_earn_sell_date',
                    'is_maintenance': False,
                    'custom_message': sell_date
                }).execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="insert sell date"
            )

        admin_wallet = session.get('wallet', 'unknown')
        logger.info(f"✅ Achievement card sell date updated to {sell_date} by admin {admin_wallet[:8]}...")

        return jsonify({
            "success": True,
            "sell_date": sell_date,
            "message": f"Sell date updated to {sell_date}"
        })
    except Exception as e:
        logger.error(f"❌ Error setting sell date: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/maintenance/learn-earn", methods=["GET"])
@admin_required
def get_learn_earn_maintenance():
    """Get Learn & Earn maintenance status"""
    try:
        from maintenance_service import maintenance_service

        status = maintenance_service.get_maintenance_status('learn_earn')
        return jsonify(status)
    except Exception as e:
        logger.error(f"❌ Error getting maintenance status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/maintenance/learn-earn", methods=["POST"])
@admin_required
def set_learn_earn_maintenance():
    """Set Learn & Earn maintenance status"""
    try:
        from maintenance_service import maintenance_service

        data = request.json
        is_maintenance = data.get('is_maintenance', False)
        message = data.get('message', '')
        admin_wallet = session.get('wallet')

        if is_maintenance and not message:
            return jsonify({
                "success": False,
                "error": "Custom message is required when enabling maintenance mode"
            }), 400

        result = maintenance_service.set_maintenance_status(
            'learn_earn',
            is_maintenance,
            message,
            admin_wallet
        )

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error setting maintenance status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/maintenance/minigames", methods=["GET"])
@admin_required
def get_minigames_maintenance():
    """Get Minigames maintenance status"""
    try:
        from maintenance_service import maintenance_service

        status = maintenance_service.get_maintenance_status('minigames')
        return jsonify(status)
    except Exception as e:
        logger.error(f"❌ Error getting minigames maintenance status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/maintenance/minigames", methods=["POST"])
@admin_required
def set_minigames_maintenance():
    """Set Minigames maintenance status"""
    try:
        from maintenance_service import maintenance_service

        data = request.json
        is_maintenance = data.get('is_maintenance', False)
        message = data.get('message', '')
        admin_wallet = session.get('wallet')

        if is_maintenance and not message:
            return jsonify({
                "success": False,
                "error": "Custom message is required when enabling maintenance mode"
            }), 400

        result = maintenance_service.set_maintenance_status(
            'minigames',
            is_maintenance,
            message,
            admin_wallet
        )

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error setting minigames maintenance status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/quiz-settings", methods=["GET"])
@admin_required
def get_quiz_settings():
    """Get current quiz settings"""
    try:
        from learn_and_earn.learn_and_earn import quiz_manager

        settings = quiz_manager.get_quiz_settings()
        return jsonify({
            "success": True,
            "settings": settings
        })
    except Exception as e:
        logger.error(f"❌ Error getting quiz settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/community-stories-settings", methods=["GET"])
@admin_required
def get_community_stories_settings():
    """Get Community Stories settings (admin only)"""
    try:
        from community_stories.community_stories_service import community_stories_service

        config = community_stories_service.get_config()

        # Get message from database
        supabase = get_supabase_client()
        message = None

        if supabase:
            result = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')\
                    .select('custom_message')\
                    .eq('feature_name', 'community_stories_message')\
                    .execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="get community stories message"
            )

            if result.data and len(result.data) > 0:
                message = result.data[0].get('custom_message')

        return jsonify({
            "success": True,
            "settings": {
                "low_reward": config['LOW_REWARD'],
                "high_reward": config['HIGH_REWARD'],
                "required_mentions": config['REQUIRED_MENTIONS'],
                "window_start_day": config['WINDOW_START_DAY'],
                "window_end_day": config['WINDOW_END_DAY'],
                "message": message or """💰 Earn G$ by sharing our story:
2,000 G$ - Text post on Twitter/X
5,000 G$ - Video post (min. 30 seconds)

📋 Requirements:
Must use hashtags: @gooddollarorg @GoodDollarTeam
Post must be public
Original content only

📅 Participation Schedule:
Opens: 26th of each month at 12:00 AM UTC
Closes: 30th of each month at 11:59 PM UTC
Duration: 5 days only each month
After reward: Blocked until next 26th

⚠️ Late submissions after 30th are NOT accepted!"""
            }
        })
    except Exception as e:
        logger.error(f"❌ Error getting Community Stories settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/community-stories-settings", methods=["POST"])
@admin_required
def update_community_stories_settings():
    """Update Community Stories settings (admin only)"""
    try:
        data = request.json
        low_reward = data.get('low_reward')
        high_reward = data.get('high_reward')
        required_mentions = data.get('required_mentions')
        window_start_day = data.get('window_start_day')
        window_end_day = data.get('window_end_day')
        message = data.get('message', '').strip()

        if not all([low_reward, high_reward, required_mentions, window_start_day, window_end_day]):
            return jsonify({"success": False, "error": "All fields are required"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Store settings in database using custom_message field for JSON data
        settings_json = json.dumps({
            'low_reward': float(low_reward),
            'high_reward': float(high_reward),
            'required_mentions': str(required_mentions),
            'window_start_day': int(window_start_day),
            'window_end_day': int(window_end_day)
        })

        settings_data = {
            'feature_name': 'community_stories_config',
            'is_maintenance': False,  # Use boolean field properly
            'custom_message': settings_json  # Store JSON in text field
        }

        # Check if exists
        existing = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')\
                .select('id')\
                .eq('feature_name', 'community_stories_config')\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="check community stories config"
        )

        if existing.data and len(existing.data) > 0:
            result = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')\
                    .update(settings_data)\
                    .eq('feature_name', 'community_stories_config')\
                    .execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="update community stories config"
            )
        else:
            result = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings').insert(settings_data).execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="insert community stories config"
            )

        # Store message separately
        if message:
            message_data = {
                'feature_name': 'community_stories_message',
                'is_maintenance': False,  # Use boolean field properly
                'custom_message': message  # Store message in text field
            }

            existing_msg = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')\
                    .select('id')\
                    .eq('feature_name', 'community_stories_message')\
                    .execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="check community stories message"
            )

            if existing_msg.data and len(existing_msg.data) > 0:
                safe_supabase_operation(
                    lambda: supabase.table('maintenance_settings')\
                        .update(message_data)\
                        .eq('feature_name', 'community_stories_message')\
                        .execute(),
                    fallback_result=type('obj', (object,), {'data': []})(),
                    operation_name="update community stories message"
                )
            else:
                safe_supabase_operation(
                    lambda: supabase.table('maintenance_settings').insert(message_data).execute(),
                    fallback_result=type('obj', (object,), {'data': []})(),
                    operation_name="insert community stories message"
                )

        if result.data:
            # Log admin action
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="update_community_stories_settings",
                action_details={
                    "low_reward": low_reward,
                    "high_reward": high_reward,
                    "window_start_day": window_start_day,
                    "window_end_day": window_end_day,
                    "message_updated": bool(message)
                }
            )

            logger.info(f"✅ Community Stories settings updated by admin {admin_wallet[:8]}...")
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to update settings"}), 500

    except Exception as e:
        logger.error(f"❌ Error updating Community Stories settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/insufficient-balance-message", methods=["GET"])
@admin_required
def get_insufficient_balance_message():
    """Get current insufficient balance error message"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Get message from maintenance_settings table
        result = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')\
                .select('custom_message')\
                .eq('feature_name', 'learn_earn_insufficient_balance')\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get insufficient balance message"
        )

        message = None
        if result.data and len(result.data) > 0:
            message = result.data[0].get('custom_message')

        return jsonify({
            "success": True,
            "message": message
        })
    except Exception as e:
        logger.error(f"❌ Error getting insufficient balance message: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/insufficient-balance-message", methods=["POST"])
@admin_required
def update_insufficient_balance_message():
    """Update insufficient balance error message"""
    try:
        data = request.json
        message = data.get('message', '').strip()

        if not message:
            return jsonify({
                "success": False,
                "error": "Message is required"
            }), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Check if record exists
        existing = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')\
                .select('id')\
                .eq('feature_name', 'learn_earn_insufficient_balance')\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="check existing message"
        )

        if existing.data and len(existing.data) > 0:
            # Update existing record
            result = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')\
                    .update({'custom_message': message})\
                    .eq('feature_name', 'learn_earn_insufficient_balance')\
                    .execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="update insufficient balance message"
            )
        else:
            # Insert new record
            from datetime import datetime
            result = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings').insert({
                    'feature_name': 'learn_earn_insufficient_balance',
                    'is_maintenance': False,
                    'custom_message': message,
                    'created_at': datetime.utcnow().isoformat()
                }).execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="insert insufficient balance message"
            )

        if result.data:
            # Log admin action
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="update_insufficient_balance_message",
                action_details={"message_length": len(message)}
            )

            logger.info(f"✅ Insufficient balance message updated by admin {admin_wallet[:8]}...")
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to update message"}), 500

    except Exception as e:
        logger.error(f"❌ Error updating insufficient balance message: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/quiz-settings", methods=["POST"])
@admin_required
def update_quiz_settings():
    """Update quiz settings"""
    try:
        from learn_and_earn.learn_and_earn import quiz_manager

        data = request.json
        questions_per_quiz = data.get('questions_per_quiz')
        time_per_question = data.get('time_per_question')
        max_reward_per_quiz = data.get('max_reward_per_quiz')

        # Validate inputs
        if questions_per_quiz is not None and (questions_per_quiz < 5 or questions_per_quiz > 30):
            return jsonify({
                "success": False,
                "error": "Questions per quiz must be between 5 and 30"
            }), 400

        if time_per_question is not None and (time_per_question < 10 or time_per_question > 60):
            return jsonify({
                "success": False,
                "error": "Time per question must be between 10 and 60 seconds"
            }), 400

        if max_reward_per_quiz is not None and (max_reward_per_quiz < 500 or max_reward_per_quiz > 10000):
            return jsonify({
                "success": False,
                "error": "Max reward must be between 500 and 10,000 G$"
            }), 400

        result = quiz_manager.update_quiz_settings(
            questions_per_quiz=questions_per_quiz,
            time_per_question=time_per_question,
            max_reward_per_quiz=max_reward_per_quiz
        )

        if result.get('success'):
            # Log admin action
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="update_quiz_settings",
                action_details={
                    "questions_per_quiz": questions_per_quiz,
                    "time_per_question": time_per_question,
                    "max_reward_per_quiz": max_reward_per_quiz
                }
            )

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error updating quiz settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/referral/my-code", methods=["GET"])
@auth_required
def get_my_referral_code():
    """Get or generate the current user's referral code.
    Checks user_data.my_referral_code first for a fast single-row lookup,
    then falls back to get_or_create_referral_code if not yet populated.
    """
    try:
        wallet = session.get('wallet')
        from referral_program.referral_service import referral_service, BASE_URL

        # Fast path: check user_data directly
        supabase = supabase_logger.client if supabase_logger and supabase_logger.enabled else None
        if supabase:
            ud = supabase.table('user_data') \
                .select('my_referral_code') \
                .ilike('wallet_address', wallet) \
                .limit(1) \
                .execute()
            if ud.data and ud.data[0].get('my_referral_code'):
                code = ud.data[0]['my_referral_code']
                return jsonify({
                    "success": True,
                    "referral_code": code,
                    "referral_link": f"{BASE_URL}/?ref={code}",
                    "source": "user_data"
                })

        # Slow path: generate/fetch from referral_codes table (also syncs back to user_data)
        result = referral_service.get_or_create_referral_code(wallet)
        if result.get('success'):
            result['source'] = 'referral_codes'
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error getting referral code: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/referral/stats", methods=["GET"])
@auth_required
def get_referral_stats():
    """Get referral program statistics for the current user."""
    try:
        wallet = session.get('wallet')
        from referral_program.referral_service import referral_service
        result = referral_service.get_referral_stats(wallet)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error getting referral stats: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/referral/process-pending", methods=["POST"])
@admin_required
def process_pending_referral_rewards():
    """Admin: attempt to disburse all pending referral rewards."""
    try:
        from referral_program.referral_service import referral_service
        result = referral_service.process_pending_disbursements()
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error processing pending referral rewards: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/referral/check/<referral_code>", methods=["GET"])
def check_referral_status(referral_code):
    """Check referral code status and history (for debugging)"""
    try:
        from referral_program.referral_service import referral_service

        # Validate code
        validation = referral_service.validate_referral_code(referral_code)

        # Get referrals using this code
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        referrals = safe_supabase_operation(
            lambda: supabase.table('referrals').select('*').eq('referral_code', referral_code).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get referrals by code"
        )

        rewards = safe_supabase_operation(
            lambda: supabase.table('referral_rewards_log').select('*').eq('referral_code', referral_code).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get rewards by code"
        )

        return jsonify({
            "success": True,
            "referral_code": referral_code,
            "validation": validation,
            "referrals": referrals.data if referrals.data else [],
            "rewards": rewards.data if rewards.data else [],
            "total_referrals": len(referrals.data) if referrals.data else 0,
            "total_rewards": len(rewards.data) if rewards.data else 0
        })
    except Exception as e:
        logger.error(f"❌ Error checking referral status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/daily-tasks/pending", methods=["GET"])
@admin_required
def get_pending_daily_tasks():
    """Get pending daily task submissions (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Get pending Telegram tasks
        telegram_pending = safe_supabase_operation(
            lambda: supabase.table('telegram_task_log')\
                .select('*')\
                .eq('status', 'pending')\
                .order('created_at', desc=False)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get pending telegram tasks"
        )

        # Get pending Twitter tasks
        twitter_pending = safe_supabase_operation(
            lambda: supabase.table('twitter_task_log')\
                .select('*')\
                .eq('status', 'pending')\
                .order('created_at', desc=False)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get pending twitter tasks"
        )

        # Get pending Telegram tasks
        telegram_pending = safe_supabase_operation(
            lambda: supabase.table('telegram_task_log')\
                .select('*')\
                .eq('status', 'pending')\
                .order('created_at', desc=False)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get pending telegram tasks"
        )

        # Get pending Twitter tasks
        twitter_pending = safe_supabase_operation(
            lambda: supabase.table('twitter_task_log')\
                .select('*')\
                .eq('status', 'pending')\
                .order('created_at', desc=False)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get pending twitter tasks"
        )

        telegram_tasks = []
        if telegram_pending.data:
            for task in telegram_pending.data:
                telegram_tasks.append({
                    'id': task.get('id'),
                    'wallet_address': task.get('wallet_address'),
                    'url': task.get('telegram_url'),
                    'reward_amount': task.get('reward_amount'),
                    'created_at': task.get('created_at'),
                    'platform': 'telegram'
                })

        twitter_tasks = []
        if twitter_pending.data:
            for task in twitter_pending.data:
                twitter_tasks.append({
                    'id': task.get('id'),
                    'wallet_address': task.get('wallet_address'),
                    'url': task.get('twitter_url'),
                    'reward_amount': task.get('reward_amount'),
                    'created_at': task.get('created_at'),
                    'platform': 'twitter'
                })

        return jsonify({
            "success": True,
            "telegram_tasks": telegram_tasks,
            "twitter_tasks": twitter_tasks,
            "total_pending": len(telegram_tasks) + len(twitter_tasks)
        })

    except Exception as e:
        logger.error(f"❌ Error getting pending tasks: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/daily-tasks/approve", methods=["POST"])
@admin_required
def approve_daily_task():
    """Approve a daily task submission (admin only)"""
    try:
        data = request.json
        submission_id = data.get('submission_id')
        platform = data.get('platform')  # 'telegram' or 'twitter' or 'facebook'
        admin_wallet = session.get('wallet')

        if not submission_id or not platform:
            return jsonify({"success": False, "error": "Missing required fields"}), 400

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            result = None
            if platform == 'telegram':
                from telegram_task.telegram_task import telegram_task_service
                result = loop.run_until_complete(
                    telegram_task_service.approve_submission(submission_id, admin_wallet)
                )
            elif platform == 'twitter':
                from twitter_task.twitter_task import twitter_task_service
                result = loop.run_until_complete(
                    twitter_task_service.approve_submission(submission_id, admin_wallet)
                )
            else:
                return jsonify({"success": False, "error": "Invalid platform"}), 400

            # Log admin action
            if result and result.get('success'):
                log_admin_action(
                    admin_wallet=admin_wallet,
                    action_type=f"approve_{platform}_task",
                    action_details={"submission_id": submission_id}
                )

            return jsonify(result) if result else jsonify({"success": False, "error": "Failed to process approval"}), 500
        finally:
            loop.close()

    except Exception as e:
        logger.error(f"❌ Error approving task: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/daily-tasks/reject", methods=["POST"])
@admin_required
def reject_daily_task():
    """Reject a daily task submission (admin only)"""
    try:
        data = request.json
        submission_id = data.get('submission_id')
        platform = data.get('platform')  # 'telegram' or 'twitter' or 'facebook'
        reason = data.get('reason', '')
        admin_wallet = session.get('wallet')

        if not submission_id or not platform:
            return jsonify({"success": False, "error": "Missing required fields"}), 400

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            result = None
            if platform == 'telegram':
                from telegram_task.telegram_task import telegram_task_service
                result = loop.run_until_complete(
                    telegram_task_service.reject_submission(submission_id, admin_wallet, reason)
                )
            elif platform == 'twitter':
                from twitter_task.twitter_task import twitter_task_service
                result = loop.run_until_complete(
                    twitter_task_service.reject_submission(submission_id, admin_wallet, reason)
                )
            else:
                return jsonify({"success": False, "error": "Invalid platform"}), 400

            # Log admin action
            if result and result.get('success'):
                log_admin_action(
                    admin_wallet=admin_wallet,
                    action_type=f"reject_{platform}_task",
                    action_details={"submission_id": submission_id, "reason": reason}
                )

            return jsonify(result) if result else jsonify({"success": False, "error": "Failed to process rejection"}), 500
        finally:
            loop.close()

    except Exception as e:
        logger.error(f"❌ Error rejecting task: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500

_bulk_jobs = {}

def _run_bulk_approve_job(job_id, tasks, delay_seconds, admin_wallet):
    """Background thread worker for bulk approve daily tasks"""
    import time as time_module
    import asyncio

    job = _bulk_jobs[job_id]
    job['status'] = 'running'

    for index, task in enumerate(tasks):
        submission_id = task.get('submission_id')
        platform = task.get('platform')

        if not submission_id or platform not in ['twitter', 'telegram']:
            job['results'].append({
                'submission_id': submission_id,
                'platform': platform,
                'success': False,
                'error': 'Invalid task data'
            })
            job['processed'] += 1
            continue

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                if platform == 'telegram':
                    from telegram_task.telegram_task import telegram_task_service
                    result = loop.run_until_complete(telegram_task_service.approve_submission(submission_id, admin_wallet))
                elif platform == 'twitter':
                    from twitter_task.twitter_task import twitter_task_service
                    result = loop.run_until_complete(twitter_task_service.approve_submission(submission_id, admin_wallet))
                else:
                    result = None
            finally:
                loop.close()

            if result and result.get('success'):
                log_admin_action(admin_wallet=admin_wallet, action_type=f"approve_{platform}_task", action_details={"submission_id": submission_id})

            job['results'].append({
                'submission_id': submission_id,
                'platform': platform,
                'success': result.get('success', False) if result else False,
                'tx_hash': result.get('tx_hash') if result else None,
                'error': result.get('error') if result else 'No result'
            })

        except Exception as e:
            logger.error(f"❌ Error bulk approving task {submission_id}: {e}")
            job['results'].append({
                'submission_id': submission_id,
                'platform': platform,
                'success': False,
                'error': str(e)
            })

        job['processed'] += 1

        if index < len(tasks) - 1:
            time_module.sleep(delay_seconds)

    job['status'] = 'done'
    succeeded = [r for r in job['results'] if r['success']]
    failed = [r for r in job['results'] if not r['success']]
    job['succeeded'] = len(succeeded)
    job['failed'] = len(failed)
    logger.info(f"📊 Bulk daily task approve [{job_id}]: {len(succeeded)} succeeded, {len(failed)} failed")


@routes.route("/api/admin/daily-tasks/bulk-approve", methods=["POST"])
@admin_required
def bulk_approve_daily_tasks():
    """Start bulk approve daily task submissions as a background job (admin only)"""
    try:
        import uuid, threading
        data = request.json
        tasks = data.get('tasks', [])
        delay_seconds = int(data.get('delay_seconds', 4))
        admin_wallet = session.get('wallet')

        if not tasks:
            return jsonify({"success": False, "error": "No tasks provided"}), 400

        if delay_seconds < 2:
            delay_seconds = 2
        if delay_seconds > 30:
            delay_seconds = 30

        job_id = str(uuid.uuid4())
        _bulk_jobs[job_id] = {
            'status': 'pending',
            'total': len(tasks),
            'processed': 0,
            'succeeded': 0,
            'failed': 0,
            'results': []
        }

        logger.info(f"📦 Admin {admin_wallet[:8]}... starting bulk approve job {job_id} for {len(tasks)} tasks")

        t = threading.Thread(
            target=_run_bulk_approve_job,
            args=(job_id, tasks, delay_seconds, admin_wallet),
            daemon=True
        )
        t.start()

        return jsonify({"success": True, "job_id": job_id, "total": len(tasks)})

    except Exception as e:
        logger.error(f"❌ Error starting bulk daily task approve: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/daily-tasks/bulk-status/<job_id>", methods=["GET"])
@admin_required
def bulk_approve_status(job_id):
    """Poll status of a background bulk approve job"""
    job = _bulk_jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404
    return jsonify({
        "success": True,
        "status": job['status'],
        "total": job['total'],
        "processed": job['processed'],
        "succeeded": job['succeeded'],
        "failed": job['failed'],
        "results": job['results']
    })

@routes.route("/api/admin/daily-tasks/bulk-reject", methods=["POST"])
@admin_required
def bulk_reject_daily_tasks():
    """Bulk reject daily task submissions (admin only)"""
    try:
        data = request.json
        tasks = data.get('tasks', [])  # list of {submission_id, platform}
        reason = data.get('reason', '')
        admin_wallet = session.get('wallet')

        if not tasks:
            return jsonify({"success": False, "error": "No tasks provided"}), 400

        logger.info(f"📦 Admin {admin_wallet[:8]}... bulk rejecting {len(tasks)} daily tasks")

        results = []

        for task in tasks:
            submission_id = task.get('submission_id')
            platform = task.get('platform')

            if not submission_id or platform not in ['twitter', 'telegram']:
                results.append({'submission_id': submission_id, 'platform': platform, 'success': False, 'error': 'Invalid task data'})
                continue

            try:
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    if platform == 'telegram':
                        from telegram_task.telegram_task import telegram_task_service
                        result = loop.run_until_complete(telegram_task_service.reject_submission(submission_id, admin_wallet, reason))
                    elif platform == 'twitter':
                        from twitter_task.twitter_task import twitter_task_service
                        result = loop.run_until_complete(twitter_task_service.reject_submission(submission_id, admin_wallet, reason))
                finally:
                    loop.close()

                if result and result.get('success'):
                    log_admin_action(admin_wallet=admin_wallet, action_type=f"reject_{platform}_task", action_details={"submission_id": submission_id, "reason": reason})

                results.append({
                    'submission_id': submission_id,
                    'platform': platform,
                    'success': result.get('success', False) if result else False,
                    'error': result.get('error') if result else 'No result'
                })

            except Exception as e:
                logger.error(f"❌ Error bulk rejecting task {submission_id}: {e}")
                results.append({'submission_id': submission_id, 'platform': platform, 'success': False, 'error': str(e)})

        succeeded = [r for r in results if r['success']]
        failed = [r for r in results if not r['success']]

        logger.info(f"📊 Bulk daily task reject: {len(succeeded)} succeeded, {len(failed)} failed")

        return jsonify({
            'success': True,
            'total': len(tasks),
            'succeeded': len(succeeded),
            'failed': len(failed),
            'results': results
        })

    except Exception as e:
        logger.error(f"❌ Error in bulk daily task reject: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/quiz-questions/upload", methods=["POST"])
@admin_required
def upload_quiz_questions():
    """Upload quiz questions from TXT file (admin only)"""
    try:
        if 'file' not in request.files:
            return jsonify({"success": False, "error": "No file uploaded"}), 400

        file = request.files['file']

        if file.filename == '':
            return jsonify({"success": False, "error": "No file selected"}), 400

        if not file.filename.endswith('.txt'):
            return jsonify({"success": False, "error": "File must be .txt format"}), 400

        # Read file content
        content = file.read().decode('utf-8')

        # Parse questions from TXT content
        questions = []
        current_question = {}
        parse_errors = []
        line_number = 0

        for line in content.split('\n'):
            line_number += 1
            line = line.strip()

            if not line:
                # Empty line - end of question
                if current_question:
                    # Check if all required fields are present
                    required_fields = ['question_id', 'question', 'answer_a', 'answer_b', 'answer_c', 'answer_d', 'correct']
                    missing_fields = [f for f in required_fields if f not in current_question]

                    if missing_fields:
                        parse_errors.append(f"Question at line ~{line_number}: Missing fields: {', '.join(missing_fields)}")
                    else:
                        questions.append(current_question)
                    current_question = {}
                continue

            if line.startswith('QUESTION_ID:'):
                current_question['question_id'] = line.replace('QUESTION_ID:', '').strip()
            elif line.startswith('QUESTION:'):
                current_question['question'] = line.replace('QUESTION:', '').strip()
            elif line.startswith('A)') or line.startswith('A:'):
                current_question['answer_a'] = line.replace('A)', '').replace('A:', '').strip()
            elif line.startswith('B)') or line.startswith('B:'):
                current_question['answer_b'] = line.replace('B)', '').replace('B:', '').strip()
            elif line.startswith('C)') or line.startswith('C:'):
                current_question['answer_c'] = line.replace('C)', '').replace('C:', '').strip()
            elif line.startswith('D)') or line.startswith('D:'):
                current_question['answer_d'] = line.replace('D)', '').replace('D:', '').strip()
            elif line.startswith('CORRECT:'):
                correct = line.replace('CORRECT:', '').strip().upper()
                if correct in ['A', 'B', 'C', 'D']:
                    current_question['correct'] = correct
                else:
                    parse_errors.append(f"Line {line_number}: Invalid correct answer '{correct}'. Must be A, B, C, or D")

        # Add last question if exists
        if current_question:
            required_fields = ['question_id', 'question', 'answer_a', 'answer_b', 'answer_c', 'answer_d', 'correct']
            missing_fields = [f for f in required_fields if f not in current_question]

            if missing_fields:
                parse_errors.append(f"Last question: Missing fields: {', '.join(missing_fields)}")
            else:
                questions.append(current_question)

        if not questions:
            example_format = """
Expected format (each question must have ALL fields):

QUESTION_ID: Q001
QUESTION: What is GoodDollar?
A: A cryptocurrency for UBI
B: A bank
C: A credit card
D: A website
CORRECT: A

(Empty line between questions)

QUESTION_ID: Q002
QUESTION: How often can you claim UBI?
A: Monthly
B: Daily
C: Yearly
D: Once
CORRECT: B
"""
            error_msg = "No valid questions found in file."
            if parse_errors:
                error_msg += f" Errors found: {'; '.join(parse_errors[:3])}"
            error_msg += f" Please check file format. {example_format}"

            return jsonify({
                "success": False,
                "error": error_msg,
                "parse_errors": parse_errors,
                "example_format": example_format
            }), 400

        # Insert questions into database
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        added_count = 0
        skipped_count = 0
        error_count = 0
        error_details = []

        admin_wallet = session.get("wallet")

        for q in questions:
            try:
                # Check if question_id already exists
                existing = safe_supabase_operation(
                    lambda: supabase.table('quiz_questions')\
                        .select('question_id')\
                        .eq('question_id', q['question_id'])\
                        .execute(),
                    fallback_result=type('obj', (object,), {'data': []})(),
                    operation_name="check question exists"
                )

                if existing.data and len(existing.data) > 0:
                    skipped_count += 1
                    logger.info(f"⚠️ Skipped duplicate question: {q['question_id']}")
                    continue

                # Add created_at timestamp
                from datetime import datetime
                q['created_at'] = datetime.utcnow().isoformat() + 'Z'

                # Insert question
                result = safe_supabase_operation(
                    lambda: supabase.table('quiz_questions').insert(q).execute(),
                    fallback_result=type('obj', (object,), {'data': []})(),
                    operation_name="insert question from file"
                )

                if result.data:
                    added_count += 1
                    logger.info(f"✅ Added question from file: {q['question_id']}")
                else:
                    error_count += 1
                    error_details.append(f"Failed to add {q['question_id']}")

            except Exception as e:
                error_count += 1
                error_details.append(f"{q.get('question_id', 'unknown')}: {str(e)}")
                logger.error(f"❌ Error adding question {q.get('question_id', 'unknown')}: {e}")

        # Log admin action
        log_admin_action(
            admin_wallet=admin_wallet,
            action_type="upload_quiz_questions",
            action_details={
                "total_questions": len(questions),
                "added": added_count,
                "skipped": skipped_count,
                "errors": error_count
            }
        )

        logger.info(f"✅ Quiz upload complete: {added_count} added, {skipped_count} skipped, {error_count} errors")

        return jsonify({
            "success": True,
            "total": len(questions),
            "added": added_count,
            "skipped": skipped_count,
            "errors": error_count,
            "error_details": error_details[:10]  # Limit to first 10 errors
        })

    except Exception as e:
        logger.error(f"❌ Upload quiz questions error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/module-links", methods=["GET"])
@admin_required
def get_module_links():
    """Get all module links (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Get all module links
        links = safe_supabase_operation(
            lambda: supabase.table('learn_earn_module_links')\
                .select('*')\
                .order('display_order', desc=False)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get module links"
        )

        return jsonify({
            "success": True,
            "links": links.data if links.data else [],
            "count": len(links.data) if links.data else 0
        })
    except Exception as e:
        logger.error(f"❌ Get module links error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/module-links", methods=["POST"])
@admin_required
def add_module_link():
    """Add new module link (admin only) - supports auto-scraping from URL"""
    try:
        data = request.json
        title = data.get('title', '').strip()
        url = data.get('url', '').strip()
        description = data.get('description', '').strip()
        content = data.get('content', '').strip()
        reading_time_minutes = int(data.get('reading_time_minutes', 5))
        display_order = int(data.get('display_order', 1))

        if not title:
            return jsonify({"success": False, "error": "Title is required"}), 400

        # Auto-scrape content from URL if no content provided - ALWAYS ENABLED
        scrape_warning = None
        if url and not content:
            logger.info(f"🔍 🤖 AUTO-SCRAPING ENABLED - Fetching content from URL: {url}")
            try:
                import requests
                import json as json_lib
                import re as re_lib
                from bs4 import BeautifulSoup

                scraped_html = ""
                is_medium = 'medium.com' in url

                # --- Medium-specific: use hidden JSON API ---
                if is_medium:
                    logger.info(f"📰 Medium URL detected — using Medium JSON API")
                    # Strip query params and append ?format=json
                    base_url = url.split('?')[0].rstrip('/')
                    json_url = base_url + '?format=json'
                    medium_headers = {
                        'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
                        'Accept': 'application/json, text/plain, */*',
                        'Accept-Language': 'en-US,en;q=0.9',
                        'Referer': 'https://medium.com/',
                    }
                    try:
                        json_resp = requests.get(json_url, timeout=15, headers=medium_headers, allow_redirects=True)
                        json_resp.raise_for_status()
                        # Medium prepends '])}while(1);</x>' as XSSI protection — strip it
                        raw = json_resp.text
                        json_start = raw.find('{')
                        if json_start != -1:
                            data = json_lib.loads(raw[json_start:])
                            # Navigate to paragraphs inside the post payload
                            payload = data.get('payload', {})
                            post_value = payload.get('value', {})
                            paragraphs = post_value.get('content', {}).get('bodyModel', {}).get('paragraphs', [])
                            if not paragraphs:
                                # Try alternative path
                                post_map = payload.get('references', {}).get('Post', {})
                                if post_map:
                                    first_post = list(post_map.values())[0]
                                    paragraphs = first_post.get('content', {}).get('bodyModel', {}).get('paragraphs', [])

                            if paragraphs:
                                logger.info(f"✅ Medium JSON API returned {len(paragraphs)} paragraphs")
                                # paragraph types: 1=p, 3=h1, 13=h2/h3, 6=blockquote, 8=ul/ol, 9=li
                                TYPE_MAP = {1: 'p', 3: 'h2', 13: 'h3', 6: 'blockquote', 4: 'h3'}
                                in_list = False
                                for para in paragraphs:
                                    ptype = para.get('type', 1)
                                    text = para.get('text', '').strip()
                                    if not text:
                                        continue
                                    if ptype in (8, 9):  # list item
                                        if not in_list:
                                            scraped_html += "<ul>\n"
                                            in_list = True
                                        scraped_html += f"<li>{text}</li>\n"
                                    else:
                                        if in_list:
                                            scraped_html += "</ul>\n"
                                            in_list = False
                                        tag = TYPE_MAP.get(ptype, 'p')
                                        scraped_html += f"<{tag}>{text}</{tag}>\n"
                                if in_list:
                                    scraped_html += "</ul>\n"
                        logger.info(f"📊 Medium JSON scrape: {len(scraped_html)} chars extracted")
                    except Exception as medium_err:
                        logger.warning(f"⚠️ Medium JSON API failed ({medium_err}), trying RSS feed...")

                    # Fallback: try Medium RSS feed for this publication
                    if not scraped_html:
                        try:
                            # Extract publication slug from URL
                            # e.g. medium.com/gooddollar/article-slug -> feed: medium.com/feed/gooddollar
                            url_parts = url.replace('https://', '').replace('http://', '').split('/')
                            # url_parts[0] = medium.com, [1] = pub or @user, [2] = article-slug
                            if len(url_parts) >= 3:
                                pub = url_parts[1]  # e.g. 'gooddollar' or '@username'
                                article_slug = url_parts[2].split('?')[0]
                                rss_url = f"https://medium.com/feed/{pub}"
                                logger.info(f"📡 Trying RSS feed: {rss_url}")
                                rss_resp = requests.get(rss_url, timeout=15, headers=medium_headers)
                                if rss_resp.status_code == 200:
                                    rss_soup = BeautifulSoup(rss_resp.content, 'xml')
                                    items = rss_soup.find_all('item')
                                    for item in items:
                                        item_link = item.find('link')
                                        link_text = item_link.get_text() if item_link else (item_link.next_sibling if item_link else '')
                                        if article_slug in str(link_text):
                                            content_encoded = item.find('content:encoded') or item.find('description')
                                            if content_encoded:
                                                article_soup = BeautifulSoup(content_encoded.get_text(), 'html.parser')
                                                for el in article_soup(['script', 'style', 'figure', 'img']):
                                                    el.decompose()
                                                for element in article_soup.find_all(['h1', 'h2', 'h3', 'p', 'ul', 'ol']):
                                                    if element.name in ('h1', 'h2'):
                                                        scraped_html += f"<h2>{element.get_text().strip()}</h2>\n"
                                                    elif element.name == 'h3':
                                                        scraped_html += f"<h3>{element.get_text().strip()}</h3>\n"
                                                    elif element.name == 'p':
                                                        t = element.get_text().strip()
                                                        if t:
                                                            scraped_html += f"<p>{t}</p>\n"
                                                    elif element.name in ('ul', 'ol'):
                                                        tag = element.name
                                                        scraped_html += f"<{tag}>\n"
                                                        for li in element.find_all('li', recursive=False):
                                                            scraped_html += f"<li>{li.get_text().strip()}</li>\n"
                                                        scraped_html += f"</{tag}>\n"
                                                logger.info(f"✅ RSS feed scrape: {len(scraped_html)} chars")
                                            break
                        except Exception as rss_err:
                            logger.warning(f"⚠️ RSS fallback failed: {rss_err}")

                # --- Generic scrape for non-Medium URLs ---
                if not scraped_html:
                    logger.info(f"📥 Downloading webpage (generic scraper)...")
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                        'Accept-Language': 'en-US,en;q=0.9',
                        'Accept-Encoding': 'gzip, deflate, br',
                        'DNT': '1',
                        'Connection': 'keep-alive',
                        'Upgrade-Insecure-Requests': '1',
                        'Cache-Control': 'max-age=0',
                    }
                    response = requests.get(url, timeout=15, headers=headers, allow_redirects=True)
                    response.raise_for_status()
                    logger.info(f"✅ Webpage downloaded ({len(response.content)} bytes)")

                    soup = BeautifulSoup(response.content, 'html.parser')
                    for element in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe', 'noscript']):
                        element.decompose()

                    main_content = (
                        soup.find('article') or
                        soup.find('main') or
                        soup.find('div', class_='content') or
                        soup.find('div', class_='article') or
                        soup.find('body')
                    )

                    if main_content:
                        logger.info(f"📄 Found content container: {main_content.name}")
                        for element in main_content.find_all(['h1', 'h2', 'h3', 'p', 'ul', 'ol']):
                            if element.name == 'h1':
                                scraped_html += f"<h2>{element.get_text().strip()}</h2>\n"
                            elif element.name in ('h2', 'h3'):
                                scraped_html += f"<h3>{element.get_text().strip()}</h3>\n"
                            elif element.name == 'p':
                                text = element.get_text().strip()
                                if text:
                                    scraped_html += f"<p>{text}</p>\n"
                            elif element.name in ('ul', 'ol'):
                                tag = element.name
                                scraped_html += f"<{tag}>\n"
                                for li in element.find_all('li', recursive=False):
                                    scraped_html += f"<li>{li.get_text().strip()}</li>\n"
                                scraped_html += f"</{tag}>\n"

                if scraped_html.strip():
                    content = scraped_html.strip()
                    word_count = len(content.split())
                    reading_time_minutes = max(1, round(word_count / 200))
                    logger.info(f"✅ AUTO-SCRAPE SUCCESSFUL! {len(content)} chars, ~{reading_time_minutes} min read")
                else:
                    logger.warning(f"⚠️ Could not extract content from {url}")

            except Exception as scrape_error:
                logger.error(f"❌ Auto-scrape error: {scrape_error}")
                import traceback
                logger.error(f"🔍 Traceback: {traceback.format_exc()}")

                # Set warning but continue — do NOT block saving the link
                error_msg = str(scrape_error)
                if "403" in error_msg or "Forbidden" in error_msg:
                    scrape_warning = "Website blocked auto-scraping (403 Forbidden). The link was saved — please edit it to add content manually."
                elif "404" in error_msg:
                    scrape_warning = "Page not found (404). The link was saved — please check the URL and add content manually."
                elif "timeout" in error_msg.lower():
                    scrape_warning = "Request timed out. The link was saved — please edit it to add content manually."
                else:
                    scrape_warning = f"Could not auto-scrape content: {error_msg}. The link was saved — please edit it to add content manually."

        # Allow saving even without content (admin can edit later)
        if not content and not scrape_warning:
            return jsonify({"success": False, "error": "Content is required (or provide a URL for auto-scraping)"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        from datetime import datetime
        link_data = {
            'title': title,
            'url': url,
            'description': description,
            'content': content,
            'reading_time_minutes': reading_time_minutes,
            'display_order': display_order,
            'is_active': True,
            'created_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat()
        }

        result = safe_supabase_operation(
            lambda: supabase.table('learn_earn_module_links').insert(link_data).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="add module link"
        )

        if result.data:
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="add_module_link",
                action_details={"title": title, "url": url}
            )

            logger.info(f"✅ Module link added: {title}")
            response_data = {"success": True, "link": result.data[0]}
            if scrape_warning:
                response_data["warning"] = scrape_warning
            return jsonify(response_data)
        else:
            return jsonify({"success": False, "error": "Failed to add module link"}), 500

    except Exception as e:
        logger.error(f"❌ Add module link error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/module-links/<int:link_id>", methods=["PUT"])
@admin_required
def update_module_link(link_id):
    """Update module link (admin only)"""
    try:
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        from datetime import datetime
        update_data = {}

        if 'title' in data:
            update_data['title'] = data['title'].strip()
        if 'url' in data:
            update_data['url'] = data['url'].strip()
        if 'description' in data:
            update_data['description'] = data['description'].strip()
        if 'content' in data:
            update_data['content'] = data['content'].strip()
        if 'reading_time_minutes' in data:
            update_data['reading_time_minutes'] = int(data['reading_time_minutes'])
        if 'display_order' in data:
            update_data['display_order'] = int(data['display_order'])
        if 'is_active' in data:
            update_data['is_active'] = data['is_active'] in [True, 'true', '1', 1]

        update_data['updated_at'] = datetime.utcnow().isoformat()

        result = safe_supabase_operation(
            lambda: supabase.table('learn_earn_module_links')\
                .update(update_data)\
                .eq('id', link_id)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="update module link"
        )

        if result.data:
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="update_module_link",
                action_details={"link_id": link_id, "updated_fields": list(update_data.keys())}
            )

            logger.info(f"✅ Module link {link_id} updated")
            return jsonify({"success": True, "link": result.data[0]})
        else:
            return jsonify({"success": False, "error": "Link not found"}), 404

    except Exception as e:
        logger.error(f"❌ Update module link error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/module-links/<int:link_id>", methods=["DELETE"])
@admin_required
def delete_module_link(link_id):
    """Delete module link (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        result = safe_supabase_operation(
            lambda: supabase.table('learn_earn_module_links')\
                .delete()\
                .eq('id', link_id)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="delete module link"
        )

        if result.data:
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="delete_module_link",
                action_details={"link_id": link_id}
            )

            logger.info(f"✅ Module link {link_id} deleted")
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Link not found"}), 404

    except Exception as e:
        logger.error(f"❌ Delete module link error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/admin")
@auth_required
def admin_dashboard():
    """Admin dashboard page"""
    wallet = session.get("wallet")

    from supabase_client import is_admin
    if not is_admin(wallet):
        logger.warning(f"⚠️ Non-admin access attempt from {wallet[:8]}...")
        return redirect("/dashboard")

    logger.info(f"✅ Admin access granted to {wallet[:8]}...")

    response = make_response(render_template("admin_dashboard.html", wallet=wallet))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


    return render_template("forum_post_detail.html",
                         wallet=wallet,
                         username=username, # Pass username to template
                         post=post,
                         categories=community_forum_service.categories)

@routes.route("/learn-earn")
def learn_earn_page():
    if not session.get("verified") or not session.get("wallet"):
        return redirect(url_for("routes.index"))

    wallet = session.get("wallet")

    # Track Learn & Earn page visit
    analytics.track_page_view(wallet, "learn_earn")

    return render_template("learn_and_earn.html",
                         wallet=wallet,
                         login_method=session.get("login_method", "walletconnect"))

# Username functionality removed


@routes.route('/api/p2p/history')
def get_p2p_history_api():
    """P2P trading has been removed - return empty history"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            return jsonify({"success": False, "error": "Not authenticated"}), 401

        logger.info(f"📋 P2P trading disabled - returning empty history for {wallet[:8]}...")

        return jsonify({
            "success": True,
            "trades": [],
            "total": 0,
            "message": "P2P trading feature has been disabled"
        })

    except Exception as e:
        logger.error(f"❌ Error in P2P history endpoint: {e}")
        return jsonify({
            "success": False,
            "error": str(e),
            "trades": [],
            "total": 0
        }), 500

@routes.route("/api/admin/community-stories-notifications", methods=["GET"])
@admin_required
def get_admin_notifications():
    """Get pending submissions for admin"""
    try:
        wallet = session.get("wallet")

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Get pending submissions directly to include storage_path
        pending = safe_supabase_operation(
            lambda: supabase.table('community_stories_submissions')\
                .select('*')\
                .eq('status', 'pending')\
                .order('submitted_at', desc=True)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get pending community stories"
        )

        # Format for admin display
        notifications = []
        if pending.data:
            for sub in pending.data:
                notifications.append({
                    'submission_id': sub.get('submission_id'),
                    'community_stories_submissions': sub
                })

        return jsonify({
            "success": True,
            "notifications": notifications,
            "count": len(notifications)
        })

    except Exception as e:
        logger.error(f"❌ Error getting admin notifications: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/developer-profile", methods=["POST"])
@admin_required
def upload_developer_profile():
    """Upload developer profile image (admin only) - supports multiple profiles"""
    try:
        from object_storage_client import upload_to_imgbb

        if 'image' not in request.files:
            return jsonify({"success": False, "error": "No image file provided"}), 400

        image_file = request.files['image']
        name = request.form.get('name', '').strip()
        position = request.form.get('position', '').strip()

        if not name or not position:
            return jsonify({"success": False, "error": "Name and position are required"}), 400

        # Upload to ImgBB
        upload_result = upload_to_imgbb(image_file)

        if not upload_result.get('success'):
            return jsonify({"success": False, "error": upload_result.get('error', 'Upload failed')}), 500

        image_url = upload_result.get('url')

        # Store in database
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        from datetime import datetime
        profile_data = {
            'name': name,
            'position': position,
            'image_url': image_url,
            'is_active': True,
            'created_at': datetime.utcnow().isoformat()
        }

        # Always insert new profile (allows multiple developers)
        result = safe_supabase_operation(
            lambda: supabase.table('developer_profile').insert(profile_data).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="insert developer profile"
        )

        if result.data:
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="upload_developer_profile",
                action_details={"name": name, "position": position}
            )

            logger.info(f"✅ Developer profile uploaded: {name}")
            return jsonify({"success": True, "profile": result.data[0]})
        else:
            return jsonify({"success": False, "error": "Failed to save profile"}), 500

    except Exception as e:
        logger.error(f"❌ Upload developer profile error: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/developer-profile", methods=["GET"])
def get_developer_profile():
    """Get all active developer profiles for homepage"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "profiles": []})

        result = safe_supabase_operation(
            lambda: supabase.table('developer_profile')\
                .select('*')\
                .eq('is_active', True)\
                .order('created_at', desc=False)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get developer profiles"
        )

        profiles = result.data if result.data else []

        return jsonify({
            "success": True,
            "profiles": profiles,
            "count": len(profiles)
        })

    except Exception as e:
        logger.error(f"❌ Get developer profiles error: {e}")
        return jsonify({"success": False, "profiles": []})


# ============================================================
# DISCOURSE TASK ROUTES
# ============================================================

@routes.route("/api/discourse-task/settings", methods=["GET"])
def get_discourse_task_settings():
    """Get discourse task settings and current user status"""
    try:
        wallet = session.get('wallet')
        if not wallet:
            return jsonify({"success": False, "error": "Not authenticated"}), 401
        from discourse_task.discourse_task import discourse_task_service
        settings = discourse_task_service.get_settings()
        current_link = settings.get('link')
        user_status = discourse_task_service.get_user_status(wallet, current_link)
        return jsonify({
            "success": True,
            "link": current_link,
            "reward_amount": settings.get('reward_amount'),
            "user_status": user_status.get('status'),
            "discourse_username": user_status.get('discourse_username'),
            "submitted_at": user_status.get('submitted_at'),
            "tx_hash": user_status.get('tx_hash')
        })
    except Exception as e:
        logger.error(f"❌ Error getting discourse task settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/discourse-task/submit", methods=["POST"])
def submit_discourse_username():
    """Submit discourse username for approval"""
    try:
        wallet = session.get('wallet')
        if not wallet:
            return jsonify({"success": False, "error": "Not authenticated"}), 401
        from discourse_task.discourse_task import discourse_task_service
        data = request.json
        discourse_username = data.get('discourse_username', '').strip()
        discourse_link = data.get('discourse_link', '').strip()

        if not discourse_username:
            return jsonify({"success": False, "error": "Discourse username is required"}), 400

        result = discourse_task_service.submit_username(wallet, discourse_username, discourse_link or None)
        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error submitting discourse username: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/discourse-task/settings", methods=["GET"])
@admin_required
def admin_get_discourse_settings():
    """Admin: Get discourse task settings"""
    try:
        from discourse_task.discourse_task import discourse_task_service
        settings = discourse_task_service.get_settings()
        return jsonify(settings)
    except Exception as e:
        logger.error(f"❌ Error getting discourse admin settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/discourse-task/settings", methods=["POST"])
@admin_required
def admin_update_discourse_settings():
    """Admin: Update discourse task settings"""
    try:
        from discourse_task.discourse_task import discourse_task_service
        data = request.json
        discourse_link = data.get('discourse_link', '').strip()
        reward_amount = float(data.get('reward_amount', 500))
        admin_wallet = session.get('wallet')

        result = discourse_task_service.update_settings(discourse_link, reward_amount, admin_wallet)

        if result.get('success'):
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="update_discourse_task_settings",
                action_details={"link": discourse_link, "reward_amount": reward_amount}
            )

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error updating discourse settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/discourse-task/pending", methods=["GET"])
@admin_required
def admin_get_discourse_pending():
    """Admin: Get pending discourse task submissions"""
    try:
        from discourse_task.discourse_task import discourse_task_service
        result = discourse_task_service.get_pending_submissions()
        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error getting pending discourse submissions: {e}")
        return jsonify({"success": False, "submissions": [], "error": str(e)}), 500


@routes.route("/api/admin/discourse-task/approve", methods=["POST"])
@admin_required
def admin_approve_discourse():
    """Admin: Approve a discourse task submission and disburse reward"""
    try:
        from discourse_task.discourse_task import discourse_task_service
        data = request.json
        submission_id = data.get('submission_id')
        admin_wallet = session.get('wallet')

        if not submission_id:
            return jsonify({"success": False, "error": "Missing submission_id"}), 400

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                discourse_task_service.approve_submission(submission_id, admin_wallet)
            )
        finally:
            loop.close()

        if result and result.get('success'):
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="approve_discourse_task",
                action_details={"submission_id": submission_id}
            )

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error approving discourse submission: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/discourse-task/reject", methods=["POST"])
@admin_required
def admin_reject_discourse():
    """Admin: Reject a discourse task submission"""
    try:
        from discourse_task.discourse_task import discourse_task_service
        data = request.json
        submission_id = data.get('submission_id')
        reason = data.get('reason', '')
        admin_wallet = session.get('wallet')

        if not submission_id:
            return jsonify({"success": False, "error": "Missing submission_id"}), 400

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                discourse_task_service.reject_submission(submission_id, admin_wallet, reason)
            )
        finally:
            loop.close()

        if result and result.get('success'):
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="reject_discourse_task",
                action_details={"submission_id": submission_id, "reason": reason}
            )

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error rejecting discourse submission: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ─────────────────────────────────────────────
# YouTube Video Management (Homepage Carousel)
# ─────────────────────────────────────────────

@routes.route("/api/youtube-videos", methods=["GET"])
def get_youtube_videos_public():
    """Get all active YouTube videos for homepage carousel"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": True, "videos": []})

        result = safe_supabase_operation(
            lambda: supabase.table('homepage_videos')
                .select('*')
                .eq('is_active', True)
                .order('created_at', desc=True)
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get homepage videos"
        )

        return jsonify({
            "success": True,
            "videos": result.data if result.data else []
        })

    except Exception as e:
        logger.error(f"❌ Error getting homepage videos: {e}")
        return jsonify({"success": True, "videos": []})


@routes.route("/api/sponsor-certificates", methods=["GET"])
def get_sponsor_certificates():
    """Get sponsor certificates for homepage carousel"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": True, "certificates": []})

        try:
            result = supabase.table('sponsor_certificates')\
                .select('*')\
                .eq('is_active', True)\
                .order('created_at', desc=True)\
                .execute()
            return jsonify({
                "success": True,
                "certificates": result.data if result.data else []
            })
        except Exception:
            return jsonify({"success": True, "certificates": []})

    except Exception as e:
        logger.error(f"❌ Error getting sponsor certificates: {e}")
        return jsonify({"success": True, "certificates": []})


@routes.route("/api/admin/youtube-videos", methods=["GET"])
@admin_required
def admin_get_youtube_videos():
    """Get all YouTube videos (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        result = safe_supabase_operation(
            lambda: supabase.table('homepage_videos')
                .select('*')
                .order('created_at', desc=True)
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="admin get homepage videos"
        )

        return jsonify({
            "success": True,
            "videos": result.data if result.data else []
        })

    except Exception as e:
        logger.error(f"❌ Error getting homepage videos (admin): {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/youtube-videos", methods=["POST"])
@admin_required
def admin_add_youtube_video():
    """Add a YouTube video link (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        data = request.get_json()
        youtube_url = (data.get('youtube_url') or '').strip()
        title = (data.get('title') or '').strip()

        if not youtube_url:
            return jsonify({"success": False, "error": "YouTube URL is required"}), 400

        # Extract YouTube video ID from various URL formats
        import re
        yt_id_match = re.search(
            r'(?:youtube\.com\/(?:watch\?v=|embed\/|shorts\/)|youtu\.be\/)([A-Za-z0-9_-]{11})',
            youtube_url
        )
        if not yt_id_match:
            return jsonify({"success": False, "error": "Invalid YouTube URL. Please use a standard YouTube link."}), 400

        video_id = yt_id_match.group(1)
        embed_url = f"https://www.youtube.com/embed/{video_id}"

        video_data = {
            "youtube_url": youtube_url,
            "embed_url": embed_url,
            "video_id": video_id,
            "title": title or "GoodMarket Video",
            "is_active": True
        }

        result = safe_supabase_operation(
            lambda: supabase.table('homepage_videos').insert(video_data).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="add homepage video"
        )

        if result.data:
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="add_youtube_video",
                action_details={"video_id": video_id, "title": title}
            )
            logger.info(f"✅ YouTube video added by admin {admin_wallet[:8] if admin_wallet else 'unknown'}...")
            return jsonify({"success": True, "video": result.data[0]})
        else:
            return jsonify({"success": False, "error": "Failed to save video. Make sure the homepage_videos table exists in Supabase."}), 500

    except Exception as e:
        logger.error(f"❌ Error adding YouTube video: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/profile", methods=["GET"])
@auth_required
def get_profile():
    """Get user profile data including earnings breakdown and activity history"""
    try:
        wallet = session.get("wallet")
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        user_info = safe_supabase_operation(
            lambda: supabase.table("user_data")
                .select("wallet_address, first_login, last_login, ubi_verified")
                .eq("wallet_address", wallet)
                .limit(1)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="get user info"
        )

        masked_wallet = f"{wallet[:6]}...{wallet[-4:]}"
        wallet_lower = wallet.lower()
        learn_data = safe_supabase_operation(
            lambda: supabase.table("learnearn_log")
                .select("amount_g$, timestamp, score, total_questions, quiz_id")
                .or_(f"wallet_address.eq.{masked_wallet},wallet_address.eq.{wallet_lower},wallet_address.eq.{wallet}")
                .eq("status", True)
                .order("timestamp", desc=True)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="get learn data"
        )

        twitter_data = safe_supabase_operation(
            lambda: supabase.table("twitter_task_log")
                .select("reward_amount, status, created_at, twitter_url")
                .eq("wallet_address", wallet)
                .eq("status", "completed")
                .order("created_at", desc=True)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="get twitter data"
        )

        telegram_data = safe_supabase_operation(
            lambda: supabase.table("telegram_task_log")
                .select("reward_amount, status, created_at, telegram_url")
                .eq("wallet_address", wallet)
                .eq("status", "completed")
                .order("created_at", desc=True)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="get telegram data"
        )

        stories_data = safe_supabase_operation(
            lambda: supabase.table("community_stories_submissions")
                .select("reward_amount, status, created_at")
                .eq("wallet_address", wallet)
                .eq("status", "approved")
                .order("created_at", desc=True)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="get stories data"
        )

        price_pred_data = safe_supabase_operation(
            lambda: supabase.table("price_predictions")
                .select("crypto_symbol, direction, timeframe_minutes, entry_price, result_price, resolved_at")
                .eq("wallet_address", wallet)
                .eq("status", "won")
                .order("resolved_at", desc=True)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="get price prediction data"
        )

        learn_records_raw = learn_data.data or []
        seen_quiz_ids = set()
        learn_records = []
        for r in learn_records_raw:
            qid = r.get("quiz_id")
            if qid and qid not in seen_quiz_ids:
                seen_quiz_ids.add(qid)
                learn_records.append(r)
            elif not qid:
                learn_records.append(r)

        twitter_records = twitter_data.data or []
        telegram_records = telegram_data.data or []
        stories_records = stories_data.data or []
        price_pred_records = price_pred_data.data or []
        user_records = user_info.data or []

        _pp_rewards = {1: 2.0, 60: 5.0, 720: 20.0, 1440: 50.0}

        learn_total = sum(float(r.get("amount_g$") or 0) for r in learn_records)
        twitter_total = sum(float(r.get("reward_amount") or 0) for r in twitter_records)
        telegram_total = sum(float(r.get("reward_amount") or 0) for r in telegram_records)
        stories_total = sum(float(r.get("reward_amount") or 0) for r in stories_records)
        price_pred_total = sum(_pp_rewards.get(int(r.get("timeframe_minutes") or 0), 0) for r in price_pred_records)
        grand_total = learn_total + twitter_total + telegram_total + stories_total + price_pred_total

        user_row = user_records[0] if user_records else {}
        first_login = user_row.get("first_login") or user_row.get("last_login")

        recent_activity = []
        for r in learn_records[:5]:
            recent_activity.append({
                "type": "Learn & Earn",
                "icon": "🎓",
                "amount": float(r.get("amount_g$") or 0),
                "date": r.get("timestamp")
            })
        for r in twitter_records[:3]:
            recent_activity.append({
                "type": "Twitter Task",
                "icon": "🐦",
                "amount": float(r.get("reward_amount") or 0),
                "date": r.get("created_at")
            })
        for r in telegram_records[:3]:
            recent_activity.append({
                "type": "Telegram Task",
                "icon": "📱",
                "amount": float(r.get("reward_amount") or 0),
                "date": r.get("created_at")
            })
        for r in stories_records[:3]:
            recent_activity.append({
                "type": "Community Story",
                "icon": "🌟",
                "amount": float(r.get("reward_amount") or 0),
                "date": r.get("created_at")
            })
        for r in price_pred_records[:5]:
            mins = int(r.get("timeframe_minutes") or 0)
            reward = _pp_rewards.get(mins, 0)
            crypto = r.get("crypto_symbol", "")
            direction = r.get("direction", "")
            tf_labels = {1: "1 Min", 60: "1 Hour", 720: "12 Hours", 1440: "24 Hours"}
            tf_label = tf_labels.get(mins, f"{mins}min")
            detail = f"{crypto} {direction} ({tf_label})"
            recent_activity.append({
                "type": f"Price Prediction Win — {detail}",
                "icon": "📈",
                "amount": reward,
                "date": r.get("resolved_at")
            })

        recent_activity.sort(key=lambda x: x.get("date") or "", reverse=True)

        return jsonify({
            "success": True,
            "wallet": wallet,
            "first_login": first_login,
            "earnings": {
                "learn_earn": round(learn_total, 2),
                "twitter": round(twitter_total, 2),
                "telegram": round(telegram_total, 2),
                "community_stories": round(stories_total, 2),
                "price_prediction": round(price_pred_total, 2),
                "total": round(grand_total, 2)
            },
            "counts": {
                "quizzes": len(learn_records),
                "twitter_tasks": len(twitter_records),
                "telegram_tasks": len(telegram_records),
                "stories": len(stories_records),
                "price_predictions": len(price_pred_records)
            },
            "recent_activity": recent_activity[:15]
        })

    except Exception as e:
        logger.error(f"❌ Error fetching profile: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/youtube-videos/<int:video_id>", methods=["DELETE"])
@admin_required
def admin_delete_youtube_video(video_id):
    """Delete a YouTube video (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        result = safe_supabase_operation(
            lambda: supabase.table('homepage_videos')
                .delete()
                .eq('id', video_id)
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="delete homepage video"
        )

        admin_wallet = session.get('wallet')
        log_admin_action(
            admin_wallet=admin_wallet,
            action_type="delete_youtube_video",
            action_details={"video_id": video_id}
        )

        logger.info(f"✅ YouTube video {video_id} deleted by admin {admin_wallet[:8] if admin_wallet else 'unknown'}...")
        return jsonify({"success": True})

    except Exception as e:
        logger.error(f"❌ Error deleting YouTube video: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/check-identity", methods=["GET"])
def check_identity():
    """Check if a wallet address is face-verified on the GoodDollar Identity contract."""
    wallet_address = request.args.get("wallet", "").strip()
    if not wallet_address:
        return jsonify({"error": "wallet param required"}), 400
    try:
        from web3 import Web3
        wallet_address = Web3.to_checksum_address(wallet_address)
    except Exception:
        return jsonify({"error": "Invalid wallet address"}), 400

    from blockchain import is_identity_verified
    result = is_identity_verified(wallet_address)
    return jsonify(result)


def _wc_service_url():
    base = os.getenv("WC_SERVICE_URL")
    if base:
        return base.rstrip("/")
    return f"http://127.0.0.1:{os.getenv('WC_SERVICE_PORT', '3001')}"


def _wc_proxy(method: str, path: str, body: dict = None, timeout: int = 30):
    url = f"{_wc_service_url()}{path}"
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
            return data, resp.status, None
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
            err_json = json.loads(err_body) if err_body else {}
            return err_json, e.code, None
        except Exception:
            return {"error": f"HTTP {e.code}"}, e.code, None
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return None, 503, f"WalletConnect service unavailable: {reason}"
    except Exception as e:
        return None, 500, str(e)


@routes.route("/api/wc-uri", methods=["GET"])
def wc_uri():
    data, status, err = _wc_proxy("GET", "/uri", timeout=35)
    if err:
        return jsonify({"success": False, "error": err}), status
    if status >= 400:
        return jsonify({"success": False, **(data or {})}), status
    return jsonify({"success": True, **(data or {})}), 200


@routes.route("/api/wc-session/<session_id>", methods=["GET"])
def wc_session(session_id):
    safe_id = str(session_id).strip()
    if not safe_id:
        return jsonify({"success": False, "error": "session_id required"}), 400
    data, status, err = _wc_proxy("GET", f"/session/{safe_id}", timeout=20)
    if err:
        return jsonify({"success": False, "error": err}), status
    if status >= 400:
        return jsonify({"success": False, **(data or {})}), status
    return jsonify({"success": True, **(data or {})}), 200


@routes.route("/api/wc-sign/<session_id>", methods=["POST"])
def wc_sign(session_id):
    safe_id = str(session_id).strip()
    if not safe_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    address = (body.get("address") or "").strip()
    if not message or not address:
        return jsonify({"success": False, "error": "message and address are required"}), 400

    data, status, err = _wc_proxy(
        "POST",
        f"/sign/{safe_id}",
        body={"message": message, "address": address},
        timeout=45,
    )
    if err:
        return jsonify({"success": False, "error": err}), status
    if status >= 400:
        return jsonify({"success": False, **(data or {})}), status
    return jsonify({"success": True, **(data or {})}), 200


@routes.route("/api/wc-tx/<session_id>", methods=["POST"])
def wc_tx(session_id):
    safe_id = str(session_id).strip()
    if not safe_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"success": False, "error": "invalid request body"}), 400

    data, status, err = _wc_proxy(
        "POST",
        f"/tx/{safe_id}",
        body=body,
        timeout=60,
    )
    if err:
        return jsonify({"success": False, "error": err}), status
    if status >= 400:
        return jsonify({"success": False, **(data or {})}), status
    return jsonify({"success": True, **(data or {})}), 200




@routes.route("/api/tx-receipt/<tx_hash>", methods=["GET"])
@auth_required
def tx_receipt(tx_hash):
    """Poll Celo for a transaction receipt and return its status."""
    try:
        from web3 import Web3
        import blockchain as _bc
        w3 = Web3(Web3.HTTPProvider(_bc.CELO_RPC, request_kwargs={"timeout": 10}))
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        if receipt is None:
            return jsonify({"found": False, "status": "pending"})
        return jsonify({
            "found": True,
            "status": "success" if receipt.status == 1 else "failed",
            "block_number": receipt.blockNumber,
            "gas_used": receipt.gasUsed
        })
    except Exception as e:
        logger.error(f"tx_receipt error for {tx_hash}: {e}")
        return jsonify({"found": False, "status": "pending", "error": str(e)})


@routes.route("/api/ubi-entitlement", methods=["GET"])
@auth_required
def ubi_entitlement():
    """Return how much G$ the logged-in wallet can claim right now."""
    try:
        wallet = session.get("wallet")
        force  = request.args.get("force", "0") == "1"
        from blockchain import get_ubi_entitlement, invalidate_entitlement_cache
        if force:
            invalidate_entitlement_cache(wallet)
        result = get_ubi_entitlement(wallet)
        return jsonify(result)
    except Exception as e:
        logger.error(f"UBI entitlement route error: {e}")
        return jsonify({"success": False, "error": str(e), "entitlement": 0, "can_claim": False}), 500


@routes.route("/wallet")
def wallet_page():
    """Wallet page for sending/receiving G$ and CELO"""
    wallet = session.get("wallet")
    if not wallet or not session.get("verified"):
        return redirect(url_for("routes.index"))
    try:
        supabase = get_supabase_client()
        if supabase:
            result = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')
                    .select('is_maintenance')
                    .eq('feature_name', 'wallet_feature')
                    .execute(),
                operation_name="check wallet feature visibility"
            )
            if result and result.data and result.data[0].get('is_maintenance', False):
                return render_template("feature_unavailable.html", feature_name="Wallet", wallet=wallet)
    except Exception:
        pass
    return render_template("wallet.html", wallet=wallet, login_method=session.get("login_method", "walletconnect"))


@routes.route("/swap")
def swap_page():
    """Swap page for G$ <-> CELO via Uniswap V3 on Celo"""
    wallet = session.get("wallet")
    if not wallet or not session.get("verified"):
        return redirect(url_for("routes.index"))
    try:
        supabase = get_supabase_client()
        if supabase:
            result = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')
                    .select('is_maintenance')
                    .eq('feature_name', 'swap_feature')
                    .execute(),
                operation_name="check swap feature visibility"
            )
            if result and result.data and result.data[0].get('is_maintenance', False):
                return render_template("feature_unavailable.html", feature_name="Swap", wallet=wallet)
    except Exception:
        pass
    return render_template("swap.html", wallet=wallet, login_method=session.get("login_method", "walletconnect"))


@routes.route("/send-link")
def send_link_page():
    """Send G$ via a one-time payment link (GoodDollar OneTimePayments contract)"""
    wallet = session.get("wallet")
    if not wallet or not session.get("verified"):
        return redirect(url_for("routes.index"))
    return render_template("send-link.html", wallet=wallet, login_method=session.get("login_method", "walletconnect"))


@routes.route("/claim")
def claim_page():
    """Claim page for one-time payment links — no login required"""
    return render_template("claim.html")


# ── Payment Link helpers ────────────────────────────────────────────────────
# Payment link private keys are NOT stored server-side.
# The ephemeral key lives only in the browser (localStorage + URL hash).

@routes.route("/api/payment-links", methods=["POST"])
@auth_required
def create_payment_link():
    """Save a sent payment link to the database (no private key stored server-side)"""
    try:
        wallet = session.get("wallet")
        data = request.get_json()
        payment_id = data.get("paymentId", "").strip()
        amount     = data.get("amount", "").strip()
        tx_hash    = data.get("txHash", "").strip()

        if not payment_id or not amount:
            return jsonify({"success": False, "error": "Missing fields"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "DB unavailable"}), 503

        safe_supabase_operation(
            lambda: supabase.table("payment_links").insert({
                "wallet_address": wallet,
                "payment_id": payment_id,
                "amount": amount,
                "tx_hash": tx_hash,
                "status": "active"
            }).execute()
        )
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"create_payment_link error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/payment-links", methods=["GET"])
@auth_required
def list_payment_links():
    """List all payment links for the current user (newest first)"""
    try:
        wallet = session.get("wallet")
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "DB unavailable"}), 503

        result = safe_supabase_operation(
            lambda: supabase.table("payment_links")
                .select("payment_id,amount,tx_hash,status,created_at")
                .eq("wallet_address", wallet)
                .order("created_at", desc=True)
                .limit(100)
                .execute()
        )
        rows = result.data if result else []
        return jsonify({"success": True, "payments": rows})
    except Exception as e:
        logger.error(f"list_payment_links error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/payment-links/<payment_id>/key", methods=["GET"])
@auth_required
def get_payment_key(payment_id):
    """Payment link keys are no longer stored server-side — claim links exist only in the browser that created them."""
    return jsonify({"success": False, "error": "Claim link is only available in the browser where this payment was created."}), 410


@routes.route("/api/payment-links/<payment_id>", methods=["PATCH"])
@auth_required
def update_payment_link(payment_id):
    """Update status of a payment link owned by the current user"""
    try:
        wallet = session.get("wallet")
        data = request.get_json()
        status = data.get("status", "").strip()
        if status not in ("active", "claimed", "cancelled"):
            return jsonify({"success": False, "error": "Invalid status"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "DB unavailable"}), 503

        safe_supabase_operation(
            lambda: supabase.table("payment_links")
                .update({"status": status})
                .eq("wallet_address", wallet)
                .eq("payment_id", payment_id)
                .execute()
        )
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"update_payment_link error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/ubi-pool-balance", methods=["GET"])
def ubi_pool_balance():
    """Get the G$ balance held in the GoodDollar UBI Pool contract (public, no auth needed)."""
    try:
        from blockchain import get_gooddollar_balance, GOODDOLLAR_CONTRACTS
        ubi_proxy = GOODDOLLAR_CONTRACTS["UBI_PROXY"]
        result = get_gooddollar_balance(ubi_proxy)
        return jsonify({
            "success": True,
            "pool_address": ubi_proxy,
            "balance": result.get("balance", 0),
            "balance_formatted": result.get("balance_formatted", "—")
        })
    except Exception as e:
        logger.error(f"ubi_pool_balance error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/wallet/balances", methods=["GET"])
@auth_required
def wallet_balances():
    """Get G$ and CELO balances for the current user"""
    try:
        wallet = session.get("wallet")
        from blockchain import get_gooddollar_balance, get_celo_balance, get_cusd_balance, get_usdt_balance
        gd = get_gooddollar_balance(wallet)
        celo = get_celo_balance(wallet)
        cusd = get_cusd_balance(wallet)
        usdt = get_usdt_balance(wallet)
        return jsonify({"success": True, "gd": gd, "celo": celo, "cusd": cusd, "usdt": usdt})
    except Exception as e:
        logger.error(f"wallet_balances error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/wallet/history", methods=["GET"])
@auth_required
def wallet_history():
    """Get G$ transfer history for the current user"""
    try:
        wallet = session.get("wallet")
        from blockchain import get_wallet_transfer_history
        transfers = get_wallet_transfer_history(wallet, limit=40)
        return jsonify({"success": True, "transfers": transfers})
    except Exception as e:
        logger.error(f"wallet_history error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/wallet/transaction-history", methods=["GET"])
@auth_required
def wallet_transaction_history():
    """
    Comprehensive transaction history — G$ transfers classified as:
    claim | savings_deposit | savings_withdraw | swap | transfer_sent | transfer_received
    """
    try:
        wallet = session.get("wallet")
        limit  = min(int(request.args.get("limit", 50)), 100)
        force  = request.args.get("force", "0") == "1"
        from blockchain import get_comprehensive_tx_history
        txs = get_comprehensive_tx_history(wallet, limit=limit, force=force)
        return jsonify({"success": True, "transactions": txs, "count": len(txs)})
    except Exception as e:
        logger.error(f"wallet_transaction_history error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/wallet/prepare-send", methods=["POST"])
@auth_required
def wallet_prepare_send():
    """
    Prepare ERC-20 transfer calldata for G$ or a native CELO send.
    Returns unsigned tx parameters so the frontend can request wallet signing.
    """
    try:
        data = request.get_json()
        token = data.get("token", "GD").upper()
        to_address = data.get("to", "").strip()
        amount_str = data.get("amount", "0")

        if not to_address or not to_address.startswith("0x") or len(to_address) != 42:
            return jsonify({"success": False, "error": "Invalid recipient address"}), 400

        try:
            amount = float(amount_str)
            if amount <= 0:
                raise ValueError("amount must be > 0")
        except Exception:
            return jsonify({"success": False, "error": "Invalid amount"}), 400

        from web3 import Web3
        from blockchain import GOODDOLLAR_CONTRACTS, CELO_CHAIN_ID, CELO_RPC

        if token in ("GD", "G$"):
            from blockchain import prepare_gd_transfer_data
            result = prepare_gd_transfer_data(to_address, amount)
            return jsonify(result)
        elif token == "CUSD":
            from blockchain import prepare_cusd_transfer_data
            result = prepare_cusd_transfer_data(to_address, amount)
            return jsonify(result)
        elif token == "USDT":
            from blockchain import prepare_usdt_transfer_data
            result = prepare_usdt_transfer_data(to_address, amount)
            return jsonify(result)
        elif token == "CELO":
            w3 = Web3(Web3.HTTPProvider(CELO_RPC))
            to_checksum = Web3.to_checksum_address(to_address)
            amount_wei = int(amount * (10 ** 18))
            return jsonify({
                "success": True,
                "to": to_checksum,
                "data": "0x",
                "value": hex(amount_wei),
                "chain_id": CELO_CHAIN_ID,
                "token": "CELO",
                "recipient": to_checksum,
                "amount": amount,
            })
        else:
            return jsonify({"success": False, "error": f"Unsupported token: {token}"}), 400

    except Exception as e:
        logger.error(f"wallet_prepare_send error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/xdc-wallet")
def xdc_wallet_page():
    """XDC Network wallet page"""
    wallet = session.get("wallet")
    if not wallet or not session.get("verified"):
        return redirect(url_for("routes.index"))
    login_method = session.get("login_method", "")
    is_custodial = login_method in ("custodial", "turnkey")
    return render_template("xdc_wallet.html", wallet=wallet,
                           login_method=login_method, is_custodial=is_custodial)


@routes.route("/api/xdc/balances", methods=["GET"])
@auth_required
def xdc_balances():
    """Get XDC and xUSDT balances for the current user"""
    try:
        wallet = session.get("wallet")
        from blockchain import get_xdc_balance, get_xusdt_balance
        xdc = get_xdc_balance(wallet)
        xusdt = get_xusdt_balance(wallet)
        return jsonify({"success": True, "xdc": xdc, "xusdt": xusdt})
    except Exception as e:
        logger.error(f"xdc_balances error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/xdc/history", methods=["GET"])
@auth_required
def xdc_history():
    """Get XDC transaction history for the current user"""
    try:
        wallet = session.get("wallet")
        from blockchain import get_xdc_transfer_history
        transfers = get_xdc_transfer_history(wallet, limit=40)
        return jsonify({"success": True, "transfers": transfers})
    except Exception as e:
        logger.error(f"xdc_history error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/xdc/gd-info", methods=["GET"])
@auth_required
def xdc_gd_info():
    """Get G$ balance + UBI entitlement + identity status on XDC Network"""
    try:
        wallet = session.get("wallet")
        from blockchain import get_xdc_gd_balance, check_xdc_ubi_entitlement, is_xdc_identity_whitelisted
        gd_bal = get_xdc_gd_balance(wallet)
        entitlement = check_xdc_ubi_entitlement(wallet)
        identity = is_xdc_identity_whitelisted(wallet)
        return jsonify({
            "success": True,
            "gd_balance": gd_bal,
            "entitlement": entitlement,
            "identity": identity,
        })
    except Exception as e:
        logger.error(f"xdc_gd_info error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/xdc/prepare-send", methods=["POST"])
@auth_required
def xdc_prepare_send():
    """Prepare XDC or xUSDT send transaction parameters"""
    try:
        data = request.get_json()
        token = data.get("token", "XDC").upper()
        to_address = data.get("to", "").strip()
        amount_str = data.get("amount", "0")

        if not to_address:
            return jsonify({"success": False, "error": "Recipient address required"}), 400

        from blockchain import _normalize_xdc_address
        norm_to = _normalize_xdc_address(to_address)
        if not norm_to.startswith("0x") or len(norm_to) != 42:
            return jsonify({"success": False, "error": "Invalid XDC/Ethereum address"}), 400

        try:
            amount = float(amount_str)
            if amount <= 0:
                raise ValueError("amount must be > 0")
        except Exception:
            return jsonify({"success": False, "error": "Invalid amount"}), 400

        if token == "XDC":
            from blockchain import prepare_xdc_send_data
            result = prepare_xdc_send_data(to_address, amount)
            return jsonify(result)
        elif token == "XUSDT":
            from blockchain import prepare_xdc_token_send_data, XUSDT_CONTRACT
            result = prepare_xdc_token_send_data(to_address, amount, XUSDT_CONTRACT, decimals=6)
            return jsonify(result)
        elif token in ("XDC_GD", "XDCGD"):
            from blockchain import prepare_xdc_token_send_data, XDC_GD_TOKEN, XDC_GD_DECIMALS
            result = prepare_xdc_token_send_data(to_address, amount, XDC_GD_TOKEN, decimals=XDC_GD_DECIMALS)
            result["token"] = "XDC_GD"
            return jsonify(result)
        else:
            return jsonify({"success": False, "error": f"Unsupported token: {token}"}), 400

    except Exception as e:
        logger.error(f"xdc_prepare_send error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500



def _get_fernet():
    """Return a Fernet instance keyed from SESSION_SECRET using PBKDF2 (stronger than SHA-256)."""
    import hashlib, base64
    from cryptography.fernet import Fernet
    secret = os.environ.get("SESSION_SECRET", "goodmarket-default-secret")
    salt = b"goodmarket-session-v2"
    key_bytes = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, iterations=200_000)
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def _is_turnkey_unavailable_error(err) -> bool:
    err_lower = str(err or "").lower()
    unavailable_markers = (
        "turnkey service unavailable",
        "turnkey not configured",
        "connection refused",
        "failed to establish a new connection",
        "name or service not known",
        "connection reset",
        "timed out",
        "timeout",
    )
    return any(marker in err_lower for marker in unavailable_markers)


def _format_turnkey_export_error(err) -> str:
    """Normalize Turnkey export failures into actionable user-facing messages."""
    raw = str(err or "").strip()
    text = raw.lower()

    if "route not found" in text or ("not found" in text and "/turnkey/" in text):
        return (
            "Turnkey export endpoint is unavailable on this deployment. "
            "Please contact support to redeploy/update the wallet sidecar "
            "and verify WC_SERVICE_URL points to the latest /api/wc service."
        )
    if "no active otp" in text:
        return "Your verification code expired. Tap Send Code, verify a new OTP, then try export again."
    if "invalid otp" in text or "verification" in text:
        return "Invalid verification code. Request a new OTP and try again."
    if _is_turnkey_unavailable_error(raw):
        return (
            "Turnkey service is temporarily unavailable. "
            "Please retry in a minute. If it keeps failing, contact support."
        )
    return raw or "Could not export Turnkey key. Please retry and contact support if the issue continues."


_email_wallet_links_ready = False


def _normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def _email_link_hash(email: str) -> str:
    import hashlib
    return hashlib.sha256(_normalize_email(email).encode()).hexdigest()


def _ensure_email_wallet_links_table():
    global _email_wallet_links_ready
    if _email_wallet_links_ready:
        return True
    supabase = get_supabase_client()
    if not supabase:
        logger.warning("Email wallet recovery requires SUPABASE_URL + SUPABASE_ANON_KEY.")
        return False
    try:
        result = safe_supabase_operation(
            lambda: supabase.table("email_wallet_links").select("id").limit(1).execute(),
            fallback_result=None,
            operation_name="email wallet links table check"
        )
        if result is None:
            logger.warning("email_wallet_links table is missing or inaccessible in Supabase.")
            return False
        _email_wallet_links_ready = True
        return True
    except Exception as err:
        logger.warning(f"Could not initialize email wallet recovery table: {err}")
        return False


def _get_email_wallet_link(email: str):
    if not email or not _ensure_email_wallet_links_table():
        return None
    try:
        supabase = get_supabase_client()
        if not supabase:
            return None

        result = safe_supabase_operation(
            lambda: supabase.table("email_wallet_links")
            .select("email_hash, wallet_address, login_method, custodial_key_enc, turnkey_suborg_id, turnkey_sign_with")
            .eq("email_hash", _email_link_hash(email))
            .limit(1)
            .execute(),
            fallback_result=None,
            operation_name="get email wallet recovery link"
        )
        if not result or not getattr(result, "data", None):
            return None
        return result.data[0]
    except Exception as err:
        logger.warning(f"Could not get email wallet recovery link: {err}")
    return None


def _save_email_wallet_link(email: str, wallet_address: str, login_method: str, custodial_key_enc: str = None, turnkey_suborg_id: str = None, turnkey_sign_with: str = None):
    if not email or not wallet_address or not _ensure_email_wallet_links_table():
        return False
    try:
        supabase = get_supabase_client()
        if not supabase:
            return False

        payload = {
            "email_hash": _email_link_hash(email),
            "wallet_address": wallet_address,
            "login_method": login_method or "custodial",
            "custodial_key_enc": custodial_key_enc,
            "turnkey_suborg_id": turnkey_suborg_id,
            "turnkey_sign_with": turnkey_sign_with
        }

        result = safe_supabase_operation(
            lambda: supabase.table("email_wallet_links").upsert(payload, on_conflict="email_hash").execute(),
            fallback_result=None,
            operation_name="save email wallet recovery link"
        )
        return bool(result)
    except Exception as err:
        logger.warning(f"Could not save email wallet recovery link: {err}")
        return False


def _email_recently_verified(email: str) -> bool:
    email = _normalize_email(email)
    verified_email = session.get("turnkey_email_pending")
    verified_flag = bool(session.get("turnkey_email_verified"))
    verified_at = int(session.get("turnkey_email_verified_at") or 0)
    return verified_flag and verified_email == email and int(time.time()) - verified_at <= 15 * 60


def _clear_email_onboarding_session():
    session.pop("turnkey_email_pending", None)
    session.pop("turnkey_email_verified", None)
    session.pop("turnkey_email_verified_at", None)
    session.pop("turnkey_email_fallback", None)
    session.pop("turnkey_email_fallback_allowed", None)
    session.pop("turnkey_email_fallback_at", None)


def _turnkey_export_recently_verified(email: str, max_age_seconds: int = 10 * 60) -> bool:
    verified_email = session.get("turnkey_export_email_verified")
    verified_at = int(session.get("turnkey_export_email_verified_at") or 0)
    if not verified_email or verified_email != _normalize_email(email):
        return False
    if not verified_at:
        return False
    return int(time.time()) - verified_at <= max_age_seconds


@routes.route("/api/turnkey/login", methods=["POST"])
def turnkey_login():
    """Login with a private key — derives address locally, stores encrypted key in session."""
    try:
        from eth_account import Account
        data = request.get_json()
        private_key = (data.get("private_key") or data.get("privateKey") or "").strip()
        referral_code = data.get("referral_code", None)

        if not private_key:
            return jsonify({"success": False, "error": "Private key required"}), 400

        # Normalise to 0x-prefixed 66-char hex
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        if len(private_key) != 66:
            return jsonify({"success": False, "error": "Invalid private key — must be 64 hex characters."}), 400

        # Derive wallet address
        try:
            acct = Account.from_key(private_key)
            wallet_address = acct.address  # checksummed
        except Exception as key_err:
            return jsonify({"success": False, "error": f"Invalid private key: {key_err}"}), 400

        # Encrypt the key for session storage (never stored in DB)
        fernet = _get_fernet()
        encrypted_key = fernet.encrypt(private_key.encode()).decode()

        session["wallet"] = wallet_address
        session["verified"] = True
        session["login_method"] = "custodial"
        session["custodial_key_enc"] = encrypted_key
        session.permanent = True

        logger.info(f"Custodial login: derived address {wallet_address}")
        analytics.track_verification_attempt(wallet_address, True)
        analytics.track_user_session(wallet_address)

        try:
            from supabase_client import get_supabase_client
            supabase = get_supabase_client()
            if supabase:
                try:
                    supabase.table("user_data").upsert({
                        "wallet_address": wallet_address
                    }, on_conflict="wallet_address").execute()
                except Exception:
                    pass
        except Exception as db_err:
            logger.warning(f"Could not save custodial login to Supabase: {db_err}")

        if referral_code and referral_code.strip():
            try:
                from referral_program.referral_service import referral_service
                validation = referral_service.validate_referral_code(referral_code.strip().upper())
                if validation.get("valid"):
                    referral_service.record_referral(
                        referral_code=referral_code.strip().upper(),
                        referee_wallet=wallet_address
                    )
            except Exception as ref_err:
                logger.warning(f"Referral processing error in custodial login: {ref_err}")

        return jsonify({
            "success": True,
            "wallet": wallet_address,
            "redirect_to": "/wallet"
        })
    except Exception as e:
        logger.error(f"Custodial login error: {e}")
        return jsonify({"success": False, "error": "Login failed. Please check your key and try again."}), 500


@routes.route("/api/turnkey/create-wallet", methods=["POST"])
def turnkey_create_wallet():
    """Create a new Turnkey custodial wallet for the current user session."""
    try:
        from turnkey_service import create_turnkey_wallet
        data = request.get_json() or {}
        referral_code = data.get("referral_code", None)
        email = str(data.get("email", "")).strip().lower()
        user_id = data.get("userId") or email or session.get("wallet") or f"new_{int(time.time())}"
        user_name = data.get("userName") or user_id

        if email:
            verified_email = session.get("turnkey_email_pending")
            verified_flag = bool(session.get("turnkey_email_verified"))
            verified_at = int(session.get("turnkey_email_verified_at") or 0)
            fallback_email = session.get("turnkey_email_fallback")
            fallback_flag = bool(session.get("turnkey_email_fallback_allowed"))
            fallback_at = int(session.get("turnkey_email_fallback_at") or 0)
            fallback_valid = (
                fallback_flag
                and fallback_email == email
                and int(time.time()) - fallback_at <= 15 * 60
            )
            if not fallback_valid:
                if not verified_flag or verified_email != email:
                    return jsonify({"status": "error", "message": "Please verify your email code first."}), 400
                if int(time.time()) - verified_at > 15 * 60:
                    session["turnkey_email_verified"] = False
                    return jsonify({"status": "error", "message": "Verification expired. Request a new code."}), 400
                existing_link = _get_email_wallet_link(email)
                if existing_link and existing_link.get("wallet_address"):
                    wallet_address = existing_link.get("wallet_address")
                    if wallet_address and Web3.is_address(wallet_address):
                        wallet_address = Web3.to_checksum_address(wallet_address)
                    session["wallet"] = wallet_address
                    session["verified"] = True
                    session["login_method"] = existing_link.get("login_method") or "custodial"
                    if existing_link.get("custodial_key_enc"):
                        session["custodial_key_enc"] = existing_link.get("custodial_key_enc")
                    if existing_link.get("turnkey_suborg_id"):
                        session["turnkey_suborg_id"] = existing_link.get("turnkey_suborg_id")
                    if existing_link.get("turnkey_sign_with"):
                        session["turnkey_sign_with"] = existing_link.get("turnkey_sign_with")
                    session.permanent = True
                    analytics.track_verification_attempt(wallet_address, True)
                    analytics.track_user_session(wallet_address)
                    _clear_email_onboarding_session()
                    return jsonify({
                        "status": "success",
                        "wallet": wallet_address,
                        "mode": "email_recovery",
                        "message": "Existing email-linked wallet recovered."
                    })

        result, err = create_turnkey_wallet(user_id, user_name)
        if err:
            if not _is_turnkey_unavailable_error(err):
                return jsonify({"status": "error", "message": err}), 500

            from eth_account import Account
            acct = Account.create()
            wallet_address = acct.address
            private_key = acct.key.hex()

            fernet = _get_fernet()
            encrypted_key = fernet.encrypt(private_key.encode()).decode()

            session["wallet"] = wallet_address
            session["verified"] = True
            session["login_method"] = "custodial"
            session["custodial_key_enc"] = encrypted_key
            session.pop("turnkey_suborg_id", None)
            session.pop("turnkey_sign_with", None)
            session.permanent = True

            analytics.track_verification_attempt(wallet_address, True)
            analytics.track_user_session(wallet_address)

            try:
                from supabase_client import get_supabase_client
                supabase = get_supabase_client()
                if supabase:
                    try:
                        supabase.table("user_data").upsert({
                            "wallet_address": wallet_address
                        }, on_conflict="wallet_address").execute()
                    except Exception:
                        pass
            except Exception as db_err:
                logger.warning(f"Could not save fallback wallet to Supabase: {db_err}")

            if email:
                _save_email_wallet_link(
                    email=email,
                    wallet_address=wallet_address,
                    login_method="custodial",
                    custodial_key_enc=encrypted_key
                )

            if referral_code and str(referral_code).strip():
                try:
                    from referral_program.referral_service import referral_service
                    normalized_code = str(referral_code).strip().upper()
                    validation = referral_service.validate_referral_code(normalized_code)
                    if validation.get("valid"):
                        referral_service.record_referral(
                            referral_code=normalized_code,
                            referee_wallet=wallet_address
                        )
                except Exception as ref_err:
                    logger.warning(f"Referral processing error in fallback wallet creation: {ref_err}")

            _clear_email_onboarding_session()

            return jsonify({
                "status": "success",
                "wallet": wallet_address,
                "mode": "custodial_fallback",
                "warning": "Turnkey unavailable on this deployment; created a secure custodial wallet instead."
            })

        wallet_address = result.get("address")
        suborg_id = result.get("subOrgId")
        sign_with = wallet_address

        if wallet_address and Web3.is_address(wallet_address):
            wallet_address = Web3.to_checksum_address(wallet_address)

        session["wallet"] = wallet_address
        session["verified"] = True
        session["turnkey_suborg_id"] = suborg_id
        session["turnkey_sign_with"] = sign_with
        session["login_method"] = "turnkey"
        session.permanent = True

        if email:
            _save_email_wallet_link(
                email=email,
                wallet_address=wallet_address,
                login_method="turnkey",
                turnkey_suborg_id=suborg_id,
                turnkey_sign_with=sign_with
            )

        if referral_code and str(referral_code).strip():
            try:
                from referral_program.referral_service import referral_service
                normalized_code = str(referral_code).strip().upper()
                validation = referral_service.validate_referral_code(normalized_code)
                if validation.get("valid"):
                    referral_service.record_referral(
                        referral_code=normalized_code,
                        referee_wallet=wallet_address
                    )
            except Exception as ref_err:
                logger.warning(f"Referral processing error in turnkey wallet creation: {ref_err}")

        _clear_email_onboarding_session()

        return jsonify({
            "status": "success",
            "wallet": wallet_address,
            "suborg_id": suborg_id
        })
    except Exception as e:
        logger.error(f"Turnkey create-wallet error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@routes.route("/api/turnkey/email/send-code", methods=["POST"])
def turnkey_email_send_code():
    """Send Turnkey email OTP code for passwordless wallet onboarding."""
    try:
        from turnkey_service import send_email_otp_turnkey
        data = request.get_json() or {}
        email = str(data.get("email", "")).strip().lower()
        if not email or "@" not in email:
            return jsonify({"success": False, "error": "Valid email required"}), 400

        result, err = send_email_otp_turnkey(email)
        if err:
            if _is_turnkey_unavailable_error(err):
                session["turnkey_email_pending"] = email
                session["turnkey_email_verified"] = False
                session["turnkey_email_fallback"] = email
                session["turnkey_email_fallback_allowed"] = True
                session["turnkey_email_fallback_at"] = int(time.time())
                session.permanent = True
                return jsonify({
                    "success": False,
                    "error": "Email OTP is unavailable on this deployment. You can still create a wallet with this email.",
                    "code": "otp_unavailable"
                }), 503
            return jsonify({"success": False, "error": err}), 400

        session["turnkey_email_pending"] = email
        session["turnkey_email_verified"] = False
        session.pop("turnkey_email_fallback", None)
        session.pop("turnkey_email_fallback_allowed", None)
        session.pop("turnkey_email_fallback_at", None)
        session.pop("turnkey_email_verified_at", None)
        session.permanent = True

        return jsonify({"success": True, "email": email, "result": result or {}})
    except Exception as e:
        logger.error(f"turnkey_email_send_code error: {e}")
        return jsonify({"success": False, "error": "Failed to send verification code"}), 500


@routes.route("/api/turnkey/email/verify-code", methods=["POST"])
def turnkey_email_verify_code():
    """Verify Turnkey email OTP code and mark the session as email-verified."""
    try:
        from turnkey_service import verify_email_otp_turnkey
        data = request.get_json() or {}
        email = str(data.get("email", "")).strip().lower()
        code = str(data.get("code", "")).strip()
        if not email or not code:
            return jsonify({"success": False, "error": "email and code required"}), 400

        pending_email = session.get("turnkey_email_pending")
        if pending_email and pending_email != email:
            return jsonify({"success": False, "error": "Email mismatch. Request a new code."}), 400

        result, err = verify_email_otp_turnkey(email, code)
        if err:
            if _is_turnkey_unavailable_error(err):
                return jsonify({
                    "success": False,
                    "error": "Email OTP is unavailable on this deployment. Please create a wallet directly.",
                    "code": "otp_unavailable"
                }), 503
            return jsonify({"success": False, "error": err}), 400

        session["turnkey_email_pending"] = email
        session["turnkey_email_verified"] = True
        session["turnkey_email_verified_at"] = int(time.time())
        session.pop("turnkey_email_fallback", None)
        session.pop("turnkey_email_fallback_allowed", None)
        session.pop("turnkey_email_fallback_at", None)
        session.permanent = True

        return jsonify({"success": True, "email": email, "result": result or {}})
    except Exception as e:
        logger.error(f"turnkey_email_verify_code error: {e}")
        return jsonify({"success": False, "error": "Code verification failed"}), 500


@routes.route("/api/turnkey/email/recover-wallet", methods=["POST"])
def turnkey_email_recover_wallet():
    """Recover an email-linked wallet after successful email OTP verification."""
    try:
        data = request.get_json() or {}
        email = _normalize_email(data.get("email", ""))
        if not email or "@" not in email:
            return jsonify({"success": False, "error": "Valid email required"}), 400
        if not _email_recently_verified(email):
            return jsonify({"success": False, "error": "Please verify your email code first."}), 400

        link = _get_email_wallet_link(email)
        if not link or not link.get("wallet_address"):
            return jsonify({"success": False, "error": "No wallet is linked to this email yet. Create a new wallet first."}), 404

        wallet_address = link.get("wallet_address")
        if wallet_address and Web3.is_address(wallet_address):
            wallet_address = Web3.to_checksum_address(wallet_address)

        login_method = link.get("login_method") or "custodial"
        session["wallet"] = wallet_address
        session["verified"] = True
        session["login_method"] = login_method
        if login_method == "custodial":
            enc_key = link.get("custodial_key_enc")
            if not enc_key:
                return jsonify({"success": False, "error": "This wallet cannot be recovered automatically. Please contact support."}), 409
            session["custodial_key_enc"] = enc_key
            session.pop("turnkey_suborg_id", None)
            session.pop("turnkey_sign_with", None)
        else:
            session["turnkey_suborg_id"] = link.get("turnkey_suborg_id")
            session["turnkey_sign_with"] = link.get("turnkey_sign_with")
            session.pop("custodial_key_enc", None)
        session.permanent = True

        analytics.track_verification_attempt(wallet_address, True)
        analytics.track_user_session(wallet_address)
        _clear_email_onboarding_session()

        return jsonify({
            "success": True,
            "wallet": wallet_address,
            "redirect_to": "/wallet"
        })
    except Exception as e:
        logger.error(f"turnkey_email_recover_wallet error: {e}")
        return jsonify({"success": False, "error": "Wallet recovery failed"}), 500


@routes.route("/api/turnkey/export/send-code", methods=["POST"])
@auth_required
def turnkey_export_send_code():
    """Send OTP specifically for private key export re-authentication."""
    if session.get("login_method") != "turnkey":
        return jsonify({"success": False, "error": "Only available for Turnkey wallets"}), 403
    try:
        from turnkey_service import send_email_otp_turnkey
        data = request.get_json() or {}
        email = _normalize_email(data.get("email", ""))
        if not email or "@" not in email:
            return jsonify({"success": False, "error": "Valid email required"}), 400

        result, err = send_email_otp_turnkey(email)
        if err:
            return jsonify({"success": False, "error": _format_turnkey_export_error(err)}), 400

        session["turnkey_export_email_pending"] = email
        session["turnkey_export_email_verified"] = None
        session["turnkey_export_email_verified_at"] = None
        session.permanent = True
        return jsonify({"success": True, "email": email, "result": result or {}})
    except Exception as e:
        logger.error(f"turnkey_export_send_code error: {e}")
        return jsonify({"success": False, "error": "Failed to send export verification code"}), 500


@routes.route("/api/turnkey/export/verify-code", methods=["POST"])
@auth_required
def turnkey_export_verify_code():
    """Verify OTP specifically for private key export re-authentication."""
    if session.get("login_method") != "turnkey":
        return jsonify({"success": False, "error": "Only available for Turnkey wallets"}), 403
    try:
        from turnkey_service import verify_email_otp_turnkey
        data = request.get_json() or {}
        email = _normalize_email(data.get("email", ""))
        code = str(data.get("code", "")).strip()
        if not email or not code:
            return jsonify({"success": False, "error": "email and code required"}), 400

        pending_email = session.get("turnkey_export_email_pending")
        if pending_email and pending_email != email:
            return jsonify({"success": False, "error": "Email mismatch. Request a new code."}), 400

        result, err = verify_email_otp_turnkey(email, code)
        if err:
            return jsonify({"success": False, "error": _format_turnkey_export_error(err)}), 400

        session["turnkey_export_email_pending"] = email
        session["turnkey_export_email_verified"] = email
        session["turnkey_export_email_verified_at"] = int(time.time())
        session.permanent = True
        return jsonify({"success": True, "email": email, "result": result or {}})
    except Exception as e:
        logger.error(f"turnkey_export_verify_code error: {e}")
        return jsonify({"success": False, "error": "Code verification failed"}), 500


@routes.route("/api/turnkey/sign-tx", methods=["POST"])
@auth_required
def turnkey_sign_tx():
    """Sign a raw EVM transaction using Turnkey for the current user."""
    try:
        from turnkey_service import sign_transaction_turnkey, broadcast_signed_tx
        suborg_id = session.get("turnkey_suborg_id")
        sign_with = session.get("turnkey_sign_with")

        if not suborg_id or not sign_with:
            return jsonify({"error": "No Turnkey wallet in session. Please login with private key."}), 400

        data = request.get_json()
        unsigned_tx = data.get("unsignedTx")
        broadcast = data.get("broadcast", True)

        if not unsigned_tx:
            return jsonify({"error": "unsignedTx required"}), 400

        signed_hex, err = sign_transaction_turnkey(suborg_id, sign_with, unsigned_tx)
        if err:
            return jsonify({"error": err}), 500

        if broadcast:
            tx_hash, broadcast_err = broadcast_signed_tx(signed_hex)
            if broadcast_err:
                return jsonify({"error": broadcast_err, "signedTx": signed_hex}), 500
            return jsonify({"txHash": tx_hash, "signedTx": signed_hex})
        else:
            return jsonify({"signedTx": signed_hex})
    except Exception as e:
        logger.error(f"Turnkey sign-tx error: {e}")
        return jsonify({"error": str(e)}), 500


@routes.route("/api/turnkey/sign-msg", methods=["POST"])
@auth_required
def turnkey_sign_msg():
    """Sign a personal message using Turnkey for the current user."""
    try:
        from turnkey_service import sign_message_turnkey
        suborg_id = session.get("turnkey_suborg_id")
        sign_with = session.get("turnkey_sign_with")

        if not suborg_id or not sign_with:
            return jsonify({"error": "No Turnkey wallet in session."}), 400

        data = request.get_json()
        message = data.get("message")
        if not message:
            return jsonify({"error": "message required"}), 400

        signature, err = sign_message_turnkey(suborg_id, sign_with, message)
        if err:
            return jsonify({"error": err}), 500

        if signature and not signature.startswith('0x'):
            signature = '0x' + signature
        return jsonify({"signature": signature})
    except Exception as e:
        logger.error(f"Turnkey sign-msg error: {e}")
        return jsonify({"error": str(e)}), 500


@routes.route("/api/custodial/sign-tx", methods=["POST"])
@auth_required
def custodial_sign_tx():
    """Sign and broadcast any EVM transaction server-side for custodial-mode users."""
    if session.get("login_method") != "custodial":
        return jsonify({"success": False, "error": "Not in custodial mode"}), 403
    enc_key = session.get("custodial_key_enc")
    if not enc_key:
        return jsonify({"success": False, "error": "No custodial key in session — please log in again"}), 401
    try:
        from eth_account import Account
        from web3 import Web3
        from blockchain import CELO_CHAIN_ID, CELO_RPC

        fernet = _get_fernet()
        private_key = fernet.decrypt(enc_key.encode()).decode()

        data = request.get_json() or {}
        to_addr = data.get("to", "").strip()
        tx_data = data.get("data", "0x")
        value_hex = data.get("value", "0x0")
        chain_id = int(data.get("chain_id", CELO_CHAIN_ID))

        if not to_addr:
            return jsonify({"success": False, "error": "to address required"}), 400

        w3 = Web3(Web3.HTTPProvider(CELO_RPC))
        acct = Account.from_key(private_key)
        from_addr = acct.address

        value_int = int(value_hex, 16) if isinstance(value_hex, str) and value_hex.startswith("0x") else int(value_hex or 0)
        nonce = w3.eth.get_transaction_count(from_addr, "pending")
        gas_price = w3.eth.gas_price

        tx = {
            "chainId": chain_id,
            "nonce": nonce,
            "gasPrice": gas_price,
            "to": Web3.to_checksum_address(to_addr),
            "value": value_int,
            "data": tx_data,
        }
        try:
            tx["gas"] = w3.eth.estimate_gas({**tx, "from": from_addr})
        except Exception:
            tx["gas"] = 200000

        signed = acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        logger.info(f"Custodial tx sent: {tx_hash.hex()} from {from_addr} to {to_addr}")
        return jsonify({"success": True, "tx_hash": "0x" + tx_hash.hex()})

    except Exception as e:
        logger.error(f"custodial_sign_tx error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/server/sign-tx", methods=["POST"])
@auth_required
def server_sign_tx():
    """Unified server-side tx signing for custodial and Turnkey users on any EVM chain."""
    login_method = session.get("login_method")
    if login_method not in ("custodial", "turnkey"):
        return jsonify({"success": False, "error": "Server signing not available for this login method"}), 403

    from blockchain import CELO_CHAIN_ID, CELO_RPC
    data = request.get_json() or {}
    to_addr = data.get("to", "").strip()
    tx_data = data.get("data", "0x")
    value_hex = data.get("value", "0x0")
    chain_id = int(data.get("chain_id", CELO_CHAIN_ID))
    wait_receipt = bool(data.get("wait_receipt", False))

    if not to_addr:
        return jsonify({"success": False, "error": "to address required"}), 400

    def _get_revert_reason(w3, from_addr, to, data, value, block_number):
        """Replay a failed call to extract the revert reason."""
        try:
            w3.eth.call({"from": from_addr, "to": to, "data": data, "value": value}, block_number)
            return "Transaction reverted (no reason returned)"
        except Exception as exc:
            msg = str(exc)
            # Parse "execution reverted: STF" or similar formats
            for marker in ("execution reverted:", "revert"):
                idx = msg.lower().find(marker)
                if idx != -1:
                    reason = msg[idx:].strip()
                    # Remove hex data suffix if present
                    for sep in (" (", "\n"):
                        reason = reason.split(sep)[0]
                    return reason
            return msg[:200]

    try:
        from web3 import Web3
        from blockchain import XDC_RPC, XDC_CHAIN_ID as XDC_CID
        rpc_url = XDC_RPC if chain_id == XDC_CID else CELO_RPC
        w3 = Web3(Web3.HTTPProvider(rpc_url))

        value_int = int(value_hex, 16) if isinstance(value_hex, str) and value_hex.startswith("0x") else int(value_hex or 0)
        checksum_to = Web3.to_checksum_address(to_addr)

        if login_method == "custodial":
            enc_key = session.get("custodial_key_enc")
            if not enc_key:
                return jsonify({"success": False, "error": "No custodial key in session — please log in again"}), 401
            from eth_account import Account
            fernet = _get_fernet()
            private_key = fernet.decrypt(enc_key.encode()).decode()
            acct = Account.from_key(private_key)
            from_addr = acct.address

            nonce = w3.eth.get_transaction_count(from_addr, "pending")
            gas_price = w3.eth.gas_price
            tx = {"chainId": chain_id, "nonce": nonce, "gasPrice": gas_price,
                  "to": checksum_to, "value": value_int, "data": tx_data}
            try:
                tx["gas"] = w3.eth.estimate_gas({**tx, "from": from_addr})
            except Exception as gas_err:
                # On Celo, gas estimation for native CELO swaps can fail because the
                # block-gas-limit deduction during simulation reduces balanceOf below
                # amountIn (STF). Fall back to a conservative fixed gas limit and let
                # the on-chain receipt check catch any real reverts.
                logger.warning(f"server_sign_tx estimate_gas fallback: {gas_err}")
                tx["gas"] = 300000

            signed = acct.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            if wait_receipt:
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                if receipt.get("status") == 0:
                    reason = _get_revert_reason(w3, from_addr, checksum_to, tx_data, value_int, receipt["blockNumber"])
                    logger.error(f"server_sign_tx revert: {reason} tx={tx_hash.hex()}")
                    return jsonify({"success": False, "error": reason, "tx_hash": "0x" + tx_hash.hex()}), 400
            return jsonify({"success": True, "tx_hash": "0x" + tx_hash.hex()})

        else:  # turnkey
            suborg_id = session.get("turnkey_suborg_id")
            sign_with = session.get("turnkey_sign_with")
            wallet_addr = session.get("wallet")
            if not suborg_id or not sign_with:
                return jsonify({"success": False, "error": "No Turnkey wallet in session — please log in again"}), 400

            from_addr = Web3.to_checksum_address(wallet_addr)
            nonce = w3.eth.get_transaction_count(from_addr, "pending")
            gas_price = w3.eth.gas_price
            tx = {"chainId": chain_id, "nonce": nonce, "gasPrice": gas_price,
                  "to": checksum_to, "value": value_int, "data": tx_data}
            try:
                tx["gas"] = w3.eth.estimate_gas({**tx, "from": from_addr})
            except Exception as gas_err:
                logger.warning(f"server_sign_tx estimate_gas fallback (Turnkey): {gas_err}")
                tx["gas"] = 300000

            from eth_account._utils.legacy_transactions import serializable_unsigned_transaction_from_dict
            import rlp
            unsigned_tx = serializable_unsigned_transaction_from_dict(tx)
            unsigned_bytes = rlp.encode(unsigned_tx)
            unsigned_hex_str = "0x" + unsigned_bytes.hex()

            from turnkey_service import sign_transaction_turnkey
            signed_hex, err = sign_transaction_turnkey(suborg_id, sign_with, unsigned_hex_str)
            if err:
                return jsonify({"success": False, "error": err}), 500

            tx_hash = w3.eth.send_raw_transaction(signed_hex)
            if wait_receipt:
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                if receipt.get("status") == 0:
                    reason = _get_revert_reason(w3, from_addr, checksum_to, tx_data, value_int, receipt["blockNumber"])
                    logger.error(f"server_sign_tx revert: {reason} tx={tx_hash.hex()}")
                    return jsonify({"success": False, "error": reason, "tx_hash": "0x" + tx_hash.hex()}), 400
            return jsonify({"success": True, "tx_hash": "0x" + tx_hash.hex()})

    except Exception as e:
        logger.error(f"server_sign_tx error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ── UBI Gas Faucet (safe claim flow support) ─────────────────────────────────
# Short-term anti-duplicate cache: wallet_address -> unix timestamp of last
# successful refill request (API or on-chain).
_faucet_recent_refill: dict = {}
_faucet_api_pending: dict = {}
_faucet_lock = threading.Lock()

FAUCET_MIN_CELO = float(os.getenv("FAUCET_MIN_CELO", "0.002"))
FAUCET_BUFFER_MULTIPLIER = float(os.getenv("FAUCET_BUFFER_MULTIPLIER", "1.35"))
FAUCET_DUPLICATE_WINDOW_MIN = int(os.getenv("FAUCET_DUPLICATE_WINDOW_MIN", "30"))
FAUCET_API_GRACE_SECONDS = int(os.getenv("FAUCET_API_GRACE_SECONDS", "30"))
FAUCET_PENDING_TTL_SECONDS = int(os.getenv("FAUCET_PENDING_TTL_SECONDS", "180"))
GOODDOLLAR_FAUCET_CONTRACT = os.getenv(
    "GOODDOLLAR_FAUCET_CONTRACT",
    "0x4F93Fa058b03953C851eFaA2e4FC5C34afDFAb84"
)
GOODDOLLAR_FAUCET_API_URL = os.getenv(
    "GOODDOLLAR_FAUCET_API_URL",
    "https://goodserver.gooddollar.org/verify/topWallet"
)


def _validate_and_authorize_wallet(data: dict) -> tuple:
    """Validate requested wallet and ensure it belongs to current session."""
    wallet = session.get("wallet")
    if not wallet:
        return None, jsonify({"success": False, "error": "Not logged in"}), 401

    requested_wallet = (data.get("wallet") or wallet).strip()
    if requested_wallet.lower() != wallet.lower():
        return None, jsonify({
            "success": False,
            "error": "Wrong wallet connected. Please use your logged-in wallet."
        }), 403

    try:
        checksum_wallet = Web3.to_checksum_address(requested_wallet)
    except Exception:
        return None, jsonify({"success": False, "error": "Invalid wallet address"}), 400

    return checksum_wallet, None, None


def _get_gas_status(w3, checksum_wallet: str) -> dict:
    """Estimate claim gas reserve and compare with current CELO balance."""
    from blockchain import GOODDOLLAR_CONTRACTS

    claim_selector = "0x4e71d92d"  # claim()
    try:
        estimated_gas = w3.eth.estimate_gas({
            "from": checksum_wallet,
            "to": Web3.to_checksum_address(GOODDOLLAR_CONTRACTS["UBI_PROXY"]),
            "data": claim_selector,
            "value": 0,
        })
    except Exception:
        estimated_gas = 220000

    gas_price_wei = int(w3.eth.gas_price)
    required_wei = int(estimated_gas * gas_price_wei * FAUCET_BUFFER_MULTIPLIER)
    minimum_wei = w3.to_wei(FAUCET_MIN_CELO, "ether")
    required_wei = max(required_wei, int(minimum_wei))

    balance_wei = int(w3.eth.get_balance(checksum_wallet))
    return {
        "balance_wei": str(balance_wei),
        "balance_celo": float(w3.from_wei(balance_wei, "ether")),
        "estimated_gas": int(estimated_gas),
        "gas_price_wei": str(gas_price_wei),
        "required_gas_wei": str(required_wei),
        "required_gas_celo": float(w3.from_wei(required_wei, "ether")),
        "gas_ready": balance_wei >= required_wei,
    }


def _has_recent_refill(checksum_wallet: str) -> tuple:
    now = time.time()
    with _faucet_lock:
        last = _faucet_recent_refill.get(checksum_wallet.lower(), 0)
    if now - last < FAUCET_DUPLICATE_WINDOW_MIN * 60:
        remaining = int((FAUCET_DUPLICATE_WINDOW_MIN * 60) - (now - last))
        return True, remaining
    return False, 0


def _record_recent_refill(checksum_wallet: str, reason: str = "unknown", source: str = "unknown", tx_hash: str = None):
    with _faucet_lock:
        _faucet_recent_refill[checksum_wallet.lower()] = time.time()
    logger.info(
        f"🧾 Faucet cooldown recorded wallet={checksum_wallet.lower()} source={source} "
        f"reason={reason} tx={tx_hash or 'n/a'}"
    )


def _set_api_pending(checksum_wallet: str, api_tx_hash: str, pre_balance_wei: int):
    with _faucet_lock:
        _faucet_api_pending[checksum_wallet.lower()] = {
            "started_at": time.time(),
            "api_tx_hash": api_tx_hash,
            "pre_balance_wei": int(pre_balance_wei),
        }


def _get_api_pending(checksum_wallet: str):
    now = time.time()
    key = checksum_wallet.lower()
    with _faucet_lock:
        pending = _faucet_api_pending.get(key)
        if not pending:
            return None
        age = now - float(pending.get("started_at", now))
        if age > FAUCET_PENDING_TTL_SECONDS:
            _faucet_api_pending.pop(key, None)
            return None
        return {**pending, "age_seconds": int(age)}


def _clear_api_pending(checksum_wallet: str):
    with _faucet_lock:
        _faucet_api_pending.pop(checksum_wallet.lower(), None)


def _poll_balance_increase(w3, checksum_wallet: str, pre_balance_wei: int, wait_seconds: int, interval_seconds: int = 5):
    """Poll wallet balance for a bounded grace period."""
    checks = max(1, int(wait_seconds / max(1, interval_seconds)))
    for _ in range(checks):
        time.sleep(interval_seconds)
        post_wei = int(w3.eth.get_balance(checksum_wallet))
        if post_wei > pre_balance_wei:
            return post_wei, True
    post_wei = int(w3.eth.get_balance(checksum_wallet))
    return post_wei, post_wei > pre_balance_wei

def _execute_onchain_faucet_topup(w3, checksum_wallet: str) -> dict:
    """Internal helper to send topWallet(address) tx with GAMES_KEY."""
    from blockchain import CELO_CHAIN_ID
    from eth_account import Account

    games_key = (os.getenv("GAMES_KEY") or "").strip()
    if not games_key:
        logger.error(
            f"❌ Faucet onchain unavailable wallet={checksum_wallet.lower()} source=onchain "
            f"reason=missing_games_key"
        )
        return {
            "success": False,
            "status": "onchain_failed",
            "reason": "not_configured",
            "error": "On-chain faucet not configured (missing GAMES_KEY)"
        }

    key = games_key if games_key.startswith("0x") else "0x" + games_key
    faucet_acct = Account.from_key(key)
    faucet_contract = Web3.to_checksum_address(GOODDOLLAR_FAUCET_CONTRACT)

    # calldata for topWallet(address): 0x3771dcf8 + padded wallet bytes
    call_data = "0x3771dcf8" + "000000000000000000000000" + checksum_wallet[2:].lower()
    nonce = w3.eth.get_transaction_count(faucet_acct.address, "pending")

    try:
        gas_est = w3.eth.estimate_gas({
            "from": faucet_acct.address,
            "to": faucet_contract,
            "data": call_data,
        })
    except Exception:
        gas_est = 140000

    tx = {
        "chainId": CELO_CHAIN_ID,
        "nonce": nonce,
        "gasPrice": int(w3.eth.gas_price * 1.2),
        "gas": int(gas_est * 1.2),
        "to": faucet_contract,
        "value": 0,
        "data": call_data,
    }

    signed = faucet_acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hash_hex = "0x" + tx_hash.hex()
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt and receipt.get("status") == 1:
        logger.info(f"✅ Faucet onchain success wallet={checksum_wallet.lower()} source=onchain tx={tx_hash_hex}")
        _record_recent_refill(
            checksum_wallet,
            reason="onchain_tx_success",
            source="onchain",
            tx_hash=tx_hash_hex
        )
        return {"success": True, "status": "onchain_sent", "tx_hash": tx_hash_hex}

    logger.error(f"❌ Faucet onchain failed wallet={checksum_wallet.lower()} source=onchain tx={tx_hash_hex}")
    return {
        "success": False,
        "status": "onchain_failed",
        "error": "On-chain faucet transaction failed",
        "tx_hash": tx_hash_hex
    }


@routes.route("/api/faucet/status", methods=["POST"])
@auth_required
def faucet_status():
    """Step A for safe-claim flow: gas readiness + duplicate refill status."""
    try:
        data = request.get_json(silent=True) or {}
        checksum_wallet, err_resp, status_code = _validate_and_authorize_wallet(data)
        if err_resp:
            return err_resp, status_code

        from blockchain import CELO_RPC
        w3 = Web3(Web3.HTTPProvider(CELO_RPC, request_kwargs={"timeout": 15}))
        gas_status = _get_gas_status(w3, checksum_wallet)
        recent_refill, seconds_remaining = _has_recent_refill(checksum_wallet)
        pending_api = _get_api_pending(checksum_wallet)
        status = "gas_ready" if gas_status.get("gas_ready") else (
            "recent_refill" if recent_refill else ("api_accepted_pending" if pending_api else "api_failed")
        )

        return jsonify({
            "success": True,
            "status": status,
            "wallet": checksum_wallet.lower(),
            "is_recent_refill": recent_refill,
            "recent_refill_cooldown_seconds": seconds_remaining,
            "pending_api": pending_api,
            "debug": {
                "required_gas_wei": gas_status.get("required_gas_wei"),
                "required_gas_celo": gas_status.get("required_gas_celo"),
                "current_balance_wei": gas_status.get("balance_wei"),
                "current_balance_celo": gas_status.get("balance_celo"),
            },
            **gas_status,
        })
    except Exception as e:
        logger.error(f"faucet_status error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/faucet/onchain", methods=["POST"])
@auth_required
def faucet_onchain():
    """Step C fallback: sign/send topWallet(address) using GAMES_KEY."""
    try:
        data = request.get_json(silent=True) or {}
        checksum_wallet, err_resp, status_code = _validate_and_authorize_wallet(data)
        if err_resp:
            return err_resp, status_code

        from blockchain import CELO_RPC
        w3 = Web3(Web3.HTTPProvider(CELO_RPC, request_kwargs={"timeout": 15}))
        onchain_result = _execute_onchain_faucet_topup(w3, checksum_wallet)
        status_code = 200 if onchain_result.get("success") else 502
        if onchain_result.get("reason") == "not_configured":
            status_code = 503
        return jsonify(onchain_result), status_code
    except Exception as e:
        err = str(e)
        logger.error(f"faucet_onchain error: {err}")
        return jsonify({"success": False, "status": "error", "error": err}), 500


@routes.route("/api/faucet/gas", methods=["POST"])
@auth_required
def faucet_gas():
    """Step B: API faucet first, then fallback to /api/faucet/onchain if needed."""
    try:
        data = request.get_json(silent=True) or {}
        checksum_wallet, err_resp, status_code = _validate_and_authorize_wallet(data)
        if err_resp:
            return err_resp, status_code

        from blockchain import CELO_RPC
        w3 = Web3(Web3.HTTPProvider(CELO_RPC, request_kwargs={"timeout": 15}))
        status_before = _get_gas_status(w3, checksum_wallet)
        pre_balance_wei = int(status_before["balance_wei"])
        logger.info(
            f"⛽ Faucet gas request wallet={checksum_wallet.lower()} source=api+fallback "
            f"pre_balance_wei={pre_balance_wei} required_wei={status_before['required_gas_wei']}"
        )
        if status_before["gas_ready"]:
            return jsonify({
                "success": True,
                "wallet": checksum_wallet.lower(),
                "gas_ready": True,
                "topped_up": False,
                "status": "gas_ready",
                "debug": {
                    "pre_balance_wei": str(pre_balance_wei),
                    "post_balance_wei": str(pre_balance_wei),
                    "required_gas_wei": status_before["required_gas_wei"],
                    "required_gas_celo": status_before["required_gas_celo"],
                },
                **status_before,
            })

        recent_refill, seconds_remaining = _has_recent_refill(checksum_wallet)
        if recent_refill:
            return jsonify({
                "success": True,
                "wallet": checksum_wallet.lower(),
                "gas_ready": False,
                "topped_up": False,
                "status": "recent_refill",
                "reason": f"Recent refill detected. Retry after ~{seconds_remaining}s.",
                "recent_refill_cooldown_seconds": seconds_remaining,
                "debug": {
                    "pre_balance_wei": str(pre_balance_wei),
                    "post_balance_wei": str(pre_balance_wei),
                    "required_gas_wei": status_before["required_gas_wei"],
                    "required_gas_celo": status_before["required_gas_celo"],
                    "cooldown_reason": "recent_refill",
                },
                **status_before,
            })

        # Step B: GoodDollar API faucet
        api_ok = False
        api_tx_hash = None
        api_error = None
        try:
            payload = json.dumps({"chainId": 42220, "account": checksum_wallet}).encode("utf-8")
            req = urllib.request.Request(
                GOODDOLLAR_FAUCET_API_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            api_ok = body.get("ok", -1) == 1
            api_tx_hash = body.get("txHash") or body.get("tx_hash")
            api_error = None if api_ok else (body.get("error") or "API faucet declined")
        except Exception as e:
            api_error = str(e)

        onchain_result = None
        topup_source = None
        onchain_fallback_reason = None

        if api_ok:
            _set_api_pending(checksum_wallet, api_tx_hash, pre_balance_wei)
            logger.info(
                f"✅ Faucet API accepted wallet={checksum_wallet.lower()} source=api tx={api_tx_hash or 'n/a'} "
                f"pre_balance_wei={pre_balance_wei}"
            )
            post_balance_wei, increased = _poll_balance_increase(
                w3, checksum_wallet, pre_balance_wei, FAUCET_API_GRACE_SECONDS
            )
            if increased and api_tx_hash:
                status_after_api = _get_gas_status(w3, checksum_wallet)
                _clear_api_pending(checksum_wallet)
                _record_recent_refill(
                    checksum_wallet,
                    reason="api_balance_increase_confirmed",
                    source="api",
                    tx_hash=api_tx_hash
                )
                return jsonify({
                    "success": True,
                    "wallet": checksum_wallet.lower(),
                    "gas_ready": status_after_api["gas_ready"],
                    "topped_up": True,
                    "topup_source": "api",
                    "api_tx_hash": api_tx_hash,
                    "api_error": api_error,
                    "onchain_result": None,
                    "status": "gas_ready" if status_after_api["gas_ready"] else "api_accepted_pending",
                    "debug": {
                        "pre_balance_wei": str(pre_balance_wei),
                        "post_balance_wei": str(post_balance_wei),
                        "required_gas_wei": status_after_api["required_gas_wei"],
                        "required_gas_celo": status_after_api["required_gas_celo"],
                        "required_gas_reserve_wei": status_after_api["required_gas_wei"],
                        "required_gas_reserve_celo": status_after_api["required_gas_celo"],
                    },
                    **status_after_api,
                })
            onchain_fallback_reason = "api_ok_missing_txhash_or_no_balance_increase"
            logger.warning(
                f"⚠️ Faucet API pending unresolved wallet={checksum_wallet.lower()} source=api tx={api_tx_hash or 'n/a'} "
                f"post_balance_wei={post_balance_wei} fallback=onchain reason={onchain_fallback_reason}"
            )
        else:
            onchain_fallback_reason = "api_failed"

        # Step C: on-chain fallback
        onchain_result = _execute_onchain_faucet_topup(w3, checksum_wallet)
        if onchain_result.get("success"):
            topup_source = "onchain"
            _clear_api_pending(checksum_wallet)
        else:
            if api_ok:
                # Keep pending marker visible for status polling/troubleshooting.
                _set_api_pending(checksum_wallet, api_tx_hash, pre_balance_wei)

        status_after = _get_gas_status(w3, checksum_wallet)
        post_balance_wei = int(status_after["balance_wei"])
        topped_up = bool(topup_source)
        logger.info(
            f"⛽ Faucet gas result wallet={checksum_wallet.lower()} source={topup_source or 'none'} "
            f"api_tx={api_tx_hash or 'n/a'} onchain_tx={(onchain_result or {}).get('tx_hash', 'n/a')} "
            f"pre_balance_wei={pre_balance_wei} post_balance_wei={post_balance_wei} "
            f"fallback_reason={onchain_fallback_reason or 'none'}"
        )

        return jsonify({
            "success": bool(status_after["gas_ready"] or topped_up),
            "wallet": checksum_wallet.lower(),
            "gas_ready": status_after["gas_ready"],
            "topped_up": topped_up,
            "topup_source": topup_source,
            "api_tx_hash": api_tx_hash,
            "api_error": api_error,
            "onchain_result": onchain_result,
            "status": (
                "gas_ready" if status_after["gas_ready"] else
                ("onchain_sent" if topped_up else ("api_failed" if onchain_fallback_reason == "api_failed" else "onchain_failed"))
            ),
            "debug": {
                "pre_balance_wei": str(pre_balance_wei),
                "post_balance_wei": str(post_balance_wei),
                "required_gas_wei": status_after["required_gas_wei"],
                "required_gas_celo": status_after["required_gas_celo"],
                "required_gas_reserve_wei": status_after["required_gas_wei"],
                "required_gas_reserve_celo": status_after["required_gas_celo"],
                "fallback_reason": onchain_fallback_reason,
            },
            **status_after,
        })
    except Exception as e:
        logger.error(f"faucet_gas error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# Backward-compat endpoint used by existing clients.
@routes.route("/api/gas-faucet", methods=["POST"])
@auth_required
def gas_faucet_compat():
    return faucet_gas()


@routes.route("/api/custodial/sign-msg", methods=["POST"])
@auth_required
def custodial_sign_msg():
    """Sign a personal message server-side for custodial (private key) users."""
    if session.get("login_method") != "custodial":
        return jsonify({"success": False, "error": "Not in custodial mode"}), 403
    enc_key = session.get("custodial_key_enc")
    if not enc_key:
        return jsonify({"success": False, "error": "No custodial key in session — please log in again"}), 401
    try:
        from eth_account import Account
        fernet = _get_fernet()
        private_key = fernet.decrypt(enc_key.encode()).decode()
        data = request.get_json() or {}
        message = data.get("message", "")
        if not message:
            return jsonify({"success": False, "error": "message required"}), 400
        acct = Account.from_key(private_key)
        signed = acct.sign_message(
            __import__('eth_account.messages', fromlist=['encode_defunct']).encode_defunct(text=message)
        )
        sig_hex = signed.signature.hex()
        if not sig_hex.startswith('0x'):
            sig_hex = '0x' + sig_hex
        return jsonify({"success": True, "signature": sig_hex})
    except Exception as e:
        logger.error(f"custodial_sign_msg error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/turnkey/status", methods=["GET"])
def turnkey_status():
    """Check if the current session has a Turnkey wallet connected."""
    suborg_id = session.get("turnkey_suborg_id")
    wallet = session.get("wallet")
    return jsonify({
        "has_turnkey": bool(suborg_id),
        "suborg_id": suborg_id,
        "wallet": wallet,
        "login_method": session.get("login_method", "walletconnect")
    })


@routes.route("/api/wallet/export-key", methods=["POST"])
@auth_required
def export_private_key():
    """Return the decrypted private key for custodial-mode users (session only)."""
    login_method = session.get("login_method")
    if login_method == "custodial":
        enc_key = session.get("custodial_key_enc")
        if not enc_key:
            return jsonify({"success": False, "error": "No key in session — please log in again"}), 401
        try:
            fernet = _get_fernet()
            private_key = fernet.decrypt(enc_key.encode()).decode()
            return jsonify({"success": True, "private_key": private_key})
        except Exception as e:
            logger.error(f"export-key (custodial) error: {e}")
            return jsonify({"success": False, "error": "Could not decrypt key"}), 500

    if login_method == "turnkey":
        try:
            data = request.get_json(silent=True) or {}
            email = _normalize_email(data.get("email", session.get("turnkey_export_email_verified", "")))
            if not email or not _turnkey_export_recently_verified(email):
                return jsonify({
                    "success": False,
                    "error": "Please verify your email code first to export this key."
                }), 403

            suborg_id = session.get("turnkey_suborg_id")
            wallet = session.get("wallet")
            if not suborg_id or not wallet:
                return jsonify({"success": False, "error": "Turnkey wallet session missing. Please log in again."}), 401

            from turnkey_service import export_wallet_account_turnkey
            private_key, err = export_wallet_account_turnkey(suborg_id, wallet)
            if err:
                return jsonify({"success": False, "error": _format_turnkey_export_error(err)}), 500
            return jsonify({"success": True, "private_key": private_key})
        except Exception as e:
            logger.error(f"export-key (turnkey) error: {e}")
            return jsonify({"success": False, "error": "Could not export Turnkey key"}), 500

    return jsonify({"success": False, "error": "Only available for custodial and Turnkey wallets"}), 403


@routes.route("/api/notifications", methods=["GET"])
def get_notifications():
    """Return user notifications."""
    try:
        wallet = session.get("wallet")
        if not wallet:
            return json.dumps({"success": False, "message": "Not authenticated"}), 401, {"Content-Type": "application/json"}

        limit = int(request.args.get("limit", 50))
        result = notification_service.get_all_notifications(wallet, limit)

        notifications = result.get("notifications", [])
        has_broadcast = any(n.get("type") == "admin_broadcast" for n in notifications)

        return json.dumps({
            "success": True,
            "notifications": notifications,
            "unread_count": result.get("unread_count", 0),
            "total_count": result.get("total_count", 0),
            "has_broadcast": has_broadcast
        }), 200, {"Content-Type": "application/json"}
    except Exception as e:
        logger.error(f"Error fetching notifications: {e}")
        return json.dumps({"success": False, "message": "Server error"}), 500, {"Content-Type": "application/json"}


@routes.route("/api/notifications/mark-read", methods=["POST"])
def mark_notifications_read():
    """Mark notifications as read."""
    try:
        wallet = session.get("wallet")
        if not wallet:
            return json.dumps({"success": False, "message": "Not authenticated"}), 401, {"Content-Type": "application/json"}

        data = request.get_json() or {}
        notification_ids = data.get("notification_ids", [])
        result = notification_service.mark_notifications_read(wallet, notification_ids)
        return json.dumps(result), 200, {"Content-Type": "application/json"}
    except Exception as e:
        logger.error(f"Error marking notifications read: {e}")
        return json.dumps({"success": False, "message": "Server error"}), 500, {"Content-Type": "application/json"}


# ─────────────────────────────────────────────────────────
#  DAILY VOUCHER
# ─────────────────────────────────────────────────────────

def _get_today_pht():
    """Return the current date string (YYYY-MM-DD) in PHT (UTC+8)."""
    from datetime import datetime, timezone, timedelta
    pht = timezone(timedelta(hours=8))
    return datetime.now(pht).strftime("%Y-%m-%d")


@routes.route("/api/voucher/daily", methods=["GET"])
@auth_required
def get_daily_voucher():
    """Return the active daily voucher for today if not yet claimed."""
    try:
        today = _get_today_pht()
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": True, "voucher": None, "reason": "db_unavailable"})

        result = safe_supabase_operation(
            lambda: supabase.table("daily_voucher")
                .select("id, voucher_link, is_claimed, claimed_at, voucher_date")
                .eq("voucher_date", today)
                .limit(1)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="get daily voucher"
        )

        if not result or not result.data:
            return jsonify({"success": True, "voucher": None, "reason": "no_voucher_today"})

        row = result.data[0]
        if row.get("is_claimed"):
            return jsonify({"success": True, "voucher": None, "reason": "already_claimed"})

        return jsonify({
            "success": True,
            "voucher": {
                "id": row["id"],
                "voucher_link": row["voucher_link"],
                "voucher_date": row["voucher_date"],
            }
        })
    except Exception as e:
        logger.error(f"get_daily_voucher error: {e}")
        return jsonify({"success": False, "voucher": None, "error": str(e)}), 500


@routes.route("/api/voucher/claim", methods=["POST"])
@auth_required
def claim_daily_voucher():
    """Mark today's voucher as claimed. First user to call this wins."""
    try:
        from datetime import datetime, timezone
        wallet = session.get("wallet")
        today = _get_today_pht()
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 503

        result = safe_supabase_operation(
            lambda: supabase.table("daily_voucher")
                .select("id, is_claimed, voucher_link")
                .eq("voucher_date", today)
                .limit(1)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="fetch voucher for claim"
        )

        if not result or not result.data:
            return jsonify({"success": False, "error": "No voucher available today."}), 404

        row = result.data[0]
        if row.get("is_claimed"):
            return jsonify({"success": False, "error": "Voucher already claimed!", "already_claimed": True}), 409

        safe_supabase_operation(
            lambda: supabase.table("daily_voucher")
                .update({
                    "is_claimed": True,
                    "claimed_by": wallet,
                    "claimed_at": datetime.now(timezone.utc).isoformat()
                })
                .eq("id", row["id"])
                .eq("is_claimed", False)
                .execute(),
            operation_name="mark voucher claimed"
        )

        return jsonify({"success": True, "voucher_link": row["voucher_link"]})
    except Exception as e:
        logger.error(f"claim_daily_voucher error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/voucher/confirm", methods=["POST"])
@auth_required
def confirm_voucher_claim():
    """Save the on-chain tx_hash and G$ amount after a successful voucher claim."""
    try:
        from datetime import datetime, timezone
        wallet = session.get("wallet")
        data = request.get_json() or {}
        tx_hash = (data.get("tx_hash") or "").strip()
        gd_amount = float(data.get("gd_amount") or 0)
        voucher_date = (data.get("voucher_date") or _get_today_pht()).strip()

        if not tx_hash:
            return jsonify({"success": False, "error": "tx_hash is required"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 503

        safe_supabase_operation(
            lambda: supabase.table("voucher_claims_log")
                .insert({
                    "wallet_address": wallet,
                    "voucher_date": voucher_date,
                    "tx_hash": tx_hash,
                    "gd_amount": gd_amount,
                    "claimed_at": datetime.now(timezone.utc).isoformat()
                })
                .execute(),
            operation_name="insert voucher claim log"
        )
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"confirm_voucher_claim error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/voucher", methods=["POST"])
@admin_required
def admin_set_voucher():
    """Admin: set or update today's daily voucher link."""
    try:
        wallet = session.get("wallet")
        data = request.get_json()
        voucher_link = (data.get("voucher_link") or "").strip()
        voucher_date = (data.get("voucher_date") or _get_today_pht()).strip()

        if not voucher_link:
            return jsonify({"success": False, "error": "voucher_link is required"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 503

        existing = safe_supabase_operation(
            lambda: supabase.table("daily_voucher")
                .select("id")
                .eq("voucher_date", voucher_date)
                .limit(1)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="check existing voucher"
        )

        if existing and existing.data:
            safe_supabase_operation(
                lambda: supabase.table("daily_voucher")
                    .update({"voucher_link": voucher_link, "is_claimed": False, "claimed_by": None, "claimed_at": None, "created_by": wallet})
                    .eq("voucher_date", voucher_date)
                    .execute(),
                operation_name="update voucher link"
            )
        else:
            safe_supabase_operation(
                lambda: supabase.table("daily_voucher")
                    .insert({"voucher_date": voucher_date, "voucher_link": voucher_link, "is_claimed": False, "created_by": wallet})
                    .execute(),
                operation_name="insert voucher"
            )

        log_admin_action(wallet, "set_daily_voucher", {"voucher_date": voucher_date, "voucher_link": voucher_link})
        return jsonify({"success": True, "voucher_date": voucher_date})
    except Exception as e:
        logger.error(f"admin_set_voucher error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/voucher", methods=["GET"])
@admin_required
def admin_get_voucher():
    """Admin: get current voucher status for today."""
    try:
        today = _get_today_pht()
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 503

        result = safe_supabase_operation(
            lambda: supabase.table("daily_voucher")
                .select("id, voucher_date, voucher_link, is_claimed, claimed_by, claimed_at, created_by")
                .eq("voucher_date", today)
                .limit(1)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="admin get voucher"
        )

        if not result or not result.data:
            return jsonify({"success": True, "voucher": None, "today": today})

        return jsonify({"success": True, "voucher": result.data[0], "today": today})
    except Exception as e:
        logger.error(f"admin_get_voucher error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/voucher/delete", methods=["POST"])
@admin_required
def admin_delete_voucher():
    """Admin: completely delete today's voucher so it no longer shows on any dashboard."""
    try:
        wallet = session.get("wallet")
        today = _get_today_pht()
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 503

        safe_supabase_operation(
            lambda: supabase.table("daily_voucher")
                .delete()
                .eq("voucher_date", today)
                .execute(),
            operation_name="delete voucher"
        )

        log_admin_action(wallet, "delete_daily_voucher", {"voucher_date": today})
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"admin_delete_voucher error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/voucher/reset", methods=["POST"])
@admin_required
def admin_reset_voucher():
    """Admin: reset today's voucher claim status so it becomes available again."""
    try:
        wallet = session.get("wallet")
        today = _get_today_pht()
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 503

        safe_supabase_operation(
            lambda: supabase.table("daily_voucher")
                .update({"is_claimed": False, "claimed_by": None, "claimed_at": None})
                .eq("voucher_date", today)
                .execute(),
            operation_name="reset voucher"
        )

        log_admin_action(wallet, "reset_daily_voucher", {"voucher_date": today})
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"admin_reset_voucher error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ── Unified Treasury Routes ────────────────────────────────────────────────────

@routes.route("/api/admin/treasury/status", methods=["GET"])
@admin_required
def admin_treasury_status():
    """Return current Unified Treasury balance, stats, and recipient addresses."""
    try:
        from unified_treasury.service import get_treasury_status
        status = get_treasury_status()
        return jsonify(status)
    except Exception as e:
        logger.error(f"admin_treasury_status error: {e}")
        return jsonify({"configured": False, "error": str(e)}), 500


@routes.route("/api/admin/treasury/distribute", methods=["POST"])
@admin_required
def admin_treasury_distribute():
    """
    Distribute G$ from the Unified Treasury to a hardcoded recipient.
    Body: { "recipient_key": "learn_earn"|"daily_task"|"discourse"|
                             "minigames"|"community_stories"|"referral",
            "amount": <float G$> }
    """
    try:
        from unified_treasury.service import distribute_funds, RECIPIENT_LABELS
        wallet = session.get("wallet")
        data   = request.get_json(force=True) or {}

        recipient_key = data.get("recipient_key", "").strip()
        amount        = float(data.get("amount", 0))

        if not recipient_key:
            return jsonify({"success": False, "error": "recipient_key is required"}), 400
        if recipient_key not in RECIPIENT_LABELS:
            return jsonify({"success": False, "error": f"Unknown recipient: {recipient_key}"}), 400
        if amount <= 0:
            return jsonify({"success": False, "error": "Amount must be greater than 0"}), 400

        result = distribute_funds(recipient_key, amount)

        if result.get("success"):
            log_admin_action(wallet, "treasury_distribute", {
                "recipient_key":   recipient_key,
                "recipient_label": RECIPIENT_LABELS.get(recipient_key),
                "amount_gd":       amount,
                "tx_hash":         result.get("tx_hash"),
            })

        return jsonify(result)
    except Exception as e:
        logger.error(f"admin_treasury_distribute error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
