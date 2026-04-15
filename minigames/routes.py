import logging
import asyncio
from flask import Blueprint, request, jsonify, render_template, session, redirect
from .minigames_manager import minigames_manager
from maintenance_service import maintenance_service

logger = logging.getLogger(__name__)

minigames_bp = Blueprint('minigames', __name__, url_prefix='/minigames')

@minigames_bp.route('/')
def minigames_home():
    """Minigames dashboard"""
    wallet = session.get('wallet') or session.get('wallet_address')
    verified = session.get('verified') or session.get('ubi_verified')

    if not wallet or not verified:
        return redirect('/')

    # Check maintenance mode from database
    maintenance_status = maintenance_service.get_maintenance_status('minigames')
    if maintenance_status.get('is_maintenance', False):
        maintenance_message = maintenance_status.get('message', 'Minigames are temporarily under maintenance. Please check back later.')
        return render_template('minigames.html', wallet=wallet, maintenance_mode=True, maintenance_message=maintenance_message)

    return render_template('minigames.html', wallet=wallet, maintenance_mode=False)

@minigames_bp.route('/api/check-limit/<game_type>')
def check_game_limit(game_type):
    """Check if user can play a game"""
    # Check maintenance mode from database
    maintenance_status = maintenance_service.get_maintenance_status('minigames')
    if maintenance_status.get('is_maintenance', False):
        return jsonify({'error': maintenance_status.get('message', 'Minigames are temporarily under maintenance')}), 503

    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            return jsonify({'error': 'Not authenticated'}), 401

        # Removed coin_flip game type check
        if game_type == 'coin_flip':
            return jsonify({'success': False, 'error': 'Coin flip game is not available'}), 404

        limit_check = minigames_manager.check_daily_limit(wallet, game_type)

        return jsonify({
            'success': True,
            'limit_check': limit_check
        })

    except Exception as e:
        logger.error(f"❌ Error checking game limit: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@minigames_bp.route('/api/start-game', methods=['POST'])
def start_game():
    """Start a new minigame session"""
    # Check maintenance mode from database
    maintenance_status = maintenance_service.get_maintenance_status('minigames')
    if maintenance_status.get('is_maintenance', False):
        return jsonify({'error': maintenance_status.get('message', 'Minigames are temporarily under maintenance')}), 503

    try:
        wallet_address = session.get('wallet_address')
        if not wallet_address:
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        data = request.json
        game_type = data.get('game_type')
        bet_amount = data.get('bet_amount', 0)

        if not game_type:
            return jsonify({'success': False, 'error': 'Game type required'}), 400

        # Removed coin_flip game type check
        if game_type == 'coin_flip':
            return jsonify({'success': False, 'error': 'Coin flip game is not available'}), 404

        result = minigames_manager.start_game_session(wallet_address, game_type, bet_amount)
        return jsonify(result)

    except Exception as e:
        logger.error(f"❌ Error starting game: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@minigames_bp.route('/api/complete-game', methods=['POST'])
def complete_game():
    """Complete a game session"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            return jsonify({'error': 'Not authenticated'}), 401

        data = request.get_json()
        session_id = data.get('session_id')
        score = data.get('score', 0)
        game_data = data.get('game_data', {})

        if not session_id:
            return jsonify({'success': False, 'error': 'Session ID required'}), 400

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                minigames_manager.complete_game_session(session_id, score, game_data)
            )
        finally:
            loop.close()

        return jsonify(result)

    except Exception as e:
        logger.error(f"❌ Error completing game: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@minigames_bp.route('/api/user-stats')
def get_user_stats():
    """Get user game statistics with total virtual tokens across all games"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            logger.warning("⚠️ Unauthenticated request to /api/user-stats")
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        logger.info(f"📊 Getting user stats for {wallet[:8]}...")
        result = minigames_manager.get_user_stats(wallet)

        # Always ensure we have a valid response structure
        stats = result.get('stats', [])
        logger.info(f"📊 Retrieved {len(stats)} game stats for {wallet[:8]}...")

        total_tokens = sum(stat.get('virtual_tokens', 0) for stat in stats)

        logger.info(f"💰 Total tokens across all games for {wallet[:8]}...: {total_tokens}")

        # Log individual game tokens for debugging
        if stats:
            for stat in stats:
                game_type = stat.get('game_type', 'unknown')
                tokens = stat.get('virtual_tokens', 0)
                plays = stat.get('total_plays', 0)
                logger.info(f"   {game_type}: {tokens} tokens ({plays} plays)")
        else:
            logger.info(f"   No game stats found - user hasn't played any games yet")

        # Always return success with proper data structure
        response_data = {
            'success': True,
            'stats': stats,
            'total_virtual_tokens': total_tokens
        }

        logger.info(f"✅ Returning response: {response_data}")

        return jsonify(response_data)

    except Exception as e:
        logger.error(f"❌ Error getting user stats: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        # Return error with proper structure
        return jsonify({
            'success': False,
            'stats': [],
            'total_virtual_tokens': 0,
            'error': str(e)
        }), 500

@minigames_bp.route('/api/balance')
def get_balance():
    """Get user's Play & Earn balance"""
    try:
        wallet = session.get('wallet') or session.get('wallet_address')
        if not wallet or not session.get('verified'):
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        result = minigames_manager.get_deposit_balance(wallet)
        min_withdrawal = minigames_manager.MIN_WITHDRAWAL
        available = result.get('available_balance', 0)
        return jsonify({
            'success': True,
            'available_balance': available,
            'total_withdrawn': result.get('total_withdrawn', 0),
            'min_withdrawal': min_withdrawal,
            'can_withdraw': available >= min_withdrawal
        })
    except Exception as e:
        logger.error(f"❌ Error getting balance: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@minigames_bp.route('/api/withdraw', methods=['POST'])
def withdraw():
    """Withdraw Play & Earn balance"""
    try:
        wallet = session.get('wallet') or session.get('wallet_address')
        if not wallet or not session.get('verified'):
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                minigames_manager.withdraw_winnings(wallet)
            )
        finally:
            loop.close()

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error processing withdrawal: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@minigames_bp.route('/api/withdrawal-history')
def withdrawal_history():
    """Get user's withdrawal transaction history"""
    try:
        wallet = session.get('wallet') or session.get('wallet_address')
        if not wallet or not session.get('verified'):
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        from supabase_client import get_supabase_client
        sb = get_supabase_client()
        res = sb.table('minigame_withdrawals_log')\
            .select('*')\
            .eq('wallet_address', wallet)\
            .order('withdrawal_date', desc=True)\
            .limit(20)\
            .execute()

        return jsonify({'success': True, 'withdrawals': res.data or []})
    except Exception as e:
        logger.error(f"❌ Error fetching withdrawal history: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@minigames_bp.route('/api/quiz-questions')
def get_quiz_questions():
    """Get quiz questions"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            return jsonify({'error': 'Not authenticated'}), 401

        difficulty = request.args.get('difficulty')
        questions = minigames_manager.get_quiz_questions(difficulty)

        return jsonify({
            'success': True,
            'questions': questions
        })

    except Exception as e:
        logger.error(f"❌ Error getting quiz questions: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

