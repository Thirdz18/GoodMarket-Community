from flask import Blueprint, request, jsonify, session, render_template, redirect
import logging
import asyncio
import time
from .community_stories_service import community_stories_service
from config import COMMUNITY_STORIES_CONFIG
from supabase_client import get_supabase_client, safe_supabase_operation
import os
import base64
import requests
import uuid

logger = logging.getLogger(__name__)

community_stories_bp = Blueprint('community_stories', __name__)

@community_stories_bp.route('/')
def community_stories_page():
    """Community Stories main page - Publicly accessible"""
    wallet = session.get('wallet')
    verified = session.get('verified')

    # Allow both authenticated and guest users to view the page
    return render_template('community_stories.html', 
                         wallet=wallet if wallet and verified else None)

@community_stories_bp.route('/api/config', methods=['GET'])
def get_config():
    """Get Community Stories configuration"""
    try:
        config = community_stories_service.get_config()
        
        # Get custom message from database
        from supabase_client import get_supabase_client, safe_supabase_operation
        supabase = get_supabase_client()
        custom_message = COMMUNITY_STORIES_CONFIG['DESCRIPTION']  # Default fallback
        
        if supabase:
            result = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')\
                    .select('custom_message')\
                    .eq('feature_name', 'community_stories_message')\
                    .execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="get community stories custom message"
            )
            
            if result.data and len(result.data) > 0 and result.data[0].get('custom_message'):
                custom_message = result.data[0]['custom_message']
                logger.info(f"✅ Using custom Community Stories message from database")
        
        return jsonify({
            'success': True,
            'config': {
                'rewards': {
                    'low': config['LOW_REWARD'],
                    'high': config['HIGH_REWARD']
                },
                'requirements': {
                    'mentions': config['REQUIRED_MENTIONS'],
                    'min_video_duration': COMMUNITY_STORIES_CONFIG['MIN_VIDEO_DURATION']
                },
                'window': {
                    'start_day': config['WINDOW_START_DAY'],
                    'end_day': config['WINDOW_END_DAY']
                },
                'description': custom_message  # Use custom message from DB
            }
        })
    except Exception as e:
        logger.error(f"❌ Error getting config: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@community_stories_bp.route('/api/status', methods=['GET'])
def get_status():
    """Get participation window status and user eligibility"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        # Check window
        window = community_stories_service.is_participation_window_open()

        # Check cooldown
        cooldown = community_stories_service.check_user_cooldown(wallet)

        # Check pending submission
        pending = community_stories_service.has_pending_submission(wallet)

        return jsonify({
            'success': True,
            'window': window,
            'cooldown': cooldown,
            'pending': pending
        })

    except Exception as e:
        logger.error(f"❌ Error getting status: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@community_stories_bp.route('/api/submit', methods=['POST'])
def submit_tweet():
    """Submit tweet URL"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        data = request.get_json()
        tweet_url = data.get('tweet_url', '').strip()

        if not tweet_url:
            return jsonify({'success': False, 'error': 'Tweet URL required'}), 400

        result = community_stories_service.submit_tweet(wallet, tweet_url)

        return jsonify(result)

    except Exception as e:
        logger.error(f"❌ Error submitting tweet: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@community_stories_bp.route('/api/submit-screenshot', methods=['POST'])
def submit_screenshot():
    """Submit screenshot directly (for participants)"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            logger.error("❌ Submit screenshot: Not authenticated")
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        # Check if file was uploaded
        if 'image' not in request.files:
            return jsonify({'success': False, 'error': 'No image file provided'}), 400

        file = request.files['image']

        if file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'}), 400

        # Validate file type
        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}
        file_ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
        if file_ext not in allowed_extensions:
            return jsonify({'success': False, 'error': 'Invalid file type. Allowed: png, jpg, jpeg, gif, bmp, webp'}), 400

        # Check participation window
        window = community_stories_service.is_participation_window_open()
        if not window['is_open']:
            return jsonify({
                'success': False,
                'error': 'Participation window closed',
                'next_window': window['next_window']
            })

        # CRITICAL: Check if user already has a PENDING submission
        # Users can only submit ONCE - they must wait for approval/rejection
        pending_check = community_stories_service.has_pending_submission(wallet)
        if pending_check.get('has_pending'):
            return jsonify({
                'success': False,
                'error': 'You already have a pending submission. Please wait for admin approval.',
                'pending_submission': pending_check.get('pending_submission')
            })

        # Check if user already RECEIVED a reward this month
        # Cooldown only activates AFTER reward is disbursed
        cooldown = community_stories_service.check_user_cooldown(wallet)
        if not cooldown.get('can_participate'):
            return jsonify({
                'success': False,
                'error': 'Already received reward this month',
                'next_participation': cooldown.get('next_participation')
            })

        # Get ImgBB API key from environment
        imgbb_api_key = os.getenv('IMGBB_API_KEY')

        if not imgbb_api_key:
            logger.error("❌ ImgBB API key not configured")
            return jsonify({'success': False, 'error': 'Image upload service not configured. Please contact admin.'}), 500

        # Upload to ImgBB
        logger.info(f"📤 User {wallet[:8]}... uploading screenshot to ImgBB...")

        # Read and encode image
        image_data = base64.b64encode(file.read()).decode('utf-8')

        # Upload to ImgBB
        upload_url = 'https://api.imgbb.com/1/upload'
        payload = {
            'key': imgbb_api_key,
            'image': image_data,
            'name': file.filename
        }

        response = requests.post(upload_url, data=payload, timeout=30)

        if response.status_code != 200:
            logger.error(f"❌ ImgBB upload failed: {response.status_code} - {response.text}")
            return jsonify({'success': False, 'error': f'Image upload failed: {response.status_code}'}), 500

        upload_result = response.json()

        if not upload_result.get('success'):
            logger.error(f"❌ ImgBB API error: {upload_result}")
            return jsonify({'success': False, 'error': 'Image upload failed'}), 500

        # Get the image URL
        screenshot_url = upload_result['data']['url']

        logger.info(f"✅ Image uploaded to ImgBB: {screenshot_url}")

        # Generate unique submission ID
        submission_id = f"CS{uuid.uuid4().hex[:12].upper()}"

        logger.info(f"🔑 Generated submission ID: {submission_id}")

        # Create submission with screenshot
        result = community_stories_service.submit_screenshot(wallet, screenshot_url, submission_id)

        return jsonify(result)

    except Exception as e:
        logger.error(f"❌ Error submitting screenshot: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500

@community_stories_bp.route('/api/admin/update-settings', methods=['POST'])
def update_settings():
    """Update Community Stories configuration (admin only)"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        from supabase_client import is_admin
        if not is_admin(wallet):
            return jsonify({'success': False, 'error': 'Admin access required'}), 403

        data = request.get_json()
        
        # Validate data
        low_reward = data.get('low_reward')
        high_reward = data.get('high_reward')
        required_mentions = data.get('required_mentions')
        window_start_day = data.get('window_start_day')
        window_end_day = data.get('window_end_day')
        message = data.get('message')

        if not all([low_reward, high_reward, required_mentions, window_start_day, window_end_day]):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        # Save to database (maintenance_settings table is used for dynamic config)
        from supabase_client import get_supabase_client
        supabase = get_supabase_client()
        
        import json
        config_json = json.dumps({
            'low_reward': low_reward,
            'high_reward': high_reward,
            'required_mentions': required_mentions,
            'window_start_day': window_start_day,
            'window_end_day': window_end_day
        })

        # Update or insert config
        supabase.table('maintenance_settings').upsert({
            'feature_name': 'community_stories_config',
            'custom_message': config_json,
            'is_enabled': True
        }, on_conflict='feature_name').execute()

        # Update message
        if message:
            supabase.table('maintenance_settings').upsert({
                'feature_name': 'community_stories_message',
                'custom_message': message,
                'is_enabled': True
            }, on_conflict='feature_name').execute()

        return jsonify({'success': True, 'message': 'Settings updated successfully'})

    except Exception as e:
        logger.error(f"❌ Error updating settings: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@community_stories_bp.route('/api/admin/notifications', methods=['GET'])
def get_admin_notifications():
    """Get admin notifications (admin only)"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        from supabase_client import is_admin
        if not is_admin(wallet):
            return jsonify({'success': False, 'error': 'Admin access required'}), 403

        result = community_stories_service.get_admin_notifications(wallet)

        return jsonify(result)

    except Exception as e:
        logger.error(f"❌ Error getting admin notifications: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@community_stories_bp.route('/api/admin/approve', methods=['POST'])
def approve_submission():
    """Approve submission and disburse reward (admin only)"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            logger.error(f"❌ Approve submission: Not authenticated")
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        from supabase_client import is_admin
        if not is_admin(wallet):
            logger.error(f"❌ Approve submission: Not admin - {wallet[:8]}...")
            return jsonify({'success': False, 'error': 'Admin access required'}), 403

        data = request.get_json()
        submission_id = data.get('submission_id')
        reward_type = data.get('reward_type')  # 'low' or 'high'

        logger.info(f"📝 Admin {wallet[:8]}... approving submission {submission_id} as {reward_type}")

        if not submission_id or not reward_type:
            logger.error(f"❌ Missing fields - submission_id: {submission_id}, reward_type: {reward_type}")
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        # Validate reward_type
        if reward_type not in ['low', 'high']:
            logger.error(f"❌ Invalid reward_type: {reward_type}")
            return jsonify({'success': False, 'error': 'Invalid reward type. Must be "low" or "high"'}), 400

        # Run async function
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                community_stories_service.approve_submission(submission_id, reward_type, wallet)
            )
            logger.info(f"📊 Approval result: {result.get('success')} - {result.get('error', 'Success')}")
        finally:
            loop.close()

        if result.get('success'):
            return jsonify(result)
        else:
            return jsonify(result), 400

    except Exception as e:
        logger.error(f"❌ Error approving submission: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500

@community_stories_bp.route('/api/admin/reject', methods=['POST'])
def reject_submission():
    """Reject submission (admin only)"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        from supabase_client import is_admin
        if not is_admin(wallet):
            return jsonify({'success': False, 'error': 'Admin access required'}), 403

        data = request.get_json()
        submission_id = data.get('submission_id')
        reason = data.get('reason')

        if not submission_id:
            return jsonify({'success': False, 'error': 'Missing submission_id'}), 400

        result = community_stories_service.reject_submission(submission_id, wallet, reason)

        return jsonify(result)

    except Exception as e:
        logger.error(f"❌ Error rejecting submission: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@community_stories_bp.route('/api/admin/upload-screenshot', methods=['POST'])
def upload_screenshot():
    """Upload image directly to ImgBB and save to database"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            logger.error("❌ Upload screenshot: Not authenticated")
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        from supabase_client import is_admin
        if not is_admin(wallet):
            logger.error(f"❌ Upload screenshot: Not admin - {wallet[:8]}...")
            return jsonify({'success': False, 'error': 'Admin access required'}), 403

        # Check if file was uploaded
        if 'image' not in request.files:
            return jsonify({'success': False, 'error': 'No image file provided'}), 400

        file = request.files['image']
        wallet_address = request.form.get('wallet_address', '').strip()

        if file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'}), 400

        # Validate file type
        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}
        file_ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
        if file_ext not in allowed_extensions:
            return jsonify({'success': False, 'error': 'Invalid file type. Allowed: png, jpg, jpeg, gif, bmp, webp'}), 400

        # Get ImgBB API key from environment
        imgbb_api_key = os.getenv('IMGBB_API_KEY')

        if not imgbb_api_key:
            logger.error("❌ ImgBB API key not configured")
            return jsonify({'success': False, 'error': 'ImgBB API key not configured. Please add IMGBB_API_KEY to Secrets.'}), 500

        # Upload to ImgBB
        logger.info(f"📤 Uploading image to ImgBB...")

        # Read and encode image
        image_data = base64.b64encode(file.read()).decode('utf-8')

        # Upload to ImgBB
        upload_url = 'https://api.imgbb.com/1/upload'
        payload = {
            'key': imgbb_api_key,
            'image': image_data,
            'name': file.filename
        }

        response = requests.post(upload_url, data=payload, timeout=30)

        if response.status_code != 200:
            logger.error(f"❌ ImgBB upload failed: {response.status_code} - {response.text}")
            return jsonify({'success': False, 'error': f'ImgBB upload failed: {response.status_code}'}), 500

        upload_result = response.json()

        if not upload_result.get('success'):
            logger.error(f"❌ ImgBB API error: {upload_result}")
            return jsonify({'success': False, 'error': 'ImgBB upload failed'}), 500

        # Get the image URL
        screenshot_url = upload_result['data']['url']

        logger.info(f"✅ Image uploaded to ImgBB: {screenshot_url}")

        # Use placeholder if no wallet provided
        if not wallet_address:
            wallet_address = '0x0000000000000000000000000000000000000000'

        logger.info(f"📸 Admin {wallet[:8]}... saving screenshot for {wallet_address[:8]}...")

        # Generate unique submission ID
        submission_id = f"CS{uuid.uuid4().hex[:12].upper()}"

        logger.info(f"🔑 Generated submission ID: {submission_id}")

        # Create a screenshot entry in database with ImgBB URL
        result = community_stories_service.create_screenshot_entry(
            wallet_address, 
            screenshot_url,
            submission_id
        )

        if result.get('success'):
            logger.info(f"✅ Screenshot entry created: {submission_id}")
            result['screenshot_url'] = screenshot_url  # Include URL in response
        else:
            logger.error(f"❌ Failed to create DB entry: {result.get('error')}")

        return jsonify(result)

    except Exception as e:
        logger.error(f"❌ Error uploading screenshot: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500

@community_stories_bp.route('/api/my-submissions', methods=['GET'])
def get_my_submissions():
    """Get user's submission history"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        result = community_stories_service.get_user_submissions(wallet)

        return jsonify(result)

    except Exception as e:
        logger.error(f"❌ Error getting submissions: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@community_stories_bp.route('/api/admin/history', methods=['GET'])
def get_admin_history():
    """Get processed submissions history (admin only)"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        from supabase_client import is_admin
        if not is_admin(wallet):
            return jsonify({'success': False, 'error': 'Admin access required'}), 403

        result = community_stories_service.get_submission_history()

        return jsonify(result)

    except Exception as e:
        logger.error(f"❌ Error getting history: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@community_stories_bp.route('/api/requirement-example-images', methods=['GET'])
def get_requirement_example_images():
    """Get requirement example images (selfie examples for higher reward)"""
    try:
        limit = int(request.args.get('limit', 3))
        
        # Get approved high reward submissions with screenshots as examples
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database not available', 'images': []}), 200
        
        # Get approved_high submissions with screenshots (these are good examples)
        result = safe_supabase_operation(
            lambda: supabase.table('community_stories_submissions')\
                .select('submission_id, storage_path, wallet_address, reviewed_at')\
                .eq('status', 'approved_high')\
                .not_.is_('storage_path', 'null')\
                .order('reviewed_at', desc=True)\
                .limit(limit)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get requirement example images"
        )
        
        images = []
        if result.data:
            for item in result.data:
                if item.get('storage_path'):
                    images.append({
                        'screenshot_url': item['storage_path'],
                        'title': 'Good Example - Selfie with GoodWallet',
                        'submission_id': item['submission_id']
                    })
        
        logger.info(f"✅ Retrieved {len(images)} requirement example images")
        
        return jsonify({
            'success': True,
            'images': images,
            'count': len(images)
        })
        
    except Exception as e:
        logger.error(f"❌ Error getting requirement example images: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e), 'images': []}), 200


@community_stories_bp.route('/api/admin/bulk-approve', methods=['POST'])
def bulk_approve_submissions():
    """Bulk approve submissions with a delay between each blockchain transaction (admin only)"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        from supabase_client import is_admin
        if not is_admin(wallet):
            return jsonify({'success': False, 'error': 'Admin access required'}), 403

        data = request.get_json()
        submission_ids = data.get('submission_ids', [])
        reward_type = data.get('reward_type')
        delay_seconds = int(data.get('delay_seconds', 4))

        if not submission_ids:
            return jsonify({'success': False, 'error': 'No submission IDs provided'}), 400

        if reward_type not in ['low', 'high']:
            return jsonify({'success': False, 'error': 'Invalid reward type. Must be "low" or "high"'}), 400

        if delay_seconds < 2:
            delay_seconds = 2
        if delay_seconds > 30:
            delay_seconds = 30

        logger.info(f"📦 Admin {wallet[:8]}... bulk approving {len(submission_ids)} submissions as {reward_type} with {delay_seconds}s delay")

        results = []

        for index, submission_id in enumerate(submission_ids):
            logger.info(f"⏳ Processing {index + 1}/{len(submission_ids)}: {submission_id}")

            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    result = loop.run_until_complete(
                        community_stories_service.approve_submission(submission_id, reward_type, wallet)
                    )
                finally:
                    loop.close()

                results.append({
                    'submission_id': submission_id,
                    'success': result.get('success', False),
                    'tx_hash': result.get('tx_hash'),
                    'amount': result.get('amount'),
                    'error': result.get('error')
                })

                logger.info(f"✅ Processed {submission_id}: {result.get('success')} - {result.get('error', 'OK')}")

            except Exception as e:
                logger.error(f"❌ Error processing {submission_id}: {e}")
                results.append({
                    'submission_id': submission_id,
                    'success': False,
                    'error': str(e)
                })

            if index < len(submission_ids) - 1:
                logger.info(f"⏱️ Waiting {delay_seconds}s before next transaction...")
                time.sleep(delay_seconds)

        succeeded = [r for r in results if r['success']]
        failed = [r for r in results if not r['success']]

        logger.info(f"📊 Bulk approve complete: {len(succeeded)} succeeded, {len(failed)} failed")

        return jsonify({
            'success': True,
            'total': len(submission_ids),
            'succeeded': len(succeeded),
            'failed': len(failed),
            'results': results
        })

    except Exception as e:
        logger.error(f"❌ Error in bulk approve: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500


@community_stories_bp.route('/api/admin/bulk-reject', methods=['POST'])
def bulk_reject_submissions():
    """Bulk reject submissions (admin only)"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401

        from supabase_client import is_admin
        if not is_admin(wallet):
            return jsonify({'success': False, 'error': 'Admin access required'}), 403

        data = request.get_json()
        submission_ids = data.get('submission_ids', [])
        reason = data.get('reason', '')

        if not submission_ids:
            return jsonify({'success': False, 'error': 'No submission IDs provided'}), 400

        logger.info(f"📦 Admin {wallet[:8]}... bulk rejecting {len(submission_ids)} submissions")

        results = []

        for submission_id in submission_ids:
            try:
                result = community_stories_service.reject_submission(submission_id, wallet, reason)
                results.append({
                    'submission_id': submission_id,
                    'success': result.get('success', False),
                    'error': result.get('error')
                })
            except Exception as e:
                logger.error(f"❌ Error rejecting {submission_id}: {e}")
                results.append({
                    'submission_id': submission_id,
                    'success': False,
                    'error': str(e)
                })

        succeeded = [r for r in results if r['success']]
        failed = [r for r in results if not r['success']]

        logger.info(f"📊 Bulk reject complete: {len(succeeded)} succeeded, {len(failed)} failed")

        return jsonify({
            'success': True,
            'total': len(submission_ids),
            'succeeded': len(succeeded),
            'failed': len(failed),
            'results': results
        })

    except Exception as e:
        logger.error(f"❌ Error in bulk reject: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500

